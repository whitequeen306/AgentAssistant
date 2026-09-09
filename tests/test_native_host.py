"""Tests for the native messaging host (native-host/agent_assistant_host.py).

The host sits at the untrusted boundary (Edge feeds it attacker-controlled
length prefixes + JSON). These tests cover the stdio protocol (read_exact
looping, short reads, oversize/zero/invalid), the ack writer, and the main
dispatch (empty / forward-ok / forward-fail-launch / both-fail / truncation).
"""

import importlib.util
import io
import json
import struct
import sys
from pathlib import Path

import pytest

_HOST_PATH = Path(__file__).resolve().parent.parent / "native-host" / "agent_assistant_host.py"


def _load_host():
    spec = importlib.util.spec_from_file_location("aa_native_host", _HOST_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _FakeStream:
    """Stand-in for sys.stdin/sys.stdout exposing a .buffer BytesIO."""

    def __init__(self, data=b""):
        self.buffer = io.BytesIO(data)


def _pack(msg: dict) -> bytes:
    data = json.dumps(msg).encode("utf-8")
    return struct.pack("<I", len(data)) + data


def _decode_reply(out: _FakeStream) -> dict:
    written = out.buffer.getvalue()
    ln = struct.unpack("<I", written[:4])[0]
    return json.loads(written[4:4 + ln])


@pytest.fixture
def host():
    return _load_host()


# ─── _read_message (stdio protocol) ─────────────────────────────────────────


class TestReadMessage:
    def test_reads_valid_message(self, host, monkeypatch):
        monkeypatch.setattr(sys, "stdin", _FakeStream(_pack({"text": "hello"})))
        assert host._read_message() == {"text": "hello"}

    def test_empty_stdin_returns_empty(self, host, monkeypatch):
        monkeypatch.setattr(sys, "stdin", _FakeStream(b""))
        assert host._read_message() == {}

    def test_short_length_prefix_returns_empty(self, host, monkeypatch):
        # Only 3 bytes where 4-byte length is expected → EOF mid-prefix
        monkeypatch.setattr(sys, "stdin", _FakeStream(b"\x01\x02\x03"))
        assert host._read_message() == {}

    def test_zero_length_returns_empty(self, host, monkeypatch):
        monkeypatch.setattr(sys, "stdin", _FakeStream(struct.pack("<I", 0)))
        assert host._read_message() == {}

    def test_oversize_returns_empty(self, host, monkeypatch):
        monkeypatch.setattr(
            sys, "stdin", _FakeStream(struct.pack("<I", 11 * 1024 * 1024))
        )
        assert host._read_message() == {}

    def test_invalid_json_returns_empty(self, host, monkeypatch):
        bad = struct.pack("<I", 5) + b"xxxxx"
        monkeypatch.setattr(sys, "stdin", _FakeStream(bad))
        assert host._read_message() == {}

    def test_short_read_assembles_full_message(self, host, monkeypatch):
        # Payload delivered across several partial reads — _read_exact must loop.
        # A real short read returns 1..n bytes (fewer than requested); this sim
        # caps each read at 2 bytes regardless of n requested.
        data = _pack({"text": "hi"})

        class _Chunky:
            def __init__(self, raw, cap=2):
                self._buf = io.BytesIO(raw)
                self._cap = cap

            def read(self, n):
                return self._buf.read(min(n, self._cap))

        fake = _FakeStream()
        fake.buffer = _Chunky(data)
        monkeypatch.setattr(sys, "stdin", fake)
        assert host._read_message() == {"text": "hi"}


# ─── _send_message ──────────────────────────────────────────────────────────


class TestSendMessage:
    def test_writes_length_prefixed_json(self, host, monkeypatch):
        out = _FakeStream()
        monkeypatch.setattr(sys, "stdout", out)
        host._send_message({"ok": True, "error": ""})
        reply = _decode_reply(out)
        assert reply == {"ok": True, "error": ""}


# ─── main dispatch ───────────────────────────────────────────────────────────


class TestMain:
    def test_no_text_replies_error(self, host, monkeypatch):
        monkeypatch.setattr(sys, "stdin", _FakeStream(_pack({})))
        out = _FakeStream()
        monkeypatch.setattr(sys, "stdout", out)
        monkeypatch.setattr(host, "_forward", lambda t: True)
        host.main()
        assert _decode_reply(out) == {"ok": False, "error": "no text"}

    def test_forward_ok_replies_ok(self, host, monkeypatch):
        monkeypatch.setattr(sys, "stdin", _FakeStream(_pack({"text": "hi"})))
        out = _FakeStream()
        monkeypatch.setattr(sys, "stdout", out)
        monkeypatch.setattr(host, "_forward", lambda t: True)
        host.main()
        assert _decode_reply(out)["ok"] is True

    def test_forward_fail_launches_and_replies(self, host, monkeypatch):
        monkeypatch.setattr(sys, "stdin", _FakeStream(_pack({"text": "hi"})))
        out = _FakeStream()
        monkeypatch.setattr(sys, "stdout", out)
        monkeypatch.setattr(host, "_forward", lambda t: False)
        monkeypatch.setattr(host, "_write_pending_and_launch", lambda t: True)
        host.main()
        reply = _decode_reply(out)
        assert reply["ok"] is True and reply.get("launched") is True

    def test_both_fail_replies_error(self, host, monkeypatch):
        monkeypatch.setattr(sys, "stdin", _FakeStream(_pack({"text": "hi"})))
        out = _FakeStream()
        monkeypatch.setattr(sys, "stdout", out)
        monkeypatch.setattr(host, "_forward", lambda t: False)
        monkeypatch.setattr(host, "_write_pending_and_launch", lambda t: False)
        host.main()
        assert _decode_reply(out)["ok"] is False

    def test_long_text_truncated_before_forward(self, host, monkeypatch):
        big = "x" * 20000  # > TEXT_INVOKE_CAP (8192)
        monkeypatch.setattr(sys, "stdin", _FakeStream(_pack({"text": big})))
        out = _FakeStream()
        monkeypatch.setattr(sys, "stdout", out)
        captured = []
        monkeypatch.setattr(host, "_forward", lambda t: captured.append(t) or True)
        host.main()
        assert "…[已截断]" in captured[0]
        assert len(captured[0]) < 20000

    def test_forward_receives_text_not_message(self, host, monkeypatch):
        # _forward gets the bare text string, not the raw {text: ...} dict
        monkeypatch.setattr(sys, "stdin", _FakeStream(_pack({"text": "payload"})))
        out = _FakeStream()
        monkeypatch.setattr(sys, "stdout", out)
        captured = []
        monkeypatch.setattr(host, "_forward", lambda t: captured.append(t) or True)
        host.main()
        assert captured[0] == "payload"
