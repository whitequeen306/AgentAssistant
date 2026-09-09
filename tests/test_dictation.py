"""Tests for the streaming dictation processor (voice/dictation.py).

DictationProcessor decouples the partial-dedup + endpoint->final+reset logic
from sounddevice and the real sherpa-onnx recognizer, so it can be exercised
with a scripted fake recognizer (no model, no mic).
"""
import numpy as np
import pytest

from agent_assistant.voice.dictation import DictationProcessor


class _FakeStream:
    def __init__(self):
        self.accepted = []
        self.finished = False

    def accept_waveform(self, sample_rate, samples):
        self.accepted.append((sample_rate, samples))

    def input_finished(self):
        self.finished = True


class _FakeRecognizer:
    """Scripted recognizer: is_ready/get_result/is_endpoint pop from queues."""

    def __init__(self, ready_seq, results, endpoints):
        self._ready = list(ready_seq)
        self._results = list(results)
        self._endpoints = list(endpoints)
        self.created = 0
        self.resets = 0
        self.stream = None

    def create_stream(self):
        self.created += 1
        self.stream = _FakeStream()
        return self.stream

    def is_ready(self, s):
        return self._ready.pop(0) if self._ready else False

    def decode_stream(self, s):
        pass

    def get_result(self, s):
        return self._results.pop(0) if self._results else ""

    def is_endpoint(self, s):
        return self._endpoints.pop(0) if self._endpoints else False

    def reset(self, s):
        self.resets += 1


def _log_emit():
    log = []
    return log, (lambda kind, text: log.append((kind, text)))


SILENCE = np.zeros(1600, dtype=np.float32)


class TestDictationProcessor:
    def test_emits_partial_only_when_text_changes(self):
        r = _FakeRecognizer(
            ready_seq=[True, False, True, False],
            results=["你", "你好"],
            endpoints=[False, False],
        )
        log, emit = _log_emit()
        p = DictationProcessor(r, emit)
        p.feed(SILENCE)
        p.feed(SILENCE)
        assert log == [("partial", "你"), ("partial", "你好")]

    def test_no_partial_when_text_unchanged(self):
        r = _FakeRecognizer(
            ready_seq=[True, False, True, False],
            results=["你好", "你好"],
            endpoints=[False, False],
        )
        log, emit = _log_emit()
        p = DictationProcessor(r, emit)
        p.feed(SILENCE)
        p.feed(SILENCE)
        assert log == [("partial", "你好")]

    def test_endpoint_emits_final_and_resets(self):
        r = _FakeRecognizer(
            ready_seq=[True, False, True, False],
            results=["你好", "你好"],
            endpoints=[False, True],
        )
        log, emit = _log_emit()
        p = DictationProcessor(r, emit)
        p.feed(SILENCE)
        p.feed(SILENCE)
        assert log == [("partial", "你好"), ("final", "你好")]
        assert r.resets == 1

    def test_endpoint_with_empty_last_emits_no_final_but_still_resets(self):
        # Silence produced no text (last=""); endpoint fires → no final, but reset
        r = _FakeRecognizer(
            ready_seq=[True, False],
            results=[""],
            endpoints=[True],
        )
        log, emit = _log_emit()
        p = DictationProcessor(r, emit)
        p.feed(SILENCE)
        assert log == []
        assert r.resets == 1

    def test_finish_flushes_trailing_partial_as_final(self):
        # A partial was shown but no endpoint; finish() flushes it as a final.
        r = _FakeRecognizer(
            ready_seq=[True, False, True, False],
            results=["你好", "你好"],
            endpoints=[False, False],
        )
        log, emit = _log_emit()
        p = DictationProcessor(r, emit)
        p.feed(SILENCE)
        p.finish()
        assert ("final", "你好") in log
        assert r.stream.finished is True

    def test_finish_no_emission_when_empty(self):
        r = _FakeRecognizer(
            ready_seq=[True, False, True, False],
            results=["", ""],
            endpoints=[False, False],
        )
        log, emit = _log_emit()
        p = DictationProcessor(r, emit)
        p.feed(SILENCE)
        p.finish()
        assert log == []

    def test_feed_passes_sample_rate_and_samples_to_stream(self):
        r = _FakeRecognizer(
            ready_seq=[True, False], results=["x"], endpoints=[False]
        )
        log, emit = _log_emit()
        p = DictationProcessor(r, emit, sample_rate=16000)
        chunk = np.full(100, 0.5, dtype=np.float32)
        p.feed(chunk)
        sr, samples = r.stream.accepted[0]
        assert sr == 16000
        assert samples is chunk

    def test_reset_after_endpoint_starts_new_utterance(self):
        # After endpoint+reset, a new partial on the next feed is emitted afresh
        r = _FakeRecognizer(
            ready_seq=[True, False, True, False, True, False],
            results=["第一句", "第一句", "第二句"],
            endpoints=[False, True, False],
        )
        log, emit = _log_emit()
        p = DictationProcessor(r, emit)
        p.feed(SILENCE)  # partial "第一句"
        p.feed(SILENCE)  # endpoint → final "第一句" + reset
        p.feed(SILENCE)  # new utterance partial "第二句"
        assert log == [
            ("partial", "第一句"),
            ("final", "第一句"),
            ("partial", "第二句"),
        ]
        assert r.resets == 1
