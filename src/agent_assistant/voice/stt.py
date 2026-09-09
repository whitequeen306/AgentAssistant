"""Speech-to-Text via sherpa-onnx + SenseVoice-Small (local, Chinese-strong, free).

Replaces faster-whisper. Alibaba's SenseVoice model runs on the sherpa-onnx
ONNX runtime (CPU), strong on Mandarin + en/ja/ko/yue. Audio is captured as
int16 PCM (sounddevice) and converted to the float32 [-1, 1] samples that
SenseVoice expects before decoding.

Install:  pip install sherpa-onnx sounddevice numpy
Model:    download SenseVoice-Small int8 ONNX (model.int8.onnx + tokens.txt)
          to data_dir/models/sense-voice/ — see docs. No cloud, no key.
"""
from __future__ import annotations

import logging
import wave
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

SAMPLE_RATE = 16000
CHANNELS = 1
SAMPLE_WIDTH = 2  # 16-bit


def pcm_int16_to_float32(audio_bytes: bytes) -> Any:
    """Convert raw int16 PCM bytes -> float32 numpy array in [-1, 1] (SenseVoice format).

    An odd trailing byte (shouldn't happen with int16 capture, but defensive)
    is dropped with a warning rather than crashing numpy.frombuffer.
    """
    import numpy as np

    if len(audio_bytes) % 2 != 0:
        logger.warning(
            "odd-length PCM buffer (%d bytes); dropping trailing byte", len(audio_bytes)
        )
        audio_bytes = audio_bytes[:-1]
    arr = np.frombuffer(audio_bytes, dtype=np.int16)
    return (arr.astype(np.float32) / 32768.0).reshape(-1)


def _default_model_dir() -> Path:
    from agent_assistant.config import settings

    return settings.data_dir / "models" / "sense-voice"


class STTEngine:
    """SenseVoice ASR via sherpa-onnx. Lazy-loads the ONNX model on first use."""

    def __init__(
        self,
        model: str | None = None,
        tokens: str | None = None,
        language: str = "auto",
        use_itn: bool = True,
        provider: str = "cpu",
        num_threads: int = 1,
    ) -> None:
        from agent_assistant.config import settings

        base = _default_model_dir()
        self._model = model or settings.sense_voice_model or self._resolve_model_path(base)
        self._tokens = tokens or settings.sense_voice_tokens or self._resolve_tokens_path(base)
        self._language = language
        self._use_itn = use_itn
        self._provider = provider
        self._num_threads = num_threads
        self._recognizer = None

    @staticmethod
    def _resolve_model_path(base: Path) -> str:
        """Find the SenseVoice ONNX under base: prefer int8 (smaller/faster),
        then full model.onnx (the GitHub release bundle ships this), then any
        *.onnx via rglob (covers a nested extraction dir). Falls back to the
        int8 default so a missing model fails clearly at load, not at resolve."""
        for name in ("model.int8.onnx", "model.onnx"):
            p = base / name
            if p.is_file():
                return str(p)
        onnx = sorted(base.rglob("*.onnx"), key=lambda p: len(p.parts))
        if onnx:
            return str(onnx[0])
        return str(base / "model.int8.onnx")

    @staticmethod
    def _resolve_tokens_path(base: Path) -> str:
        """Find tokens.txt under base (expected location, else rglob)."""
        p = base / "tokens.txt"
        if p.is_file():
            return str(p)
        found = list(base.rglob("tokens.txt"))
        if found:
            return str(found[0])
        return str(base / "tokens.txt")

    def _ensure_model(self) -> None:
        """Lazy-load the SenseVoice recognizer."""
        if self._recognizer is not None:
            return
        try:
            import sherpa_onnx
        except ImportError:
            raise RuntimeError(
                "sherpa-onnx not installed. Install with: pip install sherpa-onnx"
            )
        logger.info("Loading SenseVoice: %s", self._model)
        self._recognizer = sherpa_onnx.OfflineRecognizer.from_sense_voice(
            model=self._model,
            tokens=self._tokens,
            language=self._language,
            use_itn=self._use_itn,
            provider=self._provider,
            num_threads=self._num_threads,
        )
        logger.info("SenseVoice model loaded")

    def transcribe_bytes(self, audio_bytes: bytes) -> str:
        """Transcribe raw int16 PCM audio bytes (16kHz, mono) -> text."""
        self._ensure_model()
        audio = pcm_int16_to_float32(audio_bytes)
        stream = self._recognizer.create_stream()
        stream.accept_waveform(SAMPLE_RATE, audio)
        self._recognizer.decode_stream(stream)
        # stream.result is a RecognizationResult object with .text (sherpa-onnx
        # 1.13+); getattr fallback covers an older string-result API.
        text = (getattr(stream.result, "text", stream.result) or "").strip()
        logger.info("STT result: %s", text[:100])
        return text

    def transcribe_file(self, audio_path: str | Path) -> str:
        """Transcribe a 16kHz mono 16-bit WAV file -> text."""
        with wave.open(str(audio_path), "rb") as wf:
            if wf.getnchannels() != CHANNELS:
                raise ValueError(
                    f"audio must be mono ({CHANNELS}ch), got {wf.getnchannels()}ch"
                )
            if wf.getsampwidth() != SAMPLE_WIDTH:
                raise ValueError(
                    f"audio must be 16-bit (sampwidth {SAMPLE_WIDTH}), got {wf.getsampwidth()}"
                )
            if wf.getframerate() != SAMPLE_RATE:
                logger.warning(
                    "audio %dHz != %dHz; quality will degrade — resample to 16kHz",
                    wf.getframerate(), SAMPLE_RATE,
                )
            frames = wf.readframes(wf.getnframes())
        return self.transcribe_bytes(frames)


def record_audio(duration: float) -> bytes:
    """Record `duration` seconds of audio from the microphone.

    Returns raw int16 PCM bytes (16kHz, mono). Requires sounddevice + numpy.
    """
    try:
        import sounddevice as sd  # type: ignore[import-untyped]

        audio = sd.rec(
            int(duration * SAMPLE_RATE),
            samplerate=SAMPLE_RATE,
            channels=CHANNELS,
            dtype="int16",
        )
        sd.wait()
        return audio.tobytes()
    except ImportError:
        raise RuntimeError(
            "sounddevice not installed. Install with: pip install sounddevice numpy"
        )


class PushToTalkRecorder:
    """Records audio while a flag is active (push-to-talk)."""

    def __init__(self) -> None:
        self._recording = False
        self._frames: list[bytes] = []
        self._stream = None

    def start(self) -> None:
        """Start recording."""
        import sounddevice as sd

        self._frames = []
        self._recording = True
        self._stream = sd.RawInputStream(
            samplerate=SAMPLE_RATE,
            channels=CHANNELS,
            dtype="int16",
            callback=self._audio_callback,
        )
        self._stream.start()
        logger.debug("PTT recording started")

    def stop(self) -> bytes:
        """Stop recording and return audio bytes."""
        self._recording = False
        if self._stream:
            self._stream.stop()
            self._stream.close()
            self._stream = None
        audio = b"".join(self._frames)
        logger.debug("PTT recording stopped: %d bytes", len(audio))
        return audio

    def _audio_callback(self, indata: Any, frames: int, time_info: Any, status: Any) -> None:
        if self._recording:
            self._frames.append(bytes(indata))


# Singleton
stt_engine = STTEngine()
ptt_recorder = PushToTalkRecorder()
