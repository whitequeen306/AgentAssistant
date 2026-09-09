"""Streaming dictation via sherpa-onnx Zipformer (online) — live partial text.

DictationProcessor decouples the partial-dedup + endpoint->final+reset loop
from the mic and the real recognizer, so it is unit-testable with a fake.
StreamingDictationSession (added next) wires it to sounddevice capture + the
real Zipformer OnlineRecognizer. Mirrors LianYu deploy/asr/server.py:307-371.

Models: the streaming-zipformer-bilingual-zh-en transducer bundle
(encoder/decoder/joiner int8 + tokens.txt) under data_dir/models/zipformer-zh/.
"""
from __future__ import annotations

import logging
import queue
import threading
from pathlib import Path
from typing import Any, Callable

from agent_assistant.voice.stt import pcm_int16_to_float32

logger = logging.getLogger(__name__)

SAMPLE_RATE = 16000


class DictationProcessor:
    """Partial-dedup + endpoint->final+reset loop, decoupled from the recognizer.

    ``recognizer`` must expose: ``create_stream()``, ``is_ready(s)``,
    ``decode_stream(s)``, ``get_result(s)``, ``is_endpoint(s)``, ``reset(s)``.
    The stream must expose ``accept_waveform(sr, samples)`` and
    ``input_finished()``. ``emit(kind, text)`` is called with kind in
    ``{"partial", "final"}`` — partial on every change, final on utterance end.
    """

    def __init__(
        self,
        recognizer: Any,
        emit: Callable[[str, str], None],
        sample_rate: int = SAMPLE_RATE,
    ) -> None:
        self._r = recognizer
        self._emit = emit
        self._sr = sample_rate
        self._stream = recognizer.create_stream()
        self._last = ""

    def feed(self, samples: Any) -> None:
        """Feed a float32 [-1,1] chunk; emit partial on change, final on endpoint."""
        self._stream.accept_waveform(self._sr, samples)
        while self._r.is_ready(self._stream):
            self._r.decode_stream(self._stream)
        text = (self._r.get_result(self._stream) or "").strip()
        if text and text != self._last:
            self._last = text
            self._emit("partial", text)
        if self._r.is_endpoint(self._stream):
            if self._last:
                self._emit("final", self._last)
            self._r.reset(self._stream)
            self._last = ""

    def finish(self) -> None:
        """Tail flush: input_finished + final decode -> emit any trailing text.

        Called when the user clicks stop, so the last partial (not yet committed
        by an endpoint) is still emitted as a final. Mirrors LianYu's flush path.
        """
        self._stream.input_finished()
        while self._r.is_ready(self._stream):
            self._r.decode_stream(self._stream)
        text = (self._r.get_result(self._stream) or "").strip()
        final = text or self._last
        if final:
            self._emit("final", final)
        self._last = ""


# ─── Model resolution + streaming session (integration: sounddevice + sherpa) ─

def _default_zipformer_dir() -> Path:
    from agent_assistant.config import settings
    return settings.data_dir / "models" / "zipformer-zh"


def _resolve_file(base: Path, names: tuple[str, ...], glob: str) -> str:
    """Prefer named files (int8 first), then a glob fallback under base."""
    for n in names:
        p = base / n
        if p.is_file():
            return str(p)
    found = sorted(base.rglob(glob), key=lambda p: ("int8" not in p.name, len(p.parts)))
    if found:
        return str(found[0])
    return str(base / names[0])  # default; fails clearly at load if absent


def _resolve_tokens(base: Path) -> str:
    p = base / "tokens.txt"
    if p.is_file():
        return str(p)
    found = list(base.rglob("tokens.txt"))
    return str(found[0]) if found else str(p)


_recognizer_cache: dict[tuple[str, int], Any] = {}


def load_streaming_recognizer(base: Path | None = None, num_threads: int = 2):
    """Load the Zipformer OnlineRecognizer (cached at module scope; mirrors LianYu _load_online)."""
    import sherpa_onnx

    base = Path(base or _default_zipformer_dir())
    key = (str(base), num_threads)
    cached = _recognizer_cache.get(key)
    if cached is not None:
        return cached
    r = sherpa_onnx.OnlineRecognizer.from_transducer(
        tokens=_resolve_tokens(base),
        encoder=_resolve_file(base, ("encoder-epoch-99-avg-1.int8.onnx", "encoder-epoch-99-avg-1.onnx"), "encoder*.onnx"),
        decoder=_resolve_file(base, ("decoder-epoch-99-avg-1.int8.onnx", "decoder-epoch-99-avg-1.onnx"), "decoder*.onnx"),
        joiner=_resolve_file(base, ("joiner-epoch-99-avg-1.int8.onnx", "joiner-epoch-99-avg-1.onnx"), "joiner*.onnx"),
        num_threads=num_threads,
        sample_rate=SAMPLE_RATE,
        feature_dim=80,
        decoding_method="greedy_search",
        enable_endpoint_detection=True,
        rule1_min_trailing_silence=2.4,
        rule2_min_trailing_silence=1.2,
        rule3_min_utterance_length=20,
        provider="cpu",
    )
    _recognizer_cache[key] = r
    return r


class StreamingDictationSession:
    """Live mic dictation: sounddevice int16 capture -> DictationProcessor -> events.

    A daemon worker drains the audio queue and feeds the processor; the
    sounddevice callback just enqueues frames (never blocks the audio thread).
    on_event(kind, text) receives "partial"/"final".
    """

    def __init__(self, on_event, model_dir: Path | None = None, sample_rate: int = SAMPLE_RATE) -> None:
        self._on_event = on_event
        self._sr = sample_rate
        self._model_dir = Path(model_dir or _default_zipformer_dir())
        self._recognizer = None
        self._processor = None
        self._sd_stream = None
        self._worker = None
        self._stop = threading.Event()
        # Bounded ~5s of 100ms frames; drops new frames if the UI thread stalls
        # so memory can't grow unbounded (LianYu caps max_session_bytes).
        self._q: queue.Queue = queue.Queue(maxsize=50)

    def start(self) -> None:
        self._recognizer = load_streaming_recognizer(self._model_dir)
        self._processor = DictationProcessor(self._recognizer, self._emit, self._sr)
        self._stop.clear()
        self._worker = threading.Thread(target=self._run, daemon=True, name="dictation")
        self._worker.start()
        try:
            import sounddevice as sd
            self._sd_stream = sd.RawInputStream(
                samplerate=self._sr,
                channels=1,
                dtype="int16",
                blocksize=int(0.1 * self._sr),  # 100ms chunks
                callback=self._callback,
            )
            self._sd_stream.start()
        except Exception:
            # Mic init failed (no device / PortAudio error / device locked).
            # Tear down the already-started worker so it doesn't block on the
            # queue forever (zombie thread), then re-raise.
            self.stop()
            raise
        logger.info("streaming dictation started")

    def stop(self) -> None:
        self._stop.set()
        if self._sd_stream:
            try:
                self._sd_stream.stop()
                self._sd_stream.close()
            except Exception:
                pass
            self._sd_stream = None
        # Non-blocking sentinel: if the worker is stuck and the queue is full,
        # don't block here forever — the join(timeout) below catches a stuck worker.
        try:
            self._q.put(None, timeout=1)
        except queue.Full:
            logger.warning("dictation queue full at stop; worker may be stuck")
        if self._worker:
            self._worker.join(timeout=5)
            if self._worker.is_alive():
                logger.warning("dictation worker did not stop in 5s (leaked daemon)")
            self._worker = None
        logger.info("streaming dictation stopped")

    def _callback(self, indata, frames, time_info, status) -> None:
        if self._stop.is_set():
            return
        try:
            self._q.put_nowait(bytes(indata))
        except queue.Full:
            pass  # UI thread stalled; drop this frame to bound memory (latency resumes on recovery)

    def _run(self) -> None:
        while not self._stop.is_set():
            chunk = self._q.get()
            if chunk is None:
                break
            samples = pcm_int16_to_float32(chunk)
            if samples.size:
                try:
                    self._processor.feed(samples)
                except Exception as e:
                    logger.warning("dictation feed failed; stopping worker: %s", e)
                    break  # stream likely poisoned — don't cascade into a bad state
        try:
            self._processor.finish()
        except Exception as e:
            logger.warning("dictation finish failed: %s", e)

    def _emit(self, kind: str, text: str) -> None:
        try:
            self._on_event(kind, text)
        except Exception as e:
            logger.warning("dictation emit failed: %s", e)
