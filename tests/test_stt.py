"""Tests for the sherpa-onnx + SenseVoice STT engine (voice/stt.py).

Mocks the ONNX recognizer (no model file needed) to test the int16->float32
conversion and the transcribe call structure. A real-model smoke test is left
to manual verification once the model is downloaded.
"""

import struct

import numpy as np
import pytest

from agent_assistant.voice import stt


# ─── pcm_int16_to_float32 conversion ────────────────────────────────────────


class TestPcmConversion:
    def test_zero_is_zero(self):
        out = stt.pcm_int16_to_float32(struct.pack("<2h", 0, 0))
        assert list(out) == [0.0, 0.0]

    def test_max_positive_just_under_one(self):
        out = stt.pcm_int16_to_float32(struct.pack("<h", 32767))
        assert abs(out[0] - 32767 / 32768.0) < 1e-6
        assert out[0] < 1.0

    def test_min_negative_at_neg_one(self):
        out = stt.pcm_int16_to_float32(struct.pack("<h", -32768))
        assert abs(out[0] + 1.0) < 1e-4

    def test_empty_bytes(self):
        out = stt.pcm_int16_to_float32(b"")
        assert len(out) == 0

    def test_odd_byte_count_drops_trailing(self):
        # 5 bytes (odd) → drop 1 → 2 int16 samples; no crash
        out = stt.pcm_int16_to_float32(b"\x00\x00\x00\x00\xFF")
        assert len(out) == 2

    def test_output_dtype_range_and_shape(self):
        samples = struct.pack("<6h", 0, 100, -100, 32767, -32768, 12345)
        out = stt.pcm_int16_to_float32(samples)
        assert out.dtype == np.float32
        assert out.shape == (6,)
        assert bool(((-1.0 <= out) & (out <= 1.0)).all())


# ─── STTEngine transcribe (mocked recognizer) ──────────────────────────────


class _FakeResult:
    """Mirrors sherpa-onnx RecognizationResult: an object with a .text attr."""

    def __init__(self, text):
        self.text = text


class _FakeStream:
    def __init__(self):
        self.accepted = None
        self.result = _FakeResult("")

    def accept_waveform(self, sample_rate, audio):
        self.accepted = (sample_rate, audio)


class _FakeRecognizer:
    def __init__(self, result_text="识别结果"):
        self._result = result_text
        self.last_audio = None

    def create_stream(self):
        return _FakeStream()

    def decode_stream(self, stream):
        self.last_audio = stream.accepted
        stream.result = _FakeResult(self._result)


class TestSTTEngine:
    def test_transcribe_bytes_converts_and_returns_result(self, monkeypatch):
        engine = stt.STTEngine(model="fake.onnx", tokens="fake.txt")
        fake = _FakeRecognizer("你好世界")
        monkeypatch.setattr(
            engine, "_ensure_model", lambda: setattr(engine, "_recognizer", fake)
        )
        audio = struct.pack("<4h", 100, -100, 200, -200)  # int16 PCM
        result = engine.transcribe_bytes(audio)
        assert result == "你好世界"
        # recognizer received float32 audio at 16kHz, in [-1, 1]
        sr, samples = fake.last_audio
        assert sr == stt.SAMPLE_RATE
        assert samples.dtype == np.float32
        assert len(samples) == 4
        assert bool(((-1.0 <= samples) & (samples <= 1.0)).all())

    def test_transcribe_bytes_strips_whitespace(self, monkeypatch):
        engine = stt.STTEngine(model="fake.onnx", tokens="fake.txt")
        fake = _FakeRecognizer("  带空格  \n")
        monkeypatch.setattr(
            engine, "_ensure_model", lambda: setattr(engine, "_recognizer", fake)
        )
        result = engine.transcribe_bytes(struct.pack("<h", 0))
        assert result == "带空格"

    def test_ensure_model_raises_without_sherpa(self, monkeypatch):
        import sys

        monkeypatch.setitem(sys.modules, "sherpa_onnx", None)  # import fails
        engine = stt.STTEngine(model="fake.onnx", tokens="fake.txt")
        engine._recognizer = None
        with pytest.raises(RuntimeError, match="sherpa-onnx"):
            engine._ensure_model()

    def test_ensure_model_passes_correct_kwargs(self, monkeypatch):
        # Lock the from_sense_voice call kwargs so a typo (modl=, token=) is caught
        import sys
        import types

        captured = {}

        class _FakeSVRecognizer:
            @classmethod
            def from_sense_voice(cls, **kw):
                captured.update(kw)
                return _FakeRecognizer("ok")

        fake_mod = types.ModuleType("sherpa_onnx")
        fake_mod.OfflineRecognizer = _FakeSVRecognizer
        monkeypatch.setitem(sys.modules, "sherpa_onnx", fake_mod)

        engine = stt.STTEngine(
            model="/m.onnx", tokens="/t.txt", language="zh",
            provider="cuda", num_threads=4,
        )
        engine._ensure_model()
        assert captured["model"] == "/m.onnx"
        assert captured["tokens"] == "/t.txt"
        assert captured["language"] == "zh"
        assert captured["provider"] == "cuda"
        assert captured["num_threads"] == 4
        assert captured["use_itn"] is True


class TestModelResolution:
    """Model path resolution: int8 preferred, full fallback, rglob for nested."""

    def test_prefers_int8_over_full(self, tmp_path):
        (tmp_path / "model.onnx").write_bytes(b"")
        (tmp_path / "model.int8.onnx").write_bytes(b"")
        assert stt.STTEngine._resolve_model_path(tmp_path).endswith("model.int8.onnx")

    def test_falls_back_to_full_model_onnx(self, tmp_path):
        (tmp_path / "model.onnx").write_bytes(b"")
        assert stt.STTEngine._resolve_model_path(tmp_path).endswith("model.onnx")

    def test_default_when_absent(self, tmp_path):
        # GitHub bundle's full model.onnx is the common case; default int8 if none
        assert stt.STTEngine._resolve_model_path(tmp_path).endswith("model.int8.onnx")

    def test_rglobs_nested_onnx(self, tmp_path):
        sub = tmp_path / "sherpa-onnx-sense-voice-2024-07-17"
        sub.mkdir()
        (sub / "model.onnx").write_bytes(b"")
        assert stt.STTEngine._resolve_model_path(tmp_path).endswith("model.onnx")

    def test_tokens_at_base(self, tmp_path):
        (tmp_path / "tokens.txt").write_text("a 0", encoding="utf-8")
        assert stt.STTEngine._resolve_tokens_path(tmp_path).endswith("tokens.txt")

    def test_tokens_rglobs_nested(self, tmp_path):
        sub = tmp_path / "sub"
        sub.mkdir()
        (sub / "tokens.txt").write_text("a 0", encoding="utf-8")
        assert stt.STTEngine._resolve_tokens_path(tmp_path).endswith("tokens.txt")


@pytest.mark.skip(reason="needs SenseVoice model downloaded; run manually after setup")
def test_real_model_smoke():
    # Requires model at data_dir/models/sense-voice/{model.int8.onnx,tokens.txt}
    engine = stt.STTEngine()
    audio = b"\x00\x00" * stt.SAMPLE_RATE  # 1s of silence
    result = engine.transcribe_bytes(audio)
    assert isinstance(result, str)
