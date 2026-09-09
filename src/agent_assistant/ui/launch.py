"""UI launch entry point — starts the multi-page Dynamic-Island UI.

Usage:
    python -m agent_assistant.ui.launch
    agent-assistant
"""

from __future__ import annotations

import logging
import sys

logger = logging.getLogger(__name__)


def launch_ui(with_daemon: bool = False, pending_file: str | None = None) -> None:
    """Launch the Dynamic-Island UI with full agent backend.

    Args:
        with_daemon: J5 — start background daemon (perf, briefing, hotkey)
                     alongside the UI. ``main()`` always passes True.
        pending_file: J23 — right-click path to invoke once the UI loads
                      (set by main.py --right-click when no instance runs).
    """
    from agent_assistant.agent.loop import AgentEvent
    from agent_assistant.agent.pool import AgentPool
    from agent_assistant.config import apply_reasoning_effort, settings
    from agent_assistant.ipc import IPCServer
    from agent_assistant.tools.register import register_all_tools
    from agent_assistant.ui.bridge import api_bridge
    from agent_assistant.ui.store import ui_store
    from agent_assistant.ui.window import ui_window

    # Validate config
    if not settings.deepseek_api_key:
        print("Error: DEEPSEEK_API_KEY not set. Create a .env file.")
        sys.exit(1)

    # Initialize backend
    settings.ensure_dirs()
    register_all_tools()

    # Wire UI confirm gate (save_note / kill_process / dangerous ops).
    from agent_assistant.tools.confirm import UIConfirmProvider, confirm_gate

    confirm_gate.provider = UIConfirmProvider(timeout=120.0)

    # Event forwarding to UI — one callback shared by every per-conv loop.
    def on_agent_event(event: AgentEvent) -> None:
        # J22: Forward ALL event types to UI (was only tool_call/tool_result)
        if api_bridge.report_silent:
            # Background reports must not stream into the current view.
            return
        identity = {
            "conversation_id": event.conversation_id,
            "parent_turn_id": event.parent_turn_id,
        }
        if event.type == "thinking":
            api_bridge.push_thinking(**identity)
        elif event.type == "thinking_chunk":
            api_bridge.push_thinking_chunk(event.content or "", **identity)
        elif event.type == "tool_call":
            api_bridge.push_tool_call(
                event.content["name"],
                event.content["arguments"],
                **identity,
            )
        elif event.type == "tool_result":
            result = event.content["result"]
            api_bridge.push_tool_result(
                event.content["name"],
                result.get("ok", False),
                result.get("data"),
                **identity,
            )
        elif event.type == "response_chunk":
            # J12: Stream tokens to UI
            api_bridge.push_response_chunk(event.content, **identity)
        elif event.type == "response":
            api_bridge.push_response(event.content, **identity)
        elif event.type == "error":
            api_bridge.push_event(
                "error",
                {"message": str(event.content), **identity},
            )
        elif event.type == "status":
            api_bridge.push_event(
                "status",
                {"text": str(event.content or ""), **identity},
            )

    # One AgentLoop per conversation (isolated _messages + short-term memory).
    # Perf reports use the dedicated 「性能检测」 conv's loop via report_perf.
    agent_pool = AgentPool(on_event=on_agent_event)
    api_bridge.set_agent_pool(agent_pool)

    # Subagent manager: push persisted task events to the frontend and mark
    # any tasks from a previous session as interrupted (no auto-resume).
    from agent_assistant.subagents import service as subagent_service

    def on_subagent_event(event) -> None:
        data = event.to_dict()
        # _push_event merges {"type": <outer>, **data}; the task event's own
        # "type" must not overwrite the envelope type.
        data["event_type"] = data.pop("type")
        api_bridge.push_event("subagent_event", data)

    subagent_service.set_event_sink(on_subagent_event)
    api_bridge.set_subagent_manager(subagent_service.get_manager())
    subagent_service.recover_on_startup()

    # Wire bridge → window resize (JS view switch drives window size)
    api_bridge.set_state_handler(ui_window.set_state)
    # §5.6: drag-to-edge snap → notify frontend to switch views
    ui_window.on_edge_snap = lambda state, is_snap=False: api_bridge.push_event(
        "state", {"state": state, "is_snap": is_snap}
    )
    # §5.5: apply persisted settings at startup
    ui_window.set_edge_snap_enabled(ui_store.get_setting("edge_snap") == "on")
    stored_effort = ui_store.get_setting("reasoning_effort")
    if stored_effort:
        apply_reasoning_effort(stored_effort)

    # J5: Start daemon alongside UI
    if with_daemon:
        from agent_assistant.daemon.runner import DaemonRunner

        daemon = DaemonRunner(bridge=api_bridge)
        daemon.start()
        logger.info("Daemon started alongside UI")

    # J23: single-instance IPC — later --right-click processes forward here
    def on_ipc_message(msg: dict) -> None:
        if msg.get("type") == "invoke_on_file" and msg.get("path"):
            api_bridge.invoke_on_file(msg["path"])
        elif msg.get("type") == "invoke_on_text" and msg.get("text"):
            api_bridge.invoke_on_text(msg["text"])

    ipc_server = IPCServer(on_ipc_message)
    try:
        ipc_server.start()
    except OSError as e:
        logger.warning("IPC server unavailable: %s", e)

    # J23: file passed at startup — injected after the frontend inits
    if pending_file:
        api_bridge.set_pending_file(pending_file)

    # §5.1: default startup state = main (Settings can change it)
    startup_state = ui_store.get_setting("startup_state", "main")
    if startup_state in ("minimized", "docked-sliver"):
        startup_state = "docked-search"
    if startup_state not in ("main", "docked-search", "popup-chat"):
        startup_state = "main"

    # Start UI (blocking)
    logger.info("Launching Dynamic-Island UI (state=%s)...", startup_state)
    try:
        ui_window.start(initial_state=startup_state)
    finally:
        ipc_server.stop()


if __name__ == "__main__":
    from agent_assistant.main import setup_logging

    setup_logging()
    launch_ui()
