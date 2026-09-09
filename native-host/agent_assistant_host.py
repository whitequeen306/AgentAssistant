#!/usr/bin/env python3
"""Chrome/Edge native messaging host for AgentAssistant (J1, 08 §4.5).

Edge spawns this host when the extension calls
chrome.runtime.sendNativeMessage('com.agent_assistant.host', {text: ...}).
The host reads the selected text and forwards it to the running assistant
via the single-instance IPC (ipc.py). If no instance is running, it writes
the text to pending_text.json and launches the UI, which consumes it on init.

Native messaging protocol: 4-byte little-endian length prefix + UTF-8 JSON
over stdin/stdout. Nothing else may go to stdout (it corrupts the protocol);
errors go to stderr, which Edge ignores.
"""
import json
import struct
import sys
from pathlib import Path

# Make agent_assistant importable when Edge spawns this script directly
# (covers both pip-installed and source-checkout cases).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

_MAX_LEN = 10 * 1024 * 1024


def _read_exact(n: int) -> bytes:
    """Read exactly n bytes from stdin, looping past short reads; b"" on EOF."""
    buf = b""
    while len(buf) < n:
        chunk = sys.stdin.buffer.read(n - len(buf))
        if not chunk:
            break  # EOF before n bytes
        buf += chunk
    return buf


def _read_message() -> dict:
    try:
        raw_len = _read_exact(4)
        if len(raw_len) < 4:
            return {}
        msg_len = struct.unpack("<I", raw_len)[0]
        if msg_len == 0 or msg_len > _MAX_LEN:
            return {}
        data = _read_exact(msg_len).decode("utf-8")
        return json.loads(data) if data else {}
    except Exception as e:
        sys.stderr.write(f"host: read failed: {e}\n")
        return {}


def _send_message(msg: dict) -> None:
    data = json.dumps(msg, ensure_ascii=False).encode("utf-8")
    sys.stdout.buffer.write(struct.pack("<I", len(data)))
    sys.stdout.buffer.write(data)
    sys.stdout.buffer.flush()


def _forward(text: str) -> bool:
    """Send {type: invoke_on_text, text} to the running instance; True if acked."""
    try:
        from agent_assistant.ipc import send_to_running_instance
    except Exception as e:
        sys.stderr.write(f"host: import ipc failed: {e}\n")
        return False
    return send_to_running_instance({"type": "invoke_on_text", "text": text})


def _write_pending_and_launch(text: str) -> bool:
    """No running instance — write pending_text.json + launch the UI.

    The UI subprocess is detached from the host's stdout/stderr pipes so it
    cannot corrupt the native-messaging protocol (main.py prints to stdout).
    """
    try:
        from agent_assistant.ui.bridge import write_pending_text
        write_pending_text(text)
    except Exception as e:
        sys.stderr.write(f"host: write pending failed: {e}\n")
        return False
    import subprocess
    try:
        subprocess.Popen(
            [sys.executable, "-m", "agent_assistant.main"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
        return True
    except Exception as e:
        sys.stderr.write(f"host: launch failed: {e}\n")
        return False


def main() -> None:
    msg = _read_message()
    text = (msg.get("text") or "").strip()
    if not text:
        _send_message({"ok": False, "error": "no text"})
        return
    try:
        from agent_assistant.ui.bridge import TEXT_INVOKE_CAP
    except Exception:
        TEXT_INVOKE_CAP = 8192
    if len(text) > TEXT_INVOKE_CAP:
        text = text[:TEXT_INVOKE_CAP] + "\n…[已截断]"
    if _forward(text):
        _send_message({"ok": True, "error": ""})
    elif _write_pending_and_launch(text):
        _send_message({"ok": True, "launched": True, "error": ""})
    else:
        _send_message({"ok": False, "error": "forward and launch failed"})


if __name__ == "__main__":
    main()
