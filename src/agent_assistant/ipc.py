"""J23: single-instance IPC over localhost TCP (08 §4.6).

The running UI instance listens on a localhost TCP port and records
it in ``<data_dir>/ipc.port``. When the user right-clicks a file while
the assistant is already running, the second ``--right-click`` process
forwards the path via :func:`send_to_running_instance` and exits.
If nothing is listening (missing/stale port file), the caller launches
the UI itself.

Protocol: one newline-terminated JSON message per connection;
the server replies ``{"ok": true}`` as an ack.
"""

from __future__ import annotations

import json
import logging
import socket
import threading
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

_ACK = b'{"ok": true}\n'
_MAX_MESSAGE = 65536


def _default_port_file() -> Path:
    from agent_assistant.config import settings

    return settings.data_dir / "ipc.port"


class IPCServer:
    """Listens on localhost; dispatches parsed JSON messages to a handler."""

    def __init__(
        self,
        handler: Callable[[dict[str, Any]], None],
        port: int | None = None,
        port_file: Path | None = None,
    ) -> None:
        self._handler = handler
        self._port = port if port is not None else 0  # 0 = ephemeral
        self._port_file = port_file or _default_port_file()
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._running = False

    @property
    def port(self) -> int | None:
        """Actual bound port (after start)."""
        return self._sock.getsockname()[1] if self._sock else None

    def start(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", self._port))
        sock.listen(5)
        self._sock = sock
        self._running = True
        # Record the port so a later --right-click process can find us
        self._port_file.parent.mkdir(parents=True, exist_ok=True)
        self._port_file.write_text(str(sock.getsockname()[1]), encoding="utf-8")
        self._thread = threading.Thread(
            target=self._accept_loop, daemon=True, name="ipc-server"
        )
        self._thread.start()
        logger.info("IPC server listening on 127.0.0.1:%s", self.port)

    def _accept_loop(self) -> None:
        while self._running and self._sock:
            try:
                conn, _addr = self._sock.accept()
            except OSError:
                break  # socket closed by stop()
            threading.Thread(
                target=self._serve, args=(conn,), daemon=True, name="ipc-conn"
            ).start()

    def _serve(self, conn: socket.socket) -> None:
        try:
            conn.settimeout(5)
            data = b""
            while not data.endswith(b"\n") and len(data) < _MAX_MESSAGE:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                data += chunk
            msg = json.loads(data.decode("utf-8"))
            conn.sendall(_ACK)
            self._handler(msg)
        except Exception as e:
            logger.warning("IPC message failed: %s", e)
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def stop(self) -> None:
        self._running = False
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
        try:
            self._port_file.unlink(missing_ok=True)
        except OSError:
            pass


def send_to_running_instance(
    message: dict[str, Any],
    port_file: Path | None = None,
    timeout: float = 1.0,
) -> bool:
    """Forward a message to the running instance; True if it acked."""
    port_file = port_file or _default_port_file()
    try:
        port = int(port_file.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return False  # not running (no port file / unreadable)
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout) as sock:
            sock.sendall(json.dumps(message, ensure_ascii=False).encode("utf-8") + b"\n")
            sock.settimeout(timeout)
            reply = sock.recv(1024)
        return bool(json.loads(reply.decode("utf-8")).get("ok"))
    except (OSError, ValueError):
        return False  # stale port file / dead instance / bad reply
