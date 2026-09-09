"""Text-to-Speech via Edge-TTS (free, good quality, Chinese neural voices).

Converts agent text responses to speech and plays them.
J19: Sentence-level streaming — first sentence plays while later ones synthesize,
     reducing perceived latency from full-text to single-sentence.
"""

from __future__ import annotations

import asyncio
import logging
import re
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

# Default voice: Chinese female neural voice
DEFAULT_VOICE = "zh-CN-XiaoxiaoNeural"
# Fallback English voice
EN_VOICE = "en-US-AriaNeural"

# Sentence split pattern (CJK + Western punctuation)
_SENTENCE_RE = re.compile(
    r'(?<=[。！？.!?\n])\s*'
)


class TTSEngine:
    """Text-to-speech engine using Edge-TTS.

    J19: Splits text into sentences and plays sequentially.
    First sentence starts playing while later ones synthesize.
    """

    def __init__(self, voice: str | None = None) -> None:
        from agent_assistant.config import settings
        self._voice = voice or settings.tts_voice or DEFAULT_VOICE
        self._playing = False

    def speak(self, text: str, voice: str | None = None, blocking: bool = False) -> None:
        """Convert text to speech and play it.

        J21: Default is non-blocking (background thread).
        Set blocking=True only when caller needs to wait (e.g. tests).
        """
        if not text.strip():
            return

        voice = voice or self._pick_voice(text)

        if not blocking:
            self.speak_async(text, voice)
            return

        try:
            asyncio.run(self._speak_async(text, voice))
        except RuntimeError:
            # If already in an event loop, create a new one
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as pool:
                pool.submit(asyncio.run, self._speak_async(text, voice)).result()

    def speak_async(self, text: str, voice: str | None = None) -> None:
        """Speak in a background thread (non-blocking)."""
        import threading

        voice = voice or self._pick_voice(text)
        thread = threading.Thread(
            target=lambda: asyncio.run(self._speak_async(text, voice)),
            daemon=True,
            name="tts-playback",
        )
        thread.start()

    def stop(self) -> None:
        """Stop current playback."""
        self._playing = False

    def _pick_voice(self, text: str) -> str:
        """Pick voice based on text language."""
        # Simple heuristic: if mostly CJK characters, use Chinese voice
        cjk_count = sum(1 for c in text if "\u4e00" <= c <= "\u9fff")
        if cjk_count > len(text) * 0.3:
            return DEFAULT_VOICE
        return EN_VOICE

    async def _speak_async(self, text: str, voice: str) -> None:
        """Internal async TTS + playback.

        J19: Split into sentences, synthesize and play each sequentially.
        First sentence plays while later ones are still synthesizing.
        """
        try:
            import edge_tts  # type: ignore[import-untyped]

            self._playing = True

            # J19: Split into sentences for streaming playback
            sentences = self._split_sentences(text)

            for sentence in sentences:
                if not self._playing:
                    break
                if not sentence.strip():
                    continue

                # Generate audio for this sentence
                with tempfile.NamedTemporaryFile(
                    suffix=".mp3", delete=False
                ) as f:
                    temp_path = f.name

                try:
                    communicate = edge_tts.Communicate(sentence, voice)
                    await communicate.save(temp_path)

                    if not self._playing:
                        break

                    self._play_audio(temp_path)
                finally:
                    Path(temp_path).unlink(missing_ok=True)

        except ImportError:
            logger.error(
                "edge-tts not installed. Install with: pip install edge-tts"
            )
        except Exception as e:
            logger.error("TTS failed: %s", e)
        finally:
            self._playing = False

    @staticmethod
    def _split_sentences(text: str) -> list[str]:
        """J19: Split text into sentences for streaming playback.

        Keeps chunks short enough for low-latency first-byte playback.
        """
        parts = _SENTENCE_RE.split(text)
        # Merge very short fragments (< 10 chars) with next sentence
        sentences: list[str] = []
        buffer = ""
        for part in parts:
            buffer += part
            if len(buffer.strip()) >= 10:
                sentences.append(buffer.strip())
                buffer = ""
        if buffer.strip():
            sentences.append(buffer.strip())
        # If no splits happened, return whole text
        return sentences if sentences else [text]

    @staticmethod
    def _play_audio(path: str) -> None:
        """Play an audio file (mp3/wav). Uses Windows MCI for mp3 support."""
        import subprocess
        import sys

        if sys.platform == "win32":
            # J2 fix: SoundPlayer only supports .wav — use MCI for .mp3
            if path.lower().endswith(".wav"):
                try:
                    subprocess.run(
                        [
                            "powershell", "-NoProfile", "-Command",
                            f"(New-Object Media.SoundPlayer '{path}').PlaySync()",
                        ],
                        capture_output=True,
                        timeout=60,
                    )
                    return
                except Exception:
                    pass

            # MCI supports mp3 natively on Windows
            try:
                import ctypes

                mci = ctypes.windll.winmm.mciSendStringW  # type: ignore[attr-defined]
                alias = "tts_audio"
                mci(f'open "{path}" type mpegvideo alias {alias}', None, 0, None)
                mci(f"play {alias} wait", None, 0, None)
                mci(f"close {alias}", None, 0, None)
                return
            except Exception:
                pass

            # Fallback: try mpv/vlc
            try:
                subprocess.run(
                    ["mpv", "--no-video", path], capture_output=True, timeout=60
                )
            except FileNotFoundError:
                logger.warning("No audio player available for TTS playback")
        else:
            import subprocess as sp

            sp.run(["mpv", "--no-video", path], capture_output=True, timeout=60)


# Singleton
tts_engine = TTSEngine()
