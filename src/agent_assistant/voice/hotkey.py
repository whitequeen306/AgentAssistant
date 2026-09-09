"""Push-to-talk hotkey listener.

Listens for a global hotkey (default: Ctrl+Shift+Space).
- Press → start recording
- Release → stop recording → STT → emit VOICE_COMMAND event

G1 fix: Track full combo state — only stop when a combo key releases
         WHILE the combo was active (not on random space presses).
J13 fix: Push voice_state events to UI bridge for visual feedback.

Per docs/04-features.md §4.9:
- Push-to-talk: simple, not tiring, lowest latency
- Voice is the agent's "ears" — STT converts to text, agent only sees text
"""

from __future__ import annotations

import logging
import threading

from agent_assistant.daemon.events import EventType, TriggerEvent, event_bus

logger = logging.getLogger(__name__)

# Default hotkey for push-to-talk
DEFAULT_HOTKEY = "ctrl+shift+space"

# Keys that compose the hotkey (for release tracking)
_COMBO_KEYS = {"ctrl", "left ctrl", "right ctrl",
               "shift", "left shift", "right shift", "space"}


class HotkeyListener:
    """Global hotkey listener for push-to-talk voice input.

    G1: Uses combo-state tracking to avoid false stops from typing space.
    J13: Pushes voice_state to UI bridge for Dynamic Island feedback.
    """

    def __init__(self, hotkey: str = DEFAULT_HOTKEY) -> None:
        self._hotkey = hotkey
        self._running = False
        self._recording = False
        self._combo_active = False  # G1: True only when full combo engaged
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Start listening for the hotkey."""
        if self._running:
            return

        try:
            import keyboard

            self._running = True
            keyboard.add_hotkey(self._hotkey, self._on_press, suppress=True)
            keyboard.on_release(self._on_release_check)
            logger.info("Hotkey listener started: %s", self._hotkey)
        except ImportError:
            logger.error("keyboard library not installed. Install with: pip install keyboard")
        except Exception as e:
            logger.error("Failed to start hotkey listener: %s", e)

    def stop(self) -> None:
        """Stop listening."""
        self._running = False
        try:
            import keyboard
            keyboard.unhook_all()
        except Exception:
            pass
        logger.info("Hotkey listener stopped")

    def _on_press(self) -> None:
        """Called when hotkey is pressed — start recording."""
        if self._recording:
            return
        self._recording = True
        self._combo_active = True  # G1: mark combo engaged
        logger.debug("PTT: recording started")

        # J13: Push listening state to UI
        self._push_voice_state("listening")

        try:
            from agent_assistant.voice.stt import ptt_recorder
            ptt_recorder.start()
        except Exception as e:
            logger.error("Failed to start recording: %s", e)
            self._recording = False
            self._combo_active = False
            self._push_voice_state("idle")

    def _on_release_check(self, event) -> None:
        """Called on any key release — check if combo broke.

        G1 fix: Only stop recording when a combo key releases AND the
        combo was actually engaged. Prevents typing-space from stopping.
        """
        if not self._recording:
            return

        # G1: Only react to combo keys, and only if combo was engaged
        key_name = getattr(event, "name", "")
        if key_name in _COMBO_KEYS and self._combo_active:
            self._combo_active = False
            self._stop_and_process()

    def _stop_and_process(self) -> None:
        """Stop recording and process the audio."""
        self._recording = False
        logger.debug("PTT: recording stopped, processing...")

        # J13: Push idle state back to UI
        self._push_voice_state("idle")

        # Process in background thread to not block the hotkey listener
        thread = threading.Thread(
            target=self._process_audio, daemon=True, name="ptt-process"
        )
        thread.start()

    def _process_audio(self) -> None:
        """STT the recorded audio and emit event."""
        try:
            from agent_assistant.voice.stt import ptt_recorder, stt_engine

            audio_bytes = ptt_recorder.stop()

            if len(audio_bytes) < 1600:  # Less than 0.05s of audio
                logger.debug("PTT: audio too short, ignoring")
                return

            text = stt_engine.transcribe_bytes(audio_bytes)

            if text.strip():
                logger.info("PTT result: %s", text[:100])
                event_bus.emit(TriggerEvent(
                    type=EventType.VOICE_COMMAND,
                    payload={"text": text, "method": "push_to_talk"},
                    source="hotkey_listener",
                ))
            else:
                logger.debug("PTT: no speech detected")

        except Exception as e:
            logger.error("PTT processing failed: %s", e)

    @staticmethod
    def _push_voice_state(state: str) -> None:
        """J13: Push voice state to UI bridge for Dynamic Island feedback."""
        try:
            from agent_assistant.ui.bridge import api_bridge
            api_bridge.push_voice_state(state)
        except Exception:
            pass  # UI not running — ignore


# Singleton
hotkey_listener = HotkeyListener()
