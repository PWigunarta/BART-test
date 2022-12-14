import logging
import os
import sys
from dataclasses import dataclass, field
from typing import Optional
import torch

from seq2seq_trainer import Seq2SeqTrainer
from transformers import (

    AutoConfig,
    AutoModelForSeq2SeqLM,
    AutoTokenizer,

    HfArgumentParser,
    MBartTokenizer,
    TrainingArguments,
    set_seed,

    BartConfig,
    BartTokenizer,
    BartForConditionalGeneration,
)

from transformers.trainer_utils import EvaluationStrategy

from utils import (

    LegacySeq2SeqDataset,
    Seq2SeqDataCollator,
    assert_all_frozen,
    build_compute_metrics_fn,
    freeze_embeds,
    freeze_params,
    lmap,
    save_json,
    use_task_specific_params,
    write_txt_file,

)

from evals.eval_diversity import eval_diversity
from evals.eval_accuracy import eval_top1_acc, eval_topk_acc

logger = logging.getLogger(__name__)


@dataclass
class Seq2SeqTrainingArguments(TrainingArguments):

    label_smoothing: Optional[float] = field(default=0.0, metadata={"help": "The label smoothing epsilon to apply (if not zero)."})
    sortish_sampler: bool = field(default=False, metadata={"help": "Whether to SortishSamler or not."})
    predict_with_generate: bool = field(default=False, metadata={"help": "Whether to use generate to calculate generative metrics (ROUGE, BLEU)."})
    adafactor: bool = field(default=False, metadata={"help": "whether to use adafactor"})
    encoder_layerdrop: Optional[float] = field(default=None, metadata={"help": "Encoder layer dropout probability. Goes into model.config."})
    decoder_layerdrop: Optional[float] = field(default=None, metadata={"help": "Decoder layer dropout probability. Goes into model.config."})
    dropout: Optional[float] = field(default=None, metadata={"help": "Dropout probability. Goes into model.config."})
    attention_dropout: Optional[float] = field(default=None, metadata={"help": "Attention dropout probability. Goes into model.config."})

@dataclass
class ModelArguments:

    model_name_or_path: str = field(metadata={"help": "Path to pretrained model or model identifier from huggingface.co/models"})
    config_name: Optional[str] = field(default=None, metadata={"help": "Pretrained config name or path if not the same as model_name"})
    tokenizer_name: Optional[str] = field(default=None, metadata={"help": "Pretrained tokenizer name or path if not the same as model_name"})
    cache_dir: Optional[str] = field(default=None, metadata={"help": "Where do you want to store the pretrained models downloaded from s3"})
    freeze_encoder: bool = field(default=False, metadata={"help": "Whether tp freeze the encoder."})
    freeze_embeds: bool = field(default=False, metadata={"help": "Whether  to freeze the embeddings."})


@dataclass
class DataTrainingArguments:

    data_dir: str = field(metadata={"help": "The input data dir. Should contain the .tsv files (or other data files) for the task."})
    task: Optional[str] = field(default="summarization",
        metadata={"help": "Task name, summarization (or summarization_{dataset} for pegasus) or translation"},)
    max_source_length: Optional[int] = field(default=512,
        metadata={"help": "The maximum total input sequence length after tokenization. Sequences longer "
            "than this will be truncated, sequences shorter will be padded."},)
    max_target_length: Optional[int] = field(default=128,
        metadata={"help": "The maximum total sequence length for target text after tokenization. Sequences longer "
            "than this will be truncated, sequences shorter will be padded."},)
    val_max_target_length: Optional[int] = field(default=128,
        metadata={"help": "The maximum total sequence length for validation target text after tokenization. Sequences longer "
            "than this will be truncated, sequences shorter will be padded."},)
    test_max_target_length: Optional[int] = field(default=128,
        metadata={"help": "The maximum total sequence length for test target text after tokenization. Sequences longer "
            "than this will be truncated, sequences shorter will be padded."},)

    n_train: Optional[int] = field(default=-1, metadata={"help": "# training examples. -1 means use all."})
    n_val: Optional[int] = field(default=-1, metadata={"help": "# validation examples. -1 means use all."})
    n_test: Optional[int] = field(default=-1, metadata={"help": "# test examples. -1 means use all."})
    src_lang: Optional[str] = field(default=None, metadata={"help": "Source language id for translation."})
    tgt_lang: Optional[str] = field(default=None, metadata={"help": "Target language id for translation."})
    eval_beams: Optional[int] = field(default=None, metadata={"help": "# num_beams to use for evaluation."})
    top_k: Optional[int] = field(default=0, metadata={"help": "keep only top k tokens with highest probability (top-k filtering)"})
    top_p: Optional[float] = field(default=1.0, metadata={"help": "keep the top tokens with cumulative probability >= top_p (nucleus filtering)"})
    do_sample: Optional[bool] = field(default=False, metadata={"help": "# Do sampling (multinomial/neclus sampling)."})


def main():
    parser = HfArgumentParser((ModelArguments, DataTrainingArguments, Seq2SeqTrainingArguments))

    if len(sys.argv) == 2 and sys.argv[1].endswith(".json"):
        model_args, data_args, training_args = parser.parse_json_file(json_file=os.path.abspath(sys.argv[1]))
    else:
        model_args, data_args, training_args = parser.parse_args_into_dataclasses()

    # n_sample for evluating the models during training
    training_args.eval_beams = data_args.eval_beams
    training_args.data_dir = data_args.data_dir

    # Ensure output dir is not existed
    if (
        os.path.exists(training_args.output_dir)
        and os.listdir(training_args.output_dir)
        and training_args.do_train
        and not training_args.overwrite_output_dir
    ):
        raise ValueError(
            f"Output directory ({training_args.output_dir}) already exists and is not empty. Use --overwrite_output_dir to overcome."
        )

    # Setup logging
    logging.basicConfig(
        format="%(asctime)s - %(levelname)s - %(name)s -   %(message)s",
        datefmt="%m/%d/%Y %H:%M:%S",
        level=logging.INFO if training_args.local_rank in [-1, 0] else logging.WARN,
    )

    logger.warning(
        "Process rank: %s, device: %s, n_gpu: %s, distributed training: %s, 16-bits training: %s",
        training_args.local_rank,
        training_args.device,
        training_args.n_gpu,
        bool(training_args.local_rank != -1),
        training_args.fp16,
    )
    logger.info("Training/evaluation parameters %s", training_args)

    # Set seed
    set_seed(training_args.seed)

    config = BartConfig.from_pretrained(
    # config = AutoConfig.from_pretrained(
        model_args.config_name if model_args.config_name else model_args.model_name_or_path,
        cache_dir=model_args.cache_dir,
    )

    extra_model_params = ("encoder_layerdrop", "decoder_layerdrop", "dropout", "attention_dropout")
    for p in extra_model_params:
        if getattr(training_args, p, None):
            assert hasattr(config, p), f"({config.__class__.__name__}) doesn't have a `{p}` attribute"
            setattr(config, p, getattr(training_args, p))

    tokenizer = BartTokenizer.from_pretrained(
        model_args.tokenizer_name if model_args.tokenizer_name else model_args.model_name_or_path,
        cache_dir=model_args.cache_dir,
    )

    model = BartForConditionalGeneration.from_pretrained(
        model_args.model_name_or_path,
        from_tf=".ckpt" in model_args.model_name_or_path,
        config=config,
        cache_dir=model_args.cache_dir,
    )
    # use task specific params, e.g., data_args.task = 'summarization'
    use_task_specific_params(model, data_args.task)

    # set num_beams for evaluation
    if data_args.eval_beams is None:
        data_args.eval_beams = model.config.num_beams

    # set decoder_start_token_id for MBart
    if model.config.decoder_start_token_id is None and isinstance(tokenizer, MBartTokenizer):
        assert (
            data_args.tgt_lang is not None and data_args.src_lang is not None
        ), "mBart requires --tgt_lang and --src_lang"
        model.config.decoder_start_token_id = tokenizer.lang_code_to_id[data_args.tgt_lang]

    if model_args.freeze_embeds:
        freeze_embeds(model)
    if model_args.freeze_encoder:
        freeze_params(model.get_encoder())
        assert_all_frozen(model.get_encoder())

    # Get datasets
    train_dataset = (
        LegacySeq2SeqDataset(
            tokenizer=tokenizer,
            type_path="train",
            data_dir=data_args.data_dir,
            n_obs=data_args.n_train,
            max_target_length=data_args.max_target_length,
            max_source_length=data_args.max_source_length,
            prefix=model.config.prefix or "",
        )
        if training_args.do_train
        else None
    )

    eval_dataset = (
        LegacySeq2SeqDataset(
            tokenizer=tokenizer,
            type_path="val",
            data_dir=data_args.data_dir,
            n_obs=data_args.n_val,  
            max_target_length=data_args.val_max_target_length,
            max_source_length=data_args.max_source_length,
            prefix=model.config.prefix or "",
        )
        if training_args.do_eval or training_args.evaluation_strategy != EvaluationStrategy.NO
        else None
    )

    test_dataset = (
        LegacySeq2SeqDataset(
            tokenizer=tokenizer,
            type_path="test",
            data_dir=data_args.data_dir,
            n_obs=data_args.n_test,
            max_target_length=data_args.test_max_target_length,
            max_source_length=data_args.max_source_length,
            prefix=model.config.prefix or "",
        )
        if training_args.do_predict
        else None
    )

    # Initialize our Trainer
    compute_metrics_fn = (
        build_compute_metrics_fn(data_args.task, tokenizer) if training_args.predict_with_generate else None
    )

    trainer = Seq2SeqTrainer(
        model=model,
        config=config,
        tokenizer=tokenizer,
        args=training_args,
        train_dataset=train_dataset,
        eval_dataset=eval_dataset,
        data_collator=Seq2SeqDataCollator(tokenizer, data_args, training_args.tpu_num_cores),
        compute_metrics=compute_metrics_fn,
        data_args=data_args,
    )

    # Training
    if training_args.do_train:
        trainer.train(model_path=model_args.model_name_or_path if os.path.isdir(model_args.model_name_or_path) else None)

    # Evaluation (on dev set)
    eval_results = {}
    if training_args.do_eval:

        output = trainer.evaluate()
        predictions = output.predictions.tolist()

        out_pred_path = training_args.output_dir + '/output_epoch_pred.txt'
        out_pred_metric = training_args.output_dir + '/output_metric_pred.txt'
        out_pred_ref = data_args.data_dir + '/val.target'

        with open(out_pred_path, 'w') as eval_out:
            for pred in predictions:
                output_line = tokenizer.decode(pred, 
                        skip_special_tokens=True, clean_up_tokenization_spaces=False)
                eval_out.write(output_line + '\n')

        metrics = {'epoch': 'eval_mode'}
        metrics.update(eval_top1_acc(out_pred_path, out_pred_ref, data_args.eval_beams)) ## top1_metrics
        metrics.update(eval_topk_acc(out_pred_path, out_pred_ref, data_args.eval_beams))  ## topk_metrics
        metrics.update(eval_diversity(out_pred_path, data_args.eval_beams)) ## diversity_metrics

        with open(out_pred_metric, 'w') as metric_out:
            json.dump(metrics, metric_out, indent=1)

    if training_args.do_predict:
        logging.info("*** Test ***")

        test_output = trainer.predict(test_dataset=test_dataset)
        test_metrics = {k.replace("eval", "test"): v for k, v in test_output.metrics.items()}

        if trainer.is_world_process_zero():
            logger.info("***** Test results *****")
            for key, value in test_metrics.items():
                logger.info("  %s = %s", key, value)

            save_json(test_metrics, os.path.join(training_args.output_dir, "test_results.json"))
            eval_results.update(test_metrics)

            if training_args.predict_with_generate:
                test_preds = tokenizer.batch_decode(
                    test_output.predictions, skip_special_tokens=True, clean_up_tokenization_spaces=True
                )
                test_preds = lmap(str.strip, test_preds)
                write_txt_file(test_preds, os.path.join(training_args.output_dir, "test_generations.txt"))

    return eval_results


if __name__ == "__main__":
    main()
