"""Voice pipeline sub-package.

Components:
- STT: sherpa-onnx + SenseVoice-Small (local, Chinese-strong, zero cost)
- TTS: Edge-TTS (free, good quality, Chinese neural voices)
- Hotkey: push-to-talk via keyboard library

Voice is the agent's "ears and mouth" — the agent internally only handles text.
STT/TTS are outer pipelines, not part of routing decisions.
"""
