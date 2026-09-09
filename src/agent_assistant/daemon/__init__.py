"""Background daemon sub-package.

The daemon layer runs lightweight triggers that do NOT depend on an always-on LLM.
When a trigger fires, it passes a snapshot/event to wake the main agent.

Components:
- Perf anomaly monitor (continuous; over-threshold → wake agent)
- Morning briefing (after-login delay + network-ready → dispatch_research)
- Right-click menu registration (system-level hook)
- Voice hotkey listener (Phase 9)
"""
