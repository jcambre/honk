from enum import Enum
import hashlib
import math
import os
import random
import re

from chainmap import ChainMap
from torch.autograd import Variable
import librosa
import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.utils.data as data

from .manage_audio import preprocess_audio

class SimpleCache(dict):
    def __init__(self, limit):
        super().__init__()
        self.limit = limit
        self.n_keys = 0

    def __setitem__(self, key, value):
        if key in self.keys():
            super().__setitem__(key, value)
        elif self.n_keys < self.limit:
            self.n_keys += 1
            super().__setitem__(key, value)
        return value

class ConfigType(Enum):
    CNN_TRAD_POOL2 = "cnn-trad-pool2" # default full model (TF variant)
    CNN_ONE_STRIDE1 = "cnn-one-stride1" # default compact model (TF variant)
    CNN_ONE_FPOOL3 = "cnn-one-fpool3"
    CNN_ONE_FSTRIDE4 = "cnn-one-fstride4"
    CNN_ONE_FSTRIDE8 = "cnn-one-fstride8"
    CNN_TPOOL2 = "cnn-tpool2"
    CNN_TPOOL3 = "cnn-tpool3"
    CNN_TSTRIDE2 = "cnn-tstride2"
    CNN_TSTRIDE4 = "cnn-tstride4"
    CNN_TSTRIDE8 = "cnn-tstride8"
    RES15 = "res15"
    RES26 = "res26"
    RES8 = "res8"
    RES15_NARROW = "res15-narrow"
    RES8_NARROW = "res8-narrow"
    RES26_NARROW = "res26-narrow"

def find_model(conf):
    if isinstance(conf, ConfigType):
        conf = conf.value
    if conf.startswith("res"):
        return SpeechResModel
    else:
        return SpeechModel

def find_config(conf):
    if isinstance(conf, ConfigType):
        conf = conf.value
    return _configs[conf]

def truncated_normal(tensor, std_dev=0.01):
    tensor.zero_()
    tensor.normal_(std=std_dev)
    while torch.sum(torch.abs(tensor) > 2 * std_dev) > 0:
        t = tensor[torch.abs(tensor) > 2 * std_dev]
        t.zero_()
        tensor[torch.abs(tensor) > 2 * std_dev] = torch.normal(t, std=std_dev)

class SerializableModule(nn.Module):
    def __init__(self):
        super().__init__()

    def save(self, filename):
        torch.save(self.state_dict(), filename)

    def load(self, filename):
        self.load_state_dict(torch.load(filename, map_location=lambda storage, loc: storage))


class ScalingLayer(nn.Module):
    def __init__(self, n_features):
        super().__init__()
        self.scale = nn.Parameter(torch.ones(n_features))
        self.register_buffer("drop_indices", None)
        self.register_buffer("keep_indices", None)
        self.frozen = False

    def prune(self, percentage, freeze=False):
        indices = self.scale.abs().sort(descending=False)[1]
        self.drop_indices = indices[:int((percentage / 100) * self.scale.size(0))]
        self.keep_indices = indices[int((percentage / 100) * self.scale.size(0)):]
        self.frozen = freeze
        if freeze:
            self.scale.data = self.scale.data[self.keep_indices]

    def forward(self, x):
        x = self.scale.unsqueeze(0).unsqueeze(-1).unsqueeze(-1).expand_as(x) * x
        if self.drop_indices is not None and not self.frozen:
            x[:, self.drop_indices] *= 0
        return x



class SpeechResModel(SerializableModule):
    def __init__(self, config):
        super().__init__()
        n_labels = config["n_labels"]
        n_maps = config["n_feature_maps"]
        self.conv0 = nn.Conv2d(1, n_maps, (3, 3), padding=(1, 1), bias=False)
        if "res_pool" in config:
            self.pool = nn.AvgPool2d(config["res_pool"])

        self.slimming_lambda = config["slimming_lambda"]
        self.n_layers = n_layers = config["n_layers"]
        dilation = config["use_dilation"]
        if dilation:
            self.convs = [nn.Conv2d(n_maps, n_maps, (3, 3), padding=int(2**(i // 3)), dilation=int(2**(i // 3)),
                                    bias=False) for i in range(n_layers)]
        else:
            self.convs = [nn.Conv2d(n_maps, n_maps, (3, 3), padding=1, dilation=1,
                                    bias=False) for _ in range(n_layers)]
        for i, conv in enumerate(self.convs):
            self.add_module("bn{}".format(i + 1), nn.BatchNorm2d(n_maps, affine=False))
            if i % 2 == 0:
                self.add_module("scale{}".format(i + 1), ScalingLayer(n_maps))
            self.add_module("conv{}".format(i + 1), conv)
        self.output = nn.Linear(n_maps, n_labels)
        self.frozen = False

    def prune(self, percentage, freeze=False):
        for m in self.modules():
            if isinstance(m, ScalingLayer):
                m.prune(percentage, freeze=freeze)
        if freeze:
            self.frozen = True
            for i in range(len(self.convs) - 1):
                if i % 2 == 0:
                    bn = getattr(self, "bn{}".format(i + 1))
                    scale = getattr(self, "scale{}".format(i + 1))
                    conv = getattr(self, "conv{}".format(i + 1))
                    conv.weight.data = conv.weight.data[scale.keep_indices]
                    conv = getattr(self, "conv{}".format(i + 2))
                    conv.weight.data = conv.weight.data[:, scale.keep_indices]
                    bn.running_mean = bn.running_mean[scale.keep_indices]
                    bn.running_var = bn.running_var[scale.keep_indices]

    def regularization(self):
        if not self.slimming_lambda:
            return 0
        reg = 0
        for m in self.modules():
            if isinstance(m, ScalingLayer):
                reg += self.slimming_lambda * m.scale.norm(p=1)
        return reg

    def forward(self, x):
        x = x.unsqueeze(1)
        for i in range(self.n_layers + 1):
            y = F.relu(getattr(self, "conv{}".format(i))(x))
            if i == 0:
                if hasattr(self, "pool"):
                    y = self.pool(y)
                old_x = y
            if i > 0 and i % 2 == 0:
                x = y + old_x
                old_x = x
            else:
                x = y
            if i > 0:
                x = getattr(self, "bn{}".format(i))(x)
                if i % 2 == 1:
                    x = getattr(self, "scale{}".format(i))(x)
        x = x.view(x.size(0), x.size(1), -1) # shape: (batch, feats, o3)
        x = torch.mean(x, 2)
        return self.output(x)

class SpeechModel(SerializableModule):
    def __init__(self, config):
        super().__init__()
        n_labels = config["n_labels"]
        n_featmaps1 = config["n_feature_maps1"]

        conv1_size = config["conv1_size"] # (time, frequency)
        conv1_pool = config["conv1_pool"]
        conv1_stride = tuple(config["conv1_stride"])
        dropout_prob = config["dropout_prob"]
        width = config["width"]
        height = config["height"]
        self.conv1 = nn.Conv2d(1, n_featmaps1, conv1_size, stride=conv1_stride)
        tf_variant = config.get("tf_variant")
        self.tf_variant = tf_variant
        if tf_variant:
            truncated_normal(self.conv1.weight.data)
            self.conv1.bias.data.zero_()
        self.pool1 = nn.MaxPool2d(conv1_pool)

        x = Variable(torch.zeros(1, 1, height, width), volatile=True)
        x = self.pool1(self.conv1(x))
        conv_net_size = x.view(1, -1).size(1)
        last_size = conv_net_size

        if "conv2_size" in config:
            conv2_size = config["conv2_size"]
            conv2_pool = config["conv2_pool"]
            conv2_stride = tuple(config["conv2_stride"])
            n_featmaps2 = config["n_feature_maps2"]
            self.conv2 = nn.Conv2d(n_featmaps1, n_featmaps2, conv2_size, stride=conv2_stride)
            if tf_variant:
                truncated_normal(self.conv2.weight.data)
                self.conv2.bias.data.zero_()
            self.pool2 = nn.MaxPool2d(conv2_pool)
            x = self.pool2(self.conv2(x))
            conv_net_size = x.view(1, -1).size(1)
            last_size = conv_net_size
        if not tf_variant:
            self.lin = nn.Linear(conv_net_size, 32)

        if "dnn1_size" in config:
            dnn1_size = config["dnn1_size"]
            last_size = dnn1_size
            if tf_variant:
                self.dnn1 = nn.Linear(conv_net_size, dnn1_size)
                truncated_normal(self.dnn1.weight.data)
                self.dnn1.bias.data.zero_()
            else:
                self.dnn1 = nn.Linear(32, dnn1_size)
            if "dnn2_size" in config:
                dnn2_size = config["dnn2_size"]
                last_size = dnn2_size
                self.dnn2 = nn.Linear(dnn1_size, dnn2_size)
                if tf_variant:
                    truncated_normal(self.dnn2.weight.data)
                    self.dnn2.bias.data.zero_()
        self.output = nn.Linear(last_size, n_labels)
        if tf_variant:
            truncated_normal(self.output.weight.data)
            self.output.bias.data.zero_()
        self.dropout = nn.Dropout(dropout_prob)

    def forward(self, x):
        x = F.relu(self.conv1(x.unsqueeze(1))) # shape: (batch, channels, i1, o1)
        x = self.dropout(x)
        x = self.pool1(x)
        if hasattr(self, "conv2"):
            x = F.relu(self.conv2(x)) # shape: (batch, o1, i2, o2)
            x = self.dropout(x)
            x = self.pool2(x)
        x = x.view(x.size(0), -1) # shape: (batch, o3)
        if hasattr(self, "lin"):
            x = self.lin(x)
        if hasattr(self, "dnn1"):
            x = self.dnn1(x)
            if not self.tf_variant:
                x = F.relu(x)
            x = self.dropout(x)        
        if hasattr(self, "dnn2"):
            x = self.dnn2(x)
            x = self.dropout(x)
        return self.output(x)

class DatasetType(Enum):
    TRAIN = 0
    DEV = 1
    TEST = 2

class SpeechDataset(data.Dataset):
    LABEL_SILENCE = "__silence__"
    LABEL_UNKNOWN = "__unknown__"
    def __init__(self, data, set_type, config):
        super().__init__()
        self.audio_files = list(data.keys())
        self.set_type = set_type
        self.audio_labels = list(data.values())
        config["bg_noise_files"] = list(filter(lambda x: x.endswith("wav"), config.get("bg_noise_files", [])))
        self.bg_noise_audio = [librosa.core.load(file, sr=16000)[0] for file in config["bg_noise_files"]]
        self.unknown_prob = config["unknown_prob"]
        self.silence_prob = config["silence_prob"]
        self.noise_prob = config["noise_prob"]
        self.n_dct = config["n_dct_filters"]
        self.input_length = config["input_length"]
        self.timeshift_ms = config["timeshift_ms"]
        self.filters = librosa.filters.dct(config["n_dct_filters"], config["n_mels"])
        self.n_mels = config["n_mels"]
        self._audio_cache = SimpleCache(config["cache_size"])
        self._file_cache = SimpleCache(config["cache_size"])
        n_unk = len(list(filter(lambda x: x == 1, self.audio_labels)))
        self.n_silence = int(self.silence_prob * (len(self.audio_labels) - n_unk))

    @staticmethod
    def default_config():
        config = {}
        config["group_speakers_by_id"] = True
        config["silence_prob"] = 0.1
        config["noise_prob"] = 0.8
        config["n_dct_filters"] = 40
        config["input_length"] = 16000
        config["n_mels"] = 40
        config["timeshift_ms"] = 100
        config["unknown_prob"] = 0.1
        config["train_pct"] = 80
        config["dev_pct"] = 10
        config["test_pct"] = 10
        config["network_slimming"] = False
        config["slimming_lambda"] = 0.
        config["prune_pct"] = 0.
        config["wanted_words"] = ["command", "random"]
        config["data_folder"] = ["./training_data"]
        config["pos_key_size"] = 1000
        return config

    def _timeshift_audio(self, data):
        shift = (16000 * self.timeshift_ms) // 1000
        shift = random.randint(-shift, shift)
        start_index = -min(0, shift)
        end_index = max(0, shift)
        data = np.pad(data, (start_index, end_index), "constant")
        return data[:len(data) - start_index] if start_index else data[end_index:]

    def preprocess(self, example, silence=False):
        if silence:
            example = "__silence__"
        if random.random() < 0.7:
            try:
                return self._audio_cache[example]
            except KeyError:
                pass
        in_len = self.input_length
        if self.bg_noise_audio:
            bg_noise = random.choice(self.bg_noise_audio)
            start_index = random.randint(0, len(bg_noise) - in_len - 1)
            bg_noise = bg_noise[start_index:start_index + in_len]
        else:
            bg_noise = np.zeros(in_len)

        if silence:
            data = np.zeros(in_len, dtype=np.float32)
        else:
            file_data = self._file_cache.get(example)
            try:
                data = librosa.core.load(example, sr=16000)[0] if file_data is None else file_data
            except Exception as exception:
                print('failed to load file_- ', example)
                print(exception)
            self._file_cache[example] = data
        data = np.pad(data, (0, max(0, in_len - len(data))), "constant")
        if self.set_type == DatasetType.TRAIN:
            data = self._timeshift_audio(data)

        if random.random() < self.noise_prob or silence:
            a = random.random() * 0.1
            data = np.clip(a * bg_noise + data, -1, 1)
        data = torch.from_numpy(preprocess_audio(data, self.n_mels, self.filters))
        self._audio_cache[example] = data
        return data

    @classmethod
    def splits(cls, config):
        folders = config["data_folder"]
        wanted_words = config["wanted_words"]
        unknown_prob = config["unknown_prob"]
        dev_pct = config["dev_pct"]
        test_pct = config["test_pct"]

        words = {word: i + 2 for i, word in enumerate(wanted_words)}
        words.update({cls.LABEL_SILENCE:0, cls.LABEL_UNKNOWN:1})
        sets = [{}, {}, {}]
        unknowns = [0] * 3
        bg_noise_files = []
        unknown_files = []

        # pos_key_size = {}
        # for word in words:
        #     pos_key_size[word] = 0

        # collect list of folders for each keyword
        keyword_folder_mapping = {}
        for folder in folders:
            for keyword_folder in os.listdir(folder):
                path_name = os.path.join(folder, keyword_folder)

                if os.path.isfile(path_name):
                    continue
                if keyword_folder in keyword_folder_mapping:
                    keyword_folder_mapping[keyword_folder].append(folder)
                else:
                    keyword_folder_mapping[keyword_folder] = [folder]

        for keyword, folder_arr in keyword_folder_mapping.items():

            is_bg_noise = False
            if keyword in words:
                label = words[keyword]
            elif keyword == "_background_noise_":
                is_bg_noise = True
            else:
                label = words[cls.LABEL_UNKNOWN]

            file_list = []

            for folder in folder_arr:
                file_list.extend([os.path.join(folder, keyword, wav_file) for wav_file in os.listdir(os.path.join(folder, keyword))])

            size = config["pos_key_size"]
            if keyword in words:
                prev = len(file_list)
                if len(file_list) >= size:
                    file_list = random.sample(file_list, config["pos_key_size"])
                else:
                    # not enough data to make up pos_key_size.
                    # randomly select from the existing data to fill up pos_key_size
                    data_size = len(file_list)
                    missing_count = size - data_size

                    while missing_count > data_size and missing_count > 0:
                        file_list.extend(file_list)
                        data_size = len(file_list)
                        missing_count = size - data_size

                    randomly_selected = random.sample(file_list, missing_count)
                    file_list.extend(randomly_selected)

                print(keyword, "\t\toriginal data size :", prev, ",\tadjusted data size - ", len(file_list))

            for wav_name in file_list:
                if is_bg_noise and os.path.isfile(wav_name):
                    bg_noise_files.append(wav_name)
                    continue
                elif label == words[cls.LABEL_UNKNOWN]:
                    unknown_files.append(wav_name)
                    continue
                if config["group_speakers_by_id"]:
                    filename = wav_name.split("/")[-1]
                    hashname = re.sub(r"_nohash_.*$", "", filename)
                max_no_wavs = 2**27 - 1
                bucket = int(hashlib.sha1(hashname.encode()).hexdigest(), 16)
                bucket = (bucket % (max_no_wavs + 1)) * (100. / max_no_wavs)
                if bucket < dev_pct:
                    tag = DatasetType.DEV
                elif bucket < test_pct + dev_pct:
                    tag = DatasetType.TEST
                else:
                    tag = DatasetType.TRAIN
                sets[tag.value][wav_name] = label

        for tag, _ in enumerate(sets):
            unknowns[tag] = int(unknown_prob * len(sets[tag]))

        random.shuffle(unknown_files)
        start_index = 0
        for i, dataset in enumerate(sets):
            end_index = start_index + unknowns[i]
            unk_dict = {u: words[cls.LABEL_UNKNOWN] for u in unknown_files[start_index:end_index]}
            dataset.update(unk_dict)
            start_index = end_index

        train_cfg = ChainMap(dict(bg_noise_files=bg_noise_files), config)
        test_cfg = ChainMap(dict(bg_noise_files=bg_noise_files, noise_prob=0), config)
        datasets = (cls(sets[0], DatasetType.TRAIN, train_cfg),
                    cls(sets[1], DatasetType.DEV, test_cfg),
                    cls(sets[2], DatasetType.TEST, test_cfg))
        return datasets

    def __getitem__(self, index):
        if index >= len(self.audio_labels):
            return self.preprocess(None, silence=True), 0
        return self.preprocess(self.audio_files[index]), self.audio_labels[index]

    def __len__(self):
        return len(self.audio_labels) + self.n_silence

_configs = {
    ConfigType.CNN_TRAD_POOL2.value: dict(dropout_prob=0.5, height=101, width=40, n_labels=4, n_feature_maps1=64,
        n_feature_maps2=64, conv1_size=(20, 8), conv2_size=(10, 4), conv1_pool=(2, 2), conv1_stride=(1, 1),
        conv2_stride=(1, 1), conv2_pool=(1, 1), tf_variant=True),
    ConfigType.CNN_ONE_STRIDE1.value: dict(dropout_prob=0.5, height=101, width=40, n_labels=4, n_feature_maps1=186,
        conv1_size=(101, 8), conv1_pool=(1, 1), conv1_stride=(1, 1), dnn1_size=128, dnn2_size=128, tf_variant=True),
    ConfigType.CNN_TSTRIDE2.value: dict(dropout_prob=0.5, height=101, width=40, n_labels=4, n_feature_maps1=78,
        n_feature_maps2=78, conv1_size=(16, 8), conv2_size=(9, 4), conv1_pool=(1, 3), conv1_stride=(2, 1),
        conv2_stride=(1, 1), conv2_pool=(1, 1), dnn1_size=128, dnn2_size=128),
    ConfigType.CNN_TSTRIDE4.value: dict(dropout_prob=0.5, height=101, width=40, n_labels=4, n_feature_maps1=100,
        n_feature_maps2=78, conv1_size=(16, 8), conv2_size=(5, 4), conv1_pool=(1, 3), conv1_stride=(4, 1),
        conv2_stride=(1, 1), conv2_pool=(1, 1), dnn1_size=128, dnn2_size=128),
    ConfigType.CNN_TSTRIDE8.value: dict(dropout_prob=0.5, height=101, width=40, n_labels=4, n_feature_maps1=126,
        n_feature_maps2=78, conv1_size=(16, 8), conv2_size=(5, 4), conv1_pool=(1, 3), conv1_stride=(8, 1),
        conv2_stride=(1, 1), conv2_pool=(1, 1), dnn1_size=128, dnn2_size=128),
    ConfigType.CNN_TPOOL2.value: dict(dropout_prob=0.5, height=101, width=40, n_labels=4, n_feature_maps1=94,
        n_feature_maps2=94, conv1_size=(21, 8), conv2_size=(6, 4), conv1_pool=(2, 3), conv1_stride=(1, 1),
        conv2_stride=(1, 1), conv2_pool=(1, 1), dnn1_size=128, dnn2_size=128),
    ConfigType.CNN_TPOOL3.value: dict(dropout_prob=0.5, height=101, width=40, n_labels=4, n_feature_maps1=94,
        n_feature_maps2=94, conv1_size=(15, 8), conv2_size=(6, 4), conv1_pool=(3, 3), conv1_stride=(1, 1),
        conv2_stride=(1, 1), conv2_pool=(1, 1), dnn1_size=128, dnn2_size=128),
    ConfigType.CNN_ONE_FPOOL3.value: dict(dropout_prob=0.5, height=101, width=40, n_labels=4, n_feature_maps1=54,
        conv1_size=(101, 8), conv1_pool=(1, 3), conv1_stride=(1, 1), dnn1_size=128, dnn2_size=128),
    ConfigType.CNN_ONE_FSTRIDE4.value: dict(dropout_prob=0.5, height=101, width=40, n_labels=4, n_feature_maps1=186,
        conv1_size=(101, 8), conv1_pool=(1, 1), conv1_stride=(1, 4), dnn1_size=128, dnn2_size=128),
    ConfigType.CNN_ONE_FSTRIDE8.value: dict(dropout_prob=0.5, height=101, width=40, n_labels=4, n_feature_maps1=336,
        conv1_size=(101, 8), conv1_pool=(1, 1), conv1_stride=(1, 8), dnn1_size=128, dnn2_size=128),
    ConfigType.RES15.value: dict(n_labels=12, use_dilation=True, n_layers=13, n_feature_maps=45),
    ConfigType.RES8.value: dict(n_labels=12, n_layers=6, n_feature_maps=45, res_pool=(4, 3), use_dilation=False),
    ConfigType.RES26.value: dict(n_labels=12, n_layers=24, n_feature_maps=45, res_pool=(2, 2), use_dilation=False),
    ConfigType.RES15_NARROW.value: dict(n_labels=12, use_dilation=True, n_layers=13, n_feature_maps=19),
    ConfigType.RES8_NARROW.value: dict(n_labels=12, n_layers=6, n_feature_maps=19, res_pool=(4, 3), use_dilation=False),
    ConfigType.RES26_NARROW.value: dict(n_labels=12, n_layers=24, n_feature_maps=19, res_pool=(2, 2), use_dilation=False)
}
