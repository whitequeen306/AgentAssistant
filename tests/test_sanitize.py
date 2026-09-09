"""Tests for B9: Error sanitization — strip sensitive info before model sees it.

Covers:
- File paths (Windows + Unix) are stripped from error messages
- Stack traces / tracebacks are removed
- Version strings are removed
- Generic category message is returned instead
- Full detail is logged (verified via caplog)
- Structured error codes preserved
"""

import logging

from agent_assistant.tools.sanitize import sanitize_error


class TestSanitizeErrorPaths:
    """File system paths must not leak to the model."""

    def test_windows_path_stripped(self):
        raw = (
            r"FileNotFoundError: [Errno 2] No such file or directory:"
            r" 'C:\Users\hp\secret\api_keys.json'"
        )
        result = sanitize_error(raw, context="read_file")
        assert "C:\\Users" not in result.safe_message
        assert "api_keys" not in result.safe_message
        assert result.safe_message  # non-empty

    def test_unix_path_stripped(self):
        raw = "PermissionError: [Errno 13] Permission denied: '/home/user/.ssh/id_rsa'"
        result = sanitize_error(raw, context="read_file")
        assert "/home/user" not in result.safe_message
        assert ".ssh" not in result.safe_message

    def test_relative_path_stripped(self):
        raw = "OSError: cannot open '..\\..\\..\\Windows\\System32\\config\\SAM'"
        result = sanitize_error(raw, context="read_file")
        assert "System32" not in result.safe_message


class TestSanitizeErrorStackTraces:
    """Tracebacks must not leak."""

    def test_traceback_stripped(self):
        raw = (
            'Traceback (most recent call last):\n'
            '  File "C:\\Users\\hp\\AppData\\agent\\loop.py", line 42, in chat\n'
            '    response = llm_client.chat(messages)\n'
            '  File "C:\\Users\\hp\\AppData\\agent\\client.py", line 88, in chat\n'
            '    raise ConnectionError("timeout")\n'
            'ConnectionError: timeout'
        )
        result = sanitize_error(raw, context="llm_call")
        assert "loop.py" not in result.safe_message
        assert "client.py" not in result.safe_message
        assert "line 42" not in result.safe_message

    def test_stderr_with_paths(self):
        raw = (
            "At C:\\Users\\hp\\script.ps1:3 char:1\n"
            "+ Get-Content C:\\secret\\passwords.txt\n"
            "+ CategoryInfo: ObjectNotFound: (C:\\secret\\passwords.txt:String)"
        )
        result = sanitize_error(raw, context="run_command")
        assert "C:\\secret" not in result.safe_message
        assert "script.ps1" not in result.safe_message


class TestSanitizeErrorVersions:
    """Version info must not leak."""

    def test_python_version_stripped(self):
        raw = "Python 3.11.9 (tags/v3.11.9:de54cf5, Apr  2 2024) module not found"
        result = sanitize_error(raw, context="run_command")
        assert "3.11.9" not in result.safe_message

    def test_package_version_stripped(self):
        raw = "openai 1.35.2 error: rate limit exceeded"
        result = sanitize_error(raw, context="llm_call")
        assert "1.35.2" not in result.safe_message


class TestSanitizeErrorPreservesCategory:
    """The model should still get a useful category description."""

    def test_timeout_category(self):
        raw = (
            "ConnectionError: HTTPSConnectionPool(host='api.deepseek.com',"
            " port=443): Read timed out after 30s"
        )
        result = sanitize_error(raw, context="llm_call")
        # Should mention timeout/network category
        assert any(
            w in result.safe_message.lower()
            for w in ("timeout", "network", "connection", "unavailable")
        )

    def test_permission_category(self):
        raw = "PermissionError: [Errno 13] Permission denied: 'C:\\Windows\\System32\\x'"
        result = sanitize_error(raw, context="write_file")
        assert (
            "permission" in result.safe_message.lower()
            or "denied" in result.safe_message.lower()
        )

    def test_not_found_category(self):
        raw = "FileNotFoundError: [Errno 2] No such file or directory: 'C:\\x\\y.txt'"
        result = sanitize_error(raw, context="read_file")
        assert "not found" in result.safe_message.lower() or "exist" in result.safe_message.lower()


class TestSanitizeErrorLogging:
    """Full detail must be logged locally (for debugging)."""

    def test_full_detail_logged(self, caplog):
        raw = r"FileNotFoundError: 'C:\Users\hp\secret\key.pem'"
        with caplog.at_level(logging.DEBUG, logger="agent_assistant.tools.sanitize"):
            sanitize_error(raw, context="read_file")
        # The raw message should appear in debug logs
        assert "key.pem" in caplog.text

    def test_context_included_in_log(self, caplog):
        raw = "some error"
        with caplog.at_level(logging.DEBUG, logger="agent_assistant.tools.sanitize"):
            sanitize_error(raw, context="web_search")
        assert "web_search" in caplog.text


class TestSanitizedErrorStructure:
    """SanitizedError dataclass has expected fields."""

    def test_fields(self):
        result = sanitize_error("anything", context="test")
        assert isinstance(result.safe_message, str)
        assert isinstance(result.category, str)
        assert result.category != ""

    def test_code_preserved_when_provided(self):
        result = sanitize_error("HTTP 403 Forbidden", context="fetch", code=403)
        assert result.code == 403

    def test_code_none_by_default(self):
        result = sanitize_error("oops", context="test")
        assert result.code is None


class TestPublicLlmError:
    def test_402_balance(self):
        from agent_assistant.tools.sanitize import public_llm_error

        class Fake(Exception):
            status_code = 402

        msg = public_llm_error(Fake("Error code: 402 - Insufficient Balance"))
        assert "余额" in msg
        assert "402" not in msg
        assert "Insufficient" not in msg

    def test_generic_no_leak(self):
        from agent_assistant.tools.sanitize import public_llm_error

        msg = public_llm_error(RuntimeError("https://api.deepseek.com/v1 boom sk-abc"))
        assert "deepseek" not in msg.lower()
        assert "sk-" not in msg
        assert "失败" in msg or "不可用" in msg
