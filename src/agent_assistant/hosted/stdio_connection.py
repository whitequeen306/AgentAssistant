"""Server-side stdio JSON-RPC 2.0 transport (newline-delimited).

Handles the MCP wire protocol for hosted mode:
- writes responses / notifications / server→client requests to stdout (locked)
- correlates server→client requests (e.g. ``elicitation/create``) with their
  responses that arrive later on stdin

stdout is the protocol channel — nothing else may write to it. All logging
goes to stderr / the file log (configured in ``mcp_server.main``).
"""

from __future__ import annotations

import json
import logging
import threading
from typing import Any, Callable

logger = logging.getLogger(__name__)

# Server→client request ids live in a high range so they never collide with
# the client's own request ids (initialize=1, tools/list=2, ...).
_SERVER_REQUEST_ID_BASE = 1_000_000


class _PendingRequest:
    __slots__ = ("event", "result", "error")

    def __init__(self) -> None:
        self.event = threading.Event()
        self.result: Any = None
        self.error: Any = None


class StdioConnection:
    """Thread-safe writer + server-request correlator over stdio."""

    def __init__(self, write_line: Callable[[str], None]) -> None:
        self._write_line = write_line
        self._write_lock = threading.Lock()
        self._id_lock = threading.Lock()
        self._next_server_id = _SERVER_REQUEST_ID_BASE
        self._pending: dict[int, _PendingRequest] = {}

    def _send(self, message: dict[str, Any]) -> None:
        payload = json.dumps(message, ensure_ascii=False)
        with self._write_lock:
            self._write_line(payload)

    def respond(self, request_id: Any, result: dict[str, Any]) -> None:
        self._send({"jsonrpc": "2.0", "id": request_id, "result": result})

    def respond_error(self, request_id: Any, code: int, message: str) -> None:
        self._send({
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": code, "message": message},
        })

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def send_progress(self, message: str, *, progress_token: Any = None) -> None:
        """Emit an MCP progress notification (best-effort, never raises)."""
        params: dict[str, Any] = {"message": message}
        if progress_token is not None:
            params["progressToken"] = progress_token
        try:
            self.notify("notifications/progress", params)
        except Exception:  # progress is best-effort; must not break a task
            logger.debug("send_progress failed", exc_info=True)

    def server_request(
        self, method: str, params: dict[str, Any], timeout: float
    ) -> dict[str, Any] | None:
        """Send a server→client request and block until the response or timeout.

        Returns the response ``result`` dict, or ``None`` on timeout/error.
        Safe to call from worker threads (tool-execution threads).
        """
        with self._id_lock:
            request_id = self._next_server_id
            self._next_server_id += 1
        pending = _PendingRequest()
        self._pending[request_id] = pending
        try:
            self._send({
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
                "params": params,
            })
            if not pending.event.wait(timeout):
                logger.warning("server_request timeout: %s (id=%s)", method, request_id)
                return None
            if pending.error is not None:
                logger.warning("server_request error: %s -> %s", method, pending.error)
                return None
            return pending.result if isinstance(pending.result, dict) else {}
        finally:
            self._pending.pop(request_id, None)

    def is_pending(self, request_id: Any) -> bool:
        return request_id in self._pending

    def resolve_response(self, request_id: Any, result: Any, error: Any) -> None:
        """Route a stdin response message back to the blocked server_request."""
        pending = self._pending.get(request_id)
        if pending is None:
            logger.debug("response for unknown/expired server request: %s", request_id)
            return
        pending.result = result
        pending.error = error
        pending.event.set()

    def fail_all_pending(self) -> None:
        """Unblock every waiting server_request (shutdown/stdin closed)."""
        for pending in list(self._pending.values()):
            pending.error = {"message": "connection closed"}
            pending.event.set()
