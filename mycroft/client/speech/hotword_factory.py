# Copyright 2017 Mycroft AI Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
import tempfile
import time

import os
from os.path import dirname, exists, join, abspath

from subprocess import Popen, PIPE
from threading import Thread

from mycroft.configuration import ConfigurationManager
from mycroft.util.log import LOG
import numpy as np


RECOGNIZER_DIR = join(abspath(dirname(__file__)), "recognizer")


class HotWordEngine(object):
    def __init__(self, key_phrase="hey mycroft", config=None, lang="en-us"):
        self.lang = str(lang).lower()
        self.key_phrase = str(key_phrase).lower()
        # rough estimate 1 phoneme per 2 chars
        self.num_phonemes = len(key_phrase) / 2 + 1
        if config is None:
            config = ConfigurationManager.get().get("hot_words", {})
            config = config.get(self.key_phrase, {})
        self.config = config
        self.listener_config = ConfigurationManager.get().get("listener", {})

    def found_wake_word(self, frame_data):
        return False


class PocketsphinxHotWord(HotWordEngine):
    def __init__(self, key_phrase="hey mycroft", config=None, lang="en-us"):
        super(PocketsphinxHotWord, self).__init__(key_phrase, config, lang)
        # Hotword module imports
        from pocketsphinx import Decoder
        # Hotword module config
        module = self.config.get("module")
        if module != "pocketsphinx":
            LOG.warning(
                str(module) + " module does not match with "
                              "Hotword class pocketsphinx")
        # Hotword module params
        self.phonemes = self.config.get("phonemes", "HH EY . M AY K R AO F T")
        self.num_phonemes = len(self.phonemes.split())
        self.threshold = self.config.get("threshold", 1e-90)
        self.sample_rate = self.listener_config.get("sample_rate", 1600)
        dict_name = self.create_dict(self.key_phrase, self.phonemes)
        config = self.create_config(dict_name, Decoder.default_config())
        self.decoder = Decoder(config)

    def create_dict(self, key_phrase, phonemes):
        (fd, file_name) = tempfile.mkstemp()
        words = key_phrase.split()
        phoneme_groups = phonemes.split('.')
        with os.fdopen(fd, 'w') as f:
            for word, phoneme in zip(words, phoneme_groups):
                f.write(word + ' ' + phoneme + '\n')
        return file_name

    def create_config(self, dict_name, config):
        model_file = join(RECOGNIZER_DIR, 'model', self.lang, 'hmm')
        if not exists(model_file):
            LOG.error('PocketSphinx model not found at ' + str(model_file))
        config.set_string('-hmm', model_file)
        config.set_string('-dict', dict_name)
        config.set_string('-keyphrase', self.key_phrase)
        config.set_float('-kws_threshold', float(self.threshold))
        config.set_float('-samprate', self.sample_rate)
        config.set_int('-nfft', 2048)
        config.set_string('-logfn', '/dev/null')
        return config

    def transcribe(self, byte_data, metrics=None):
        start = time.time()
        self.decoder.start_utt()
        self.decoder.process_raw(byte_data, False, False)
        self.decoder.end_utt()
        if metrics:
            metrics.timer("mycroft.stt.local.time_s", time.time() - start)
        return self.decoder.hyp()

    def found_wake_word(self, frame_data):
        hyp = self.transcribe(frame_data)
        return hyp and self.key_phrase in hyp.hypstr.lower()


class PreciseHotword(HotWordEngine):
    def __init__(self, key_phrase="hey mycroft", config=None, lang="en-us"):
        super(PreciseHotword, self).__init__(key_phrase, config, lang)

        folder = join(RECOGNIZER_DIR, 'model', self.lang)
        exe_name = join(folder, 'precise_stream')
        model_name = join(folder, 'keyword.pb')
        self.chunk_size = 1024
        self.proc = Popen([exe_name, model_name, str(self.chunk_size)], stdin=PIPE, stdout=PIPE)
        self.has_found = False
        self.cooldown = 20
        t = Thread(target=self.check_stdout)
        t.daemon = True
        t.start()

    def check_stdout(self):
        while True:
            line = self.proc.stdout.readline()
            LOG.debug('Precise feedback: ' + str(round(float(line))))
            if self.cooldown > 0:
                self.cooldown -= 1
                self.has_found = False
                LOG.debug('COOLDOWN')
                continue
            if float(line) > 0.5:
                self.has_found = True
            else:
                self.has_found = False

    def update(self, chunk):
        self.proc.stdin.write(chunk)
        self.proc.stdin.flush()

    def found_wake_word(self, raw_data):
        if self.has_found and self.cooldown == 0:
            self.cooldown = 20
            return True
        return False


class SnowboyHotWord(HotWordEngine):
    def __init__(self, key_phrase="hey mycroft", config=None, lang="en-us"):
        super(SnowboyHotWord, self).__init__(key_phrase, config, lang)
        # Hotword module imports
        from snowboydecoder import HotwordDetector
        # Hotword module config
        module = self.config.get("module")
        if module != "snowboy":
            LOG.warning(module + " module does not match with Hotword class "
                                 "snowboy")
        # Hotword params
        models = self.config.get("models", {})
        paths = []
        for key in models:
            paths.append(models[key])
        sensitivity = self.config.get("sensitivity", 0.5)
        self.snowboy = HotwordDetector(paths,
                                       sensitivity=[sensitivity] * len(paths))
        self.lang = str(lang).lower()
        self.key_phrase = str(key_phrase).lower()

    def found_wake_word(self, frame_data):
        wake_word = self.snowboy.detector.RunDetection(frame_data)
        return wake_word == 1


class HotWordFactory(object):
    CLASSES = {
        "pocketsphinx": PocketsphinxHotWord,
        "precise": PreciseHotword,
        "snowboy": SnowboyHotWord
    }

    @staticmethod
    def create_hotword(hotword="hey mycroft", config=None, lang="en-us"):
        LOG.info("creating " + hotword)
        if not config:
            config = ConfigurationManager.get().get("hotwords", {})
        module = config.get(hotword).get("module", "pocketsphinx")
        config = config.get(hotword, {"module": module})
        clazz = HotWordFactory.CLASSES.get(module)
        try:
            return clazz(hotword, config, lang=lang)
        except (KeyboardInterrupt, SystemExit):
            raise
        except:
            LOG.exception('Could not create hotword. Falling back to default.')
            return HotWordFactory.CLASSES['pocketsphinx']()
