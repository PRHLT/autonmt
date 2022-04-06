from tokenizers import normalizers
from tokenizers.normalizers import NFKC, Strip, Lowercase

from autonmt.bundle.report import generate_report, generate_multivariable_report
from autonmt.preprocessing import DatasetBuilder
from autonmt.toolkits.fairseq import FairseqTranslator


def main(fairseq_args, fairseq_venv_path):
    # Create preprocessing for training
    builder = DatasetBuilder(
        base_path="/home/scarrion/datasets/nn/translation",
        datasets=[
            {"name": "europarl", "languages": ["es-en", "fr-en", "de-en"], "sizes": [("original", None), ("100k", 100000)]},
            {"name": "scielo/health", "languages": ["es-en"], "sizes": [("100k", 100000)], "split_sizes": (None, 1000, 1000)},
        ],
        encoding=[
            {"subword_models": ["bpe", "unigram+bytes"], "vocab_sizes": [8000, 16000, 32000]},
            {"subword_models": ["bytes", "char", "char+bytes"], "vocab_sizes": [1000]},
        ],
        normalizer=normalizers.Sequence([NFKC(), Strip(), Lowercase()]),
        merge_vocabs=False,
        eval_mode="compatible",
    ).build(make_plots=False, force_overwrite=False)

    # Create preprocessing for training and testing
    tr_datasets = builder.get_train_ds()
    ts_datasets = builder.get_test_ds()

    # Train & Score a model for each dataset
    scores = []
    errors = []
    run_prefix = "transformer256emb"
    for ds in tr_datasets:
        try:
            wandb_params = None  #dict(project="fairseq", entity="salvacarrion")
            model = FairseqTranslator(fairseq_venv_path=fairseq_venv_path,
                                      model_ds=ds, wandb_params=wandb_params, force_overwrite=True, run_prefix=run_prefix)
            model.fit(resume_training=False, max_epochs=300, max_tokens=4096*2, batch_size=None, seed=1234, patience=10, num_workers=12, devices="auto", fairseq_args=fairseq_args)
            m_scores = model.predict(ts_datasets, metrics={"bleu"}, beams=[5], truncate_at=1023, max_tokens=4096*2, batch_size=None)
            print(m_scores)
            scores.append(m_scores)
        except Exception as e:
            print(e)
            errors.append(str(e))

    try:
        # Make report and print it
        output_path = f".outputs/fairseq/unigram"
        df_report, df_summary = generate_report(scores=scores, output_path=output_path, plot_metric="beam5__sacrebleu_bleu_score")

        # Plot BLEU as a function of the vocab size (one line plot per language)
        generate_multivariable_report(data=df_report,
                                      x="vocab_size",
                                      y_left=("beam5__sacrebleu_bleu_score", "lang_pair"), y_right=None,
                                      output_path=output_path, prefix="vocsizes_",
                                      save_figures=True, show_figures=False, save_csv=True)

        print("Summary:")
        print(df_report.to_string(index=False))

        print("Summary:")
        print(df_summary.to_string(index=False))
    except Exception as e:
        print(e)
        errors.append(str(e))

    # Show errors
    print("Errors:")
    print(errors)
    print(f"Total errors: {len(errors)}")


if __name__ == "__main__":
    # These args are pass to fairseq using our pipeline
    # Fairseq Command-line tools: https://fairseq.readthedocs.io/en/latest/command_line_tools.html
    fairseq_cmd_args = [
        "--arch transformer",
        "--encoder-embed-dim 256",
        "--decoder-embed-dim 256",
        "--encoder-layers 3",
        "--decoder-layers 3",
        "--encoder-attention-heads 8",
        "--decoder-attention-heads 8",
        "--encoder-ffn-embed-dim 512",
        "--decoder-ffn-embed-dim 512",
        "--dropout 0.1",

        "--lr 0.0005",
        "--weight-decay 0.0001",
        "--criterion label_smoothed_cross_entropy --label-smoothing 0.1",
        "--lr-scheduler inverse_sqrt --warmup-init-lr 1e-07 --warmup-updates 4000",
        "--optimizer adam",
        "--adam-betas '(0.9, 0.98)'",
        "--clip-norm 0.0",

        "--eval-bleu",
        "--eval-bleu-args '{\"beam\": 5, \"max_len_a\": 1.2, \"max_len_b\": 50}'",
        "--eval-bleu-detok space",
        "--eval-bleu-remove-bpe sentencepiece",
        "--eval-bleu-print-samples",
        "--scoring sacrebleu",
        "--no-epoch-checkpoints",
        "--maximize-best-checkpoint-metric",
        "--best-checkpoint-metric bleu",

        "--log-format simple",
        "--task translation",
    ]

    # Set venv path
    # To create new venvs: virtualenv -p $(which python) VENV_NAME
    fairseq_venv_path = "source /home/scarrion/venvs/fairseq/bin/activate"

    # Run grid
    main(fairseq_args=fairseq_cmd_args, fairseq_venv_path=fairseq_venv_path)
