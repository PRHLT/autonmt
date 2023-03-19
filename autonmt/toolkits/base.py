import os.path
import shutil
from abc import ABC, abstractmethod
from typing import List, Set

from autonmt.bundle.metrics import *
from autonmt.bundle.utils import *
from autonmt.preprocessing.dataset import Dataset
from autonmt.preprocessing.processors import preprocess_predict_file, pretokenize_file, encode_file, decode_file


def _check_datasets(train_ds: Dataset = None, eval_ds: Dataset = None):
    # Check that train_ds is a Dataset
    if train_ds and not isinstance(train_ds, Dataset):
        raise TypeError("'train_ds' must be an instance of 'Dataset' so that we can know the layout of the trained "
                        "model (e.g. checkpoints available, subword model, vocabularies, etc")

    # Check that train_ds is a Dataset
    if eval_ds and not isinstance(eval_ds, Dataset):
        raise TypeError("'eval_ds' must be an instance of 'Dataset' so that we can know the layout of the dataset "
                        "and get the corresponding data (e.g. splits, pretokenized, encoded, stc)")

    # Check that the preprocessing are compatible
    if train_ds and eval_ds and ((train_ds.src_lang != eval_ds.src_lang) or (train_ds.trg_lang != eval_ds.trg_lang)):
        raise ValueError(f"The languages from the train and test datasets are not compatible:\n"
                         f"\t- train_lang_pair=({train_ds.dataset_lang_pair})\n"
                         f"\t- test_lang_pair=({eval_ds.dataset_lang_pair})\n")


def _check_supported_metrics(metrics, metrics_supported):
    # Check
    metrics = set(metrics)
    metrics_supported = set(metrics_supported)

    # Get valid metrics
    metrics_valid = list(metrics.intersection(metrics_supported))
    metrics_valid += [x for x in metrics if x.startswith("hg_")]  # Ignore huggingface metrics
    metrics_valid = set(metrics_valid)
    metrics_non_valid = metrics.difference(metrics_valid)

    if metrics_non_valid:
        print(f"=> [WARNING] These metrics are not supported: {str(metrics_non_valid)}")
        if metrics == metrics_non_valid:
            print("\t- [Score]: Skipped. No valid metrics were found.")

    return metrics_valid


class BaseTranslator(ABC):

    # Global variables
    total_runs = 0
    TOOL_PARSERS = {"sacrebleu": {"filename": "sacrebleu_scores", "py": (parse_sacrebleu_json, "json")},
                    "bertscore": {"filename": "bertscore_scores", "py": (parse_bertscore_json, "json")},
                    "comet": {"filename": "comet_scores", "py": (parse_comet_json, "json")},
                    "beer": {"filename": "beer_scores", "py": (parse_beer_json, "json")},
                    "huggingface": {"filename": "huggingface_scores", "py": (parse_huggingface_json, "json")},
                    "fairseq": {"filename": "fairseq_scores", "py": (parse_fairseq_txt, "txt")},
                    }
    TOOL2METRICS = {"sacrebleu": {"bleu", "chrf", "ter"},
                    "bertscore": {"bertscore"},
                    "comet": {"comet"},
                    "beer": {"beer"},
                    "fairseq": {"fairseq"},
                    # "huggingface": "huggingface",
                    }
    METRICS2TOOL = {m: tool for tool, metrics in TOOL2METRICS.items() for m in metrics}

    def __init__(self, engine, run_prefix="model", model_ds=None, src_vocab=None, trg_vocab=None,
                 filter_tr_data_fn=None, filter_vl_data_fn=None, filter_ts_data_fn=None,
                 safe_seconds=3, **kwargs):
        # Store vars
        self.engine = engine
        self.run_prefix = run_prefix
        self.model_ds = model_ds
        self.config = {}
        self.model_ds = None
        self.safe_seconds = safe_seconds

        # Set vocab (optional)
        self.src_vocab = src_vocab
        self.trg_vocab = trg_vocab

        # Further split/preprocess each dataset if needed
        self.filter_tr_data_fn = ('', None) if not filter_tr_data_fn else filter_tr_data_fn
        self.filter_vl_data_fn = [('', None)] if not filter_vl_data_fn else filter_vl_data_fn
        self.filter_ts_data_fn = [('', None)] if not filter_ts_data_fn else filter_ts_data_fn

        # Check dataset
        _check_datasets(train_ds=self.model_ds) if self.model_ds else None

    def _get_metrics_tool(self, metrics):
        tools = set()
        for m in metrics:
            if m.startswith("hg_"):
                m_tool = "huggingface"
            else:
                m_tool = self.METRICS2TOOL.get(m)

            # Add tools
            if m_tool:
                tools.add(m_tool)
        return tools

    def _add_config(self, key: str, values: dict, reset=False):
        def is_valid(k, v):
            primitive_types = (str, bool, int, float, dict, set, list)  # Problems with list of objects
            return not(k.startswith("_") or k in {"kwargs"}) and (isinstance(v, primitive_types) or v is None)

        def parse_value(x):
            if isinstance(x, (list, set)):
                return [str(_x) for _x in x]
            return str(x)

        # Reset value (if needed)
        if reset or key not in self.config:
            self.config[key] = {}

        # Update values
        self.config[key].update({k: parse_value(v) for k, v in values.items() if is_valid(k, v)})

    def fit(self, train_ds, max_tokens=None, batch_size=128, max_epochs=1,
            learning_rate=0.001, optimizer="adam", weight_decay=0, gradient_clip_val=0.0, accumulate_grad_batches=1,
            criterion="cross_entropy", patience=None, seed=None, devices="auto", accelerator="auto", num_workers=0,
            monitor="val_loss", resume_training=False, force_overwrite=False, **kwargs):
        print("=> [Fit]: Started.")

        # Set model
        self.model_ds = train_ds

        # Store config (and save file)
        self._add_config(key="fit", values=locals(), reset=False)
        self._add_config(key="fit", values=kwargs, reset=False)
        logs_path = train_ds.get_model_logs_path(toolkit=self.engine, run_name=train_ds.get_run_name(self.run_prefix))
        make_dir(logs_path)
        save_json(self.config, savepath=os.path.join(logs_path, "config_train.json"))

        # Train and preprocess
        self.preprocess(train_ds, apply2train=True, apply2val=True, apply2test=False, force_overwrite=force_overwrite, **kwargs)
        self.train(train_ds, max_tokens=max_tokens, batch_size=batch_size, max_epochs=max_epochs,
                   learning_rate=learning_rate, optimizer=optimizer, weight_decay=weight_decay,
                   gradient_clip_val=gradient_clip_val, accumulate_grad_batches=accumulate_grad_batches,
                   criterion=criterion, patience=patience, seed=seed, devices=devices, accelerator=accelerator,
                   num_workers=num_workers, monitor=monitor, resume_training=resume_training,
                   force_overwrite=force_overwrite, **kwargs)

    def predict(self, eval_datasets: List[Dataset], beams: List[int] = None,
                metrics: Set[str] = None, batch_size=64, max_tokens=None, max_len_a=1.2, max_len_b=50, truncate_at=None,
                devices="auto", accelerator="auto", num_workers=0, load_best_checkpoint=False,
                model_ds=None, force_overwrite=False, **kwargs):
        print("=> [Predict]: Started.")

        # Set default values
        if beams is None:
            beams = [5]
        else:
            beams = list(set(beams))
            beams.sort(reverse=True)

        # Default metrics
        if metrics is None:
            metrics = {"bleu"}
        else:
            metrics = set(metrics)

        # Get model dataset
        if model_ds:
            self.model_ds = model_ds
        elif self.model_ds:
            pass
        else:
            raise ValueError(f"Missing 'model_ds'. It's needed to get the model's path (training and eval).\n"
                             f"Use the 'model_ds=train_ds' argument, if fit() was not used before predict().")

        # Store config
        self._add_config(key="predict", values=locals(), reset=False)
        self._add_config(key="predict", values=kwargs, reset=False)
        logs_path = self.model_ds.get_model_logs_path(toolkit=self.engine, run_name=self.model_ds.get_run_name(self.run_prefix))
        make_dir(logs_path)
        save_json(self.config, savepath=os.path.join(logs_path, "config_predict.json"))

        # Translate and score
        eval_scores = []
        eval_datasets = self.model_ds.get_eval_datasets(eval_datasets)
        for eval_ds in eval_datasets:
            self.translate(model_ds=self.model_ds, eval_ds=eval_ds, beams=beams, max_len_a=max_len_a, max_len_b=max_len_b,
                           truncate_at=truncate_at, batch_size=batch_size, max_tokens=max_tokens,
                           devices=devices, accelerator=accelerator, num_workers=num_workers,
                           load_best_checkpoint=load_best_checkpoint, force_overwrite=force_overwrite, **kwargs)
            self.score(model_ds=self.model_ds, eval_ds=eval_ds, beams=beams, metrics=metrics,
                       force_overwrite=force_overwrite, **kwargs)
            model_scores = self.parse_metrics(model_ds=self.model_ds, eval_ds=eval_ds, beams=beams, metrics=metrics,
                                              engine=self.engine, force_overwrite=force_overwrite, **kwargs)
            eval_scores.append(model_scores)
        return eval_scores

    @abstractmethod
    def _preprocess(self, *args, **kwargs):
        pass

    def preprocess(self, ds: Dataset, apply2train, apply2val, apply2test, force_overwrite, **kwargs):
        print(f"=> [Preprocess]: Started. ({ds.id2(as_path=True)})")

        # Set vars
        src_lang = ds.src_lang
        trg_lang = ds.trg_lang
        train_path = ds.get_encoded_path(fname=ds.train_name)
        val_path = ds.get_encoded_path(fname=ds.val_name)
        test_path = ds.get_encoded_path(fname=ds.test_name)
        model_src_vocab_path = ds.get_vocab_file(lang=src_lang)
        model_trg_vocab_path = ds.get_vocab_file(lang=trg_lang)

        start_time = time.time()
        self._preprocess(ds=ds, output_path=None,
                         src_lang=src_lang, trg_lang=trg_lang,
                         train_path=train_path, val_path=val_path, test_path=test_path,
                         src_vocab_path=model_src_vocab_path, trg_vocab_path=model_trg_vocab_path,
                         apply2train=apply2train, apply2val=apply2val, apply2test=apply2test,
                         force_overwrite=force_overwrite, **kwargs)
        print(f"\t- [INFO]: Preprocess time: {str(datetime.timedelta(seconds=time.time()-start_time))}")

    @abstractmethod
    def _train(self, *args, **kwargs):
        pass

    def train(self, train_ds: Dataset, resume_training, force_overwrite, **kwargs):
        print(f"=> [Train]: Started. ({train_ds.id2(as_path=True)})")

        # Check preprocessing
        _check_datasets(train_ds=train_ds)

        # Check debug
        if is_debug_enabled():
            print("\t=> [WARNING]: Debug is enabled. This could lead to critical problems when using a data parallel strategy.")

        # Set run name
        run_name = train_ds.get_run_name(self.run_prefix)

        # Set paths
        checkpoints_dir = train_ds.get_model_checkpoints_path(toolkit=self.engine, run_name=run_name)
        logs_path = train_ds.get_model_logs_path(toolkit=self.engine, run_name=run_name)

        # Create dirs
        make_dir([checkpoints_dir, logs_path])

        # Set seed
        self.manual_seed(seed=kwargs.get("seed"))

        start_time = time.time()
        self._train(train_ds=train_ds, checkpoints_dir=checkpoints_dir, logs_path=logs_path,
                    run_name=run_name, resume_training=resume_training, force_overwrite=force_overwrite, **kwargs)
        print(f"\t- [INFO]: Training time: {str(datetime.timedelta(seconds=time.time()-start_time))}")

    @abstractmethod
    def _translate(self, *args, **kwargs):
        pass

    def translate(self, model_ds: Dataset, eval_ds: Dataset, beams: List[int], max_len_a, max_len_b, truncate_at,
                  batch_size, max_tokens, num_workers, force_overwrite, **kwargs):
        print(f"=> [Translate]: Started. ({model_ds.id2(as_path=True)})")

        # Check preprocessing
        _check_datasets(train_ds=model_ds, eval_ds=eval_ds)
        assert model_ds.dataset_lang_pair == eval_ds.dataset_lang_pair

        # Set run names
        run_name = model_ds.get_run_name(self.run_prefix)
        eval_name = '_'.join(eval_ds.id())  # Subword model and vocab size don't characterize the dataset!

        # Checkpoints dir
        checkpoints_dir = model_ds.get_model_checkpoints_path(self.engine, run_name)

        # [Trained model]: Create eval folder
        model_src_vocab_path = model_ds.get_vocab_file(lang=model_ds.src_lang)  # Needed to preprocess
        model_trg_vocab_path = model_ds.get_vocab_file(lang=model_ds.trg_lang)  # Needed to preprocess
        model_eval_path = model_ds.get_model_eval_path(toolkit=self.engine, run_name=run_name, eval_name=eval_name)
        make_dir([model_eval_path])  # Create dir

        # Create data directories
        dst_raw_path = os.path.join(model_eval_path, model_ds.data_raw_path)
        dst_preprocessed_path = os.path.join(model_eval_path, model_ds.data_raw_preprocessed_path)
        dst_encoded_path = os.path.join(model_eval_path, model_ds.data_encoded_path)
        make_dir([dst_raw_path, dst_preprocessed_path, dst_encoded_path])  # Create dirs

        # [Encode extern data]: Encode test data using the subword model of the trained model
        for ts_fname in [fname for fname in eval_ds.split_names_lang if eval_ds.test_name in fname]:
            lang = ts_fname.split('.')[-1]
            input_file = eval_ds.get_split_path(ts_fname)  # As "raw" as possible. The split preprocessing will depend on the model

            # 1 - Get source file
            source_file = os.path.join(dst_raw_path, ts_fname)
            shutil.copyfile(input_file, source_file)
            input_file = source_file

            # 2 - Preprocess file (+pretokenization if needed)
            preprocessed_file = os.path.join(dst_preprocessed_path, ts_fname)
            preprocess_predict_file(input_file=input_file, output_file=preprocessed_file,
                           preprocess_fn=model_ds.preprocess_predict_fn, pretokenize=model_ds.pretok_flag, lang=lang,
                           force_overwrite=force_overwrite)
            input_file = preprocessed_file

            # Encode file
            enc_file = os.path.join(dst_encoded_path, ts_fname)
            encode_file(ds=model_ds, input_file=input_file, output_file=enc_file,
                        lang=lang, merge_vocabs=model_ds.merge_vocabs, truncate_at=truncate_at,
                        force_overwrite=force_overwrite)

        # Preprocess external data
        test_path = os.path.join(dst_encoded_path, eval_ds.test_name)  # without lang extension
        self._preprocess(ds=model_ds, output_path=model_eval_path,
                         src_lang=model_ds.src_lang, trg_lang=model_ds.trg_lang,
                         train_path=None, val_path=None, test_path=test_path,
                         src_vocab_path=model_src_vocab_path, trg_vocab_path=model_trg_vocab_path,
                         subword_model=model_ds.subword_model, pretok_flag=model_ds.pretok_flag,
                         apply2train=False, apply2val=False, apply2test=True, force_overwrite=force_overwrite,
                         **kwargs)

        # Allow to split ts data (optional)
        for i, (fn_name, filter_fn) in enumerate(self.filter_ts_data_fn):
            extra_str = f" | split='{fn_name}'" if fn_name else ""

            # Iterate over beams
            for beam in beams:
                start_time = time.time()
                # Create output path (if needed)
                output_path = model_ds.get_model_eval_translations_beam_path(toolkit=self.engine, run_name=run_name,
                                                                             eval_name=eval_name, split_name=fn_name,
                                                                             beam=beam)
                make_dir(output_path)

                # Translate
                tok_flag = [os.path.exists(os.path.join(output_path, f)) for f in ["hyp.tok"]]
                if force_overwrite or not all(tok_flag):
                    self._translate(model_ds=model_ds, data_path=model_eval_path, output_path=output_path,
                        src_lang=model_ds.src_lang, trg_lang=model_ds.trg_lang,
                        beam_width=beam, max_len_a=max_len_a, max_len_b=max_len_b, batch_size=batch_size, max_tokens=max_tokens,
                        checkpoints_dir=checkpoints_dir,
                        model_src_vocab_path=model_src_vocab_path, model_trg_vocab_path=model_trg_vocab_path,
                        num_workers=num_workers, force_overwrite=force_overwrite, filter_idx=i, **kwargs)

                    # Copy src/ref raw
                    src_input_file = os.path.join(dst_raw_path, f"{eval_ds.test_name}.{model_ds.src_lang}")
                    src_output_file = os.path.join(output_path, f"src.txt")
                    ref_input_file = os.path.join(dst_raw_path, f"{eval_ds.test_name}.{model_ds.trg_lang}")
                    ref_output_file = os.path.join(output_path, f"ref.txt")

                    # Filter src/ref if needed
                    if not filter_fn:
                        shutil.copyfile(src_input_file, src_output_file)  # Copy src raw files
                        shutil.copyfile(ref_input_file, ref_output_file)  # Copy trg raw files
                    else:
                        print(f"Filtering src/ref raw files (split='{fn_name}')...")
                        src_ref_lines = read_file_lines(filename=src_input_file, autoclean=True)
                        trg_ref_lines = read_file_lines(filename=ref_input_file, autoclean=True)
                        src_ref_lines, trg_ref_lines = filter_fn(src_ref_lines, trg_ref_lines, from_fn="translate")
                        write_file_lines(filename=src_output_file, lines=src_ref_lines, autoclean=True, insert_break_line=True)
                        write_file_lines(filename=ref_output_file, lines=trg_ref_lines, autoclean=True, insert_break_line=True)

                    # Postprocess src/ref files (lowercase, strip,...)
                    if model_ds.preprocess_predict_fn:
                        preprocess_predict_file(input_file=src_output_file, output_file=src_output_file,
                                                preprocess_fn=model_ds.preprocess_predict_fn,
                                                pretokenize=model_ds.pretok_flag, lang=model_ds.src_lang,
                                                force_overwrite=force_overwrite)
                        preprocess_predict_file(input_file=ref_output_file, output_file=ref_output_file,
                                                preprocess_fn=model_ds.preprocess_predict_fn,
                                                pretokenize=model_ds.pretok_flag, lang=model_ds.trg_lang,
                                                force_overwrite=force_overwrite)

                    # Postprocess tokenized files
                    for fname, lang in [("hyp", model_ds.trg_lang)]:
                        input_file = os.path.join(output_path, f"{fname}.tok")
                        output_file = os.path.join(output_path, f"{fname}.txt")
                        model_vocab_path = model_src_vocab_path if lang == model_ds.src_lang else model_trg_vocab_path

                        # Post-process files
                        decode_file(input_file=input_file, output_file=output_file, lang=lang,
                                    subword_model=model_ds.subword_model, pretok_flag=model_ds.pretok_flag,
                                    model_vocab_path=model_vocab_path, remove_unk_hyphen=True,
                                    force_overwrite=force_overwrite)

                    # Check amount of lines
                    num_lines_ref = count_file_lines(os.path.join(output_path, "ref.txt"))
                    num_lines_hyp = count_file_lines(os.path.join(output_path, "hyp.txt"))
                    if num_lines_ref != num_lines_hyp:
                        raise ValueError(f"The number of lines in 'ref.txt' ({num_lines_ref}) and 'hyp.txt' ({num_lines_hyp}) "
                                         f"does not match. If you see a 'CUDA out of memory' message, try again with "
                                         f"smaller batch.")

                print(f"\t- [INFO]: Translating time (beam={str(beam)}{extra_str}): {str(datetime.timedelta(seconds=time.time() - start_time))}")


    def score(self, model_ds: Dataset, eval_ds: Dataset, beams: List[int], metrics: Set[str], force_overwrite, **kwargs):
        print(f"=> [Score]: Started. ({model_ds.id2(as_path=True)})")

        # Check preprocessing
        _check_datasets(train_ds=model_ds, eval_ds=eval_ds)
        assert model_ds.dataset_lang_pair == eval_ds.dataset_lang_pair

        # Check supported metrics
        metrics_valid = _check_supported_metrics(metrics, self.METRICS2TOOL.keys())
        if not metrics_valid:
            return

        # Set run names
        run_name = model_ds.get_run_name(self.run_prefix)
        eval_name = '_'.join(eval_ds.id())  # Subword model and vocab size don't characterize the dataset!

        # Allow to split ts data (optional)
        for fn_name, _ in self.filter_ts_data_fn:
            extra_str = f" | split='{fn_name}'" if fn_name else ""

            # Iterate over beams
            for beam in beams:
                start_time = time.time()

                # Paths
                beam_path = model_ds.get_model_eval_translations_beam_path(toolkit=self.engine, run_name=run_name,
                                                                           eval_name=eval_name, split_name=fn_name,
                                                                           beam=beam)
                scores_path = model_ds.get_model_eval_translations_beam_scores_path(toolkit=self.engine, run_name=run_name, eval_name=eval_name, split_name=fn_name, beam=beam)
                make_dir([scores_path])

                # Set input files (results)
                src_file_path = os.path.join(beam_path, "src.txt")
                ref_file_path = os.path.join(beam_path, "ref.txt")
                hyp_file_path = os.path.join(beam_path, "hyp.txt")

                # Check that the paths exists
                if not all([os.path.exists(p) for p in [src_file_path, ref_file_path, hyp_file_path]]):
                    raise IOError("Missing files to compute scores")

                # Score: bleu, chrf and ter
                if self.TOOL2METRICS["sacrebleu"].intersection(metrics):
                    output_file = os.path.join(scores_path, f"sacrebleu_scores.json")
                    if force_overwrite or not os.path.exists(output_file):
                        compute_sacrebleu(ref_file=ref_file_path, hyp_file=hyp_file_path, output_file=output_file, metrics=metrics)

                # Score: bertscore
                if self.TOOL2METRICS["bertscore"].intersection(metrics):
                    output_file = os.path.join(scores_path, f"bertscore_scores.json")
                    if force_overwrite or not os.path.exists(output_file):
                        compute_bertscore(ref_file=ref_file_path, hyp_file=hyp_file_path, output_file=output_file, trg_lang=model_ds.trg_lang)

                # Score: comet
                if self.TOOL2METRICS["comet"].intersection(metrics):
                    output_file = os.path.join(scores_path, f"comet_scores.json")
                    if force_overwrite or not os.path.exists(output_file):
                        compute_comet(src_file=src_file_path, ref_file=ref_file_path, hyp_file=hyp_file_path, output_file=output_file)

                 # Score: fairseq
                if self.TOOL2METRICS["fairseq"].intersection(metrics):
                    output_file = os.path.join(scores_path, f"fairseq_scores.txt")
                    if force_overwrite or not os.path.exists(output_file):
                        compute_fairseq(ref_file=ref_file_path, hyp_file=hyp_file_path, output_file=output_file)

                # Huggingface metrics
                hg_metrics = {x[3:] for x in metrics if x.startswith("hg_")}
                if hg_metrics:
                    output_file = os.path.join(scores_path, f"huggingface_scores.json")
                    if force_overwrite or not os.path.exists(output_file):
                        compute_huggingface(src_file=src_file_path, hyp_file=hyp_file_path, ref_file=ref_file_path,
                                            output_file=output_file, metrics=hg_metrics, trg_lang=model_ds.trg_lang)

                print(f"\t- [INFO]: Scoring time (beam={str(beam)}{extra_str}): {str(datetime.timedelta(seconds=time.time() - start_time))}")


    def parse_metrics(self, model_ds, eval_ds, beams: List[int], metrics: Set[str], force_overwrite, **kwargs):
        print(f"=> [Parsing]: Started. ({model_ds.id2(as_path=True)})")

        # Check preprocessing
        _check_datasets(train_ds=model_ds, eval_ds=eval_ds)
        assert model_ds.dataset_lang_pair == eval_ds.dataset_lang_pair

        # Check supported metrics
        metrics_valid = _check_supported_metrics(metrics, self.METRICS2TOOL.keys())
        if not metrics_valid:
            return

        # Metrics to retrieve
        metric_tools = self._get_metrics_tool(metrics)

        # Set run names
        run_name = model_ds.get_run_name(self.run_prefix)
        eval_name = '_'.join(eval_ds.id())  # Subword model and vocab size don't characterize the dataset!

        # Walk through beams
        scores = {
            "engine": kwargs.get("engine"),
            "lang_pair": model_ds.dataset_lang_pair,
            "train_dataset": model_ds.dataset_name,
            "eval_dataset": eval_ds.dataset_name,
            "subword_model": str(model_ds.subword_model).lower(),
            "vocab_size": str(model_ds.vocab_size).lower(),
            "run_name": run_name,
            "train_max_lines": model_ds.dataset_lines,
            "translations": {},
            "config": self.config,
        }

        # Allow to split ts data (optional)
        for fn_name, _ in self.filter_ts_data_fn:
            extra_str = f" | split='{fn_name}'" if fn_name else ""

            # Iterate over beams
            for beam in beams:
                # Paths
                scores_path = model_ds.get_model_eval_translations_beam_scores_path(toolkit=self.engine, run_name=run_name, eval_name=eval_name, split_name=fn_name, beam=beam)

                # Walk through metric files
                beam_scores = {}
                for m_tool in metric_tools:
                    values = self.TOOL_PARSERS[m_tool]
                    m_parser, ext = values["py"]
                    m_fname = f"{values['filename']}.{ext}"

                    # Read file
                    filename = os.path.join(scores_path, m_fname)
                    if os.path.exists(filename):
                        try:
                            with open(filename, 'r') as f:
                                m_scores = m_parser(text=f.readlines())
                                for m_name, m_values in m_scores.items():  # [bleu_score, chrf_score, ter_score], [bertscore_precision]
                                    for score_name, score_value in m_values.items():
                                        m_name_full = f"{m_tool}_{m_name}_{score_name}".lower().strip()
                                        beam_scores[m_name_full] = score_value
                        except Exception as e:
                            print(f"\t- [PARSING ERROR]: ({m_fname}) {str(e)}")
                    else:
                        print(f"\t- [WARNING]: There are no metrics from '{m_tool}'")

                # Add beam scores
                d = {f"beam{str(beam)}": beam_scores}
                d = {fn_name: d} if fn_name else d  # Pretty
                scores["translations"].update(d)
        return scores

    @staticmethod
    def manual_seed(seed, use_deterministic_algorithms=False):
        import torch
        import random
        import numpy as np
        from lightning_fabric.utilities.seed import seed_everything

        # Define seed
        seed = seed if seed is not None else int(time.time()) % 2**32

        # Set seeds
        random.seed(seed)
        np.random.seed(seed)
        torch.manual_seed(seed)
        seed_everything(seed)

        # Tricky: https://pytorch.org/docs/stable/generated/torch.use_deterministic_algorithms.html
        torch.use_deterministic_algorithms(use_deterministic_algorithms)

        # Test randomness
        print(f"\t- [INFO]: Testing random seed ({seed}):")
        print(f"\t\t- random: {random.random()}")
        print(f"\t\t- numpy: {np.random.rand(1)}")
        print(f"\t\t- torch: {torch.rand(1)}")

        return seed
