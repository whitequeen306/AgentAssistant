"""Entry point for AgentAssistant — always launches the Dynamic-Island UI.

Usage:
    agent-assistant              # Launch UI (+ daemon)
    agent-assistant -v           # Verbose console logging
    agent-assistant --right-click <path>   # Send file/folder to a running
                                           # instance, or launch UI with it
"""

from __future__ import annotations

import logging
import sys

from agent_assistant.config import settings

# HTTP client libraries dump full request/response bodies at DEBUG, including
# Authorization headers. Keep them out of agent.log regardless of -v.
_NOISY_LOGGERS = (
    "httpcore",
    "httpcore.connection",
    "httpcore.http11",
    "httpcore.http2",
    "httpx",
    "openai",
    "openai._base_client",
    "urllib3",
)


def quiet_third_party_loggers() -> None:
    """Drop HTTP-stack DEBUG so the file log stays readable and key-free."""
    for name in _NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)


# ANSI colors for terminal / log output
DIM = "\033[2m"
RESET = "\033[0m"
RED = "\033[31m"


def setup_logging(verbose: bool = False) -> None:
    """Configure root logging: console + a persistent file log.

    The file (data_dir/logs/agent.log, append mode, DEBUG level) records
    every run with timestamps — the diagnostic trail for tool-call errors
    and the pre-API tool_calls-pairing validation (see AgentLoop._log_pre_api).
    Idempotent: safe to call from any entry point (main, launch __main__).
    """
    console_level = logging.DEBUG if verbose else logging.WARNING
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)  # let the file handler capture everything

    # Console handler (terminal) — keep the original dim look.
    if not any(getattr(h, "_aa_console", False) for h in root.handlers):
        ch = logging.StreamHandler()
        ch.setLevel(console_level)
        ch.setFormatter(logging.Formatter(
            f"{DIM}%(asctime)s [%(name)s] %(levelname)s: %(message)s{RESET}",
            datefmt="%H:%M:%S",
        ))
        ch._aa_console = True  # type: ignore[attr-defined]
        root.addHandler(ch)

    # File handler — the persistent run log (append, full timestamp, DEBUG).
    if not any(getattr(h, "_aa_file", False) for h in root.handlers):
        settings.ensure_dirs()
        log_path = settings.logs_dir / "agent.log"
        fh = logging.FileHandler(log_path, mode="a", encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(logging.Formatter(
            "%(asctime)s [%(name)s] %(levelname)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        ))
        fh._aa_file = True  # type: ignore[attr-defined]
        root.addHandler(fh)
        logging.getLogger(__name__).info(
            "════════ AgentAssistant run started ════════ model=%s "
            "log_file=%s console_level=%s",
            settings.deepseek_model,
            log_path,
            logging.getLevelName(console_level),
        )
    quiet_third_party_loggers()


def main() -> None:
    """Launch the UI (with daemon). Supports --verbose and --right-click."""
    verbose = "--verbose" in sys.argv or "-v" in sys.argv
    # --ui / --daemon kept as no-op aliases so old launchers keep working.
    setup_logging(verbose)

    # J23: Right-click file/folder handler (forward via IPC or launch UI)
    right_click_path = _parse_right_click_arg()
    if right_click_path:
        _handle_right_click(right_click_path)
        return

    from agent_assistant.ui.launch import launch_ui

    launch_ui(with_daemon=True)


def _parse_right_click_arg() -> str | None:
    """Parse --right-click <path> from argv."""
    for i, arg in enumerate(sys.argv):
        if arg == "--right-click" and i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    return None


def _handle_right_click(file_path: str) -> None:
    """J23 (08 §4.6): Handle right-click 'Send to Assistant' for a file/folder.

    If the assistant is already running, forward the path to it via the
    single-instance IPC and exit. Otherwise launch the UI with the path
    pending — invoke_on_file fires once the frontend has initialized.
    """
    from pathlib import Path

    from agent_assistant.ipc import send_to_running_instance

    path = Path(file_path)
    if not path.exists():
        print(f"{RED}Path not found: {file_path}{RESET}")
        sys.exit(1)

    # Running instance? Hand the path over and exit.
    if send_to_running_instance({"type": "invoke_on_file", "path": str(path)}):
        return

    # No instance — launch the UI (with daemon) and process there.
    from agent_assistant.ui.launch import launch_ui

    launch_ui(with_daemon=True, pending_file=str(path))


if __name__ == "__main__":
    main()
