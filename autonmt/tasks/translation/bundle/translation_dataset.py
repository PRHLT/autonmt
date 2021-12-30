import os
import math
import torch
import numpy as np
import pandas as pd

from torch.utils.data import Dataset, DataLoader
from torch.nn.utils.rnn import pad_sequence

from autonmt.utils import read_file_lines


class TranslationDataset(Dataset):
    def __init__(self, file_prefix, src_lang, trg_lang, src_vocab=None, trg_vocab=None, max_src_positions=None,
                 max_trg_positions=None, **kwargs):
        # Get src/trg file paths
        src_file_path = file_prefix.strip() + f".{src_lang}"
        trg_file_path = file_prefix.strip() + f".{trg_lang}"

        # Read files
        self.src_lines = read_file_lines(filename=src_file_path)
        self.trg_lines = read_file_lines(filename=trg_file_path)
        assert len(self.src_lines) == len(self.trg_lines)

        # Read langs
        self.src_lang = src_lang
        self.trg_lang = trg_lang

        # Set vocabs
        self.src_vocab = src_vocab
        self.trg_vocab = trg_vocab

        # Other
        self.max_src_positions = max_src_positions
        self.max_trg_positions = max_trg_positions

    def __len__(self):
        return len(self.src_lines)

    def __getitem__(self, idx):
        src_line, trg_line = self.src_lines[idx], self.trg_lines[idx]
        return src_line, trg_line

    def collate_fn(self, batch, max_tokens=None):
        x_encoded, y_encoded = [], []
        x_max_len = y_max_len = 0

        # Add elements to batch
        for i, (x, y) in enumerate(batch):
            # Encode tokens
            _x = self.src_vocab.encode(x, max_length=self.max_src_positions)
            _y = self.trg_vocab.encode(y, max_length=self.max_trg_positions)

            # Control tokens in batch
            x_max_len = max(x_max_len, len(_x))
            y_max_len = max(y_max_len, len(_y))

            # Add elements
            if max_tokens is None or (i+1)*(x_max_len+y_max_len) <= max_tokens:  # sample*size
                x_encoded.append(torch.tensor(_x, dtype=torch.long))
                y_encoded.append(torch.tensor(_y, dtype=torch.long))
            else:
                msg = "[WARNING] Dropping {:.2f}% of the batch because the maximum number of tokens ({}) was exceeded"
                drop_ratio = 1 - ((i+1)/len(batch))
                print(msg.format(drop_ratio, max_tokens))
                break

        # Pad sequence
        x_padded = pad_sequence(x_encoded, batch_first=False, padding_value=self.src_vocab.pad_id).T
        y_padded = pad_sequence(y_encoded, batch_first=False, padding_value=self.trg_vocab.pad_id).T

        # Check stuff
        assert x_padded.shape[0] == y_padded.shape[0] == len(x_encoded)  # Control samples
        assert max_tokens is None or (x_padded.numel() + y_padded.numel()) <= max_tokens  # Control max tokens
        return x_padded, y_padded
