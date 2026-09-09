from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor
from enum import Enum
from pathlib import Path

import pytest

from agent_assistant.config import Settings
from agent_assistant.subagents.archive import TaskArchive, TaskArchiveError


class Kind(Enum):
    FILE = "file"


class Unknown:
    def __repr__(self) -> str:
        return "Unknown(sk-secret-value)"


def read_jsonl(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_settings_exposes_subagent_paths_and_ensures_archive_dir(tmp_path, monkeypatch) -> None:
    settings = Settings(data_dir=tmp_path)
    monkeypatch.setattr(
        "agent_assistant.agent.instructions.ensure_instruction_stubs",
        lambda _path: None,
    )

    settings.ensure_dirs()

    assert settings.subagent_max_concurrency == 4
    assert settings.subagent_runs_dir == tmp_path / "subagent_runs"
    assert settings.subagent_db_path == tmp_path / "subagents.db"
    assert settings.subagent_runs_dir.is_dir()


@pytest.mark.parametrize(
    "task_id",
    [
        "",
        ".",
        "..",
        "../escape",
        r"..\escape",
        "/absolute",
        r"C:\absolute",
        "a/b",
        "a\\b",
        "Task",
        "task.",
        "task ",
        "con",
        "CON",
        "prn",
        "aux",
        "nul",
        "com1",
        "COM9",
        "lpt1",
        "LPT9",
    ],
)
def test_archive_rejects_noncanonical_or_unsafe_task_ids(tmp_path, task_id) -> None:
    with pytest.raises(ValueError, match="task_id"):
        TaskArchive(tmp_path, task_id)


def test_archive_paths_are_scoped_to_validated_task_id(tmp_path) -> None:
    archive = TaskArchive(tmp_path, "task_1-abc")

    assert archive.directory == tmp_path / "task_1-abc"
    assert archive.raw_path == archive.directory / "raw.jsonl"
    assert archive.trace_path == archive.directory / "trace.jsonl"
    assert archive.checkpoint_path == archive.directory / "checkpoint.json"
    assert archive.output_path == archive.directory / "output.md"
    assert not archive.raw_path.exists()
    assert not archive.trace_path.exists()
    assert not archive.checkpoint_path.exists()
    assert not archive.output_path.exists()


def test_archive_accepts_lowercase_canonical_task_id(tmp_path) -> None:
    assert TaskArchive(tmp_path, "task").task_id == "task"


def test_archive_rejects_symlink_raw_and_checkpoint_targets(tmp_path) -> None:
    archive = TaskArchive(tmp_path / "runs", "task-1")
    outside_raw = tmp_path / "outside.raw"
    outside_checkpoint = tmp_path / "outside.json"
    outside_raw.write_text("unchanged", encoding="utf-8")
    outside_checkpoint.write_text('{"safe": true}', encoding="utf-8")
    try:
        archive.raw_path.symlink_to(outside_raw)
        archive.checkpoint_path.symlink_to(outside_checkpoint)
    except OSError:
        pytest.skip("symlinks are unavailable on this platform")

    with pytest.raises(TaskArchiveError, match="unsafe"):
        archive.log_raw("tool", {}, {})
    with pytest.raises(TaskArchiveError, match="unsafe"):
        archive.write_checkpoint({"changed": True})

    assert outside_raw.read_text(encoding="utf-8") == "unchanged"
    assert outside_checkpoint.read_text(encoding="utf-8") == '{"safe": true}'


def test_log_raw_recursively_redacts_secrets_and_ui_text(tmp_path) -> None:
    archive = TaskArchive(tmp_path, "task-1")
    archive.log_raw(
        "http",
        {
            "password": "pw",
            "nested": {
                "API_KEY": "sk-secret-value",
                "Authorization": "Bearer token",
                "Cookie": "session=secret",
            },
            "path": Path("relative/file"),
            "enum": Kind.FILE,
            "bytes": b"secret",
            "unknown": Unknown(),
        },
        {"access_token": "Bearer token", "ok": True},
    )
    archive.log_raw("ui_type", {"text": "sk-secret-value Bearer token Cookie"}, {"ok": True})

    text = archive.raw_path.read_text(encoding="utf-8")
    records = read_jsonl(archive.raw_path)

    assert "sk-secret-value" not in text
    assert "Bearer token" not in text
    assert "session=secret" not in text
    assert records[0]["arguments"]["password"] == "<redacted>"
    assert records[0]["arguments"]["path"] == str(Path("relative/file"))
    assert records[0]["arguments"]["enum"] == "file"
    assert records[0]["arguments"]["bytes"] == "<bytes:6>"
    assert records[0]["arguments"]["unknown"] == "<unsupported object>"
    assert records[1]["arguments"]["text"] == "<redacted>"


def test_nested_sensitive_key_variants_are_redacted(tmp_path) -> None:
    archive = TaskArchive(tmp_path, "task-1")
    archive.log_raw(
        "http",
        {
            "headers": {
                "api-key": "one",
                "api key": "two",
                "apikey": "three",
                "x-api-key": "four",
                "Authorization": "five",
                "Cookie": "six",
                "safe": "visible",
            }
        },
        {"ok": True},
    )

    headers = read_jsonl(archive.raw_path)[0]["arguments"]["headers"]

    assert headers["safe"] == "visible"
    assert all(
        headers[key] == "<redacted>"
        for key in ("api-key", "api key", "apikey", "x-api-key", "Authorization", "Cookie")
    )


def test_ui_type_redacts_text_at_every_nested_dict_and_list_level(tmp_path) -> None:
    archive = TaskArchive(tmp_path, "task-1")
    archive.log_raw(
        "ui_type",
        {
            "nested": {
                "text": "first secret",
                "items": [{"text": "second secret"}, {"safe": "visible"}],
            }
        },
        {"text": "result remains visible"},
    )

    record = read_jsonl(archive.raw_path)[0]

    assert record["arguments"]["nested"]["text"] == "<redacted>"
    assert record["arguments"]["nested"]["items"][0]["text"] == "<redacted>"
    assert record["arguments"]["nested"]["items"][1]["safe"] == "visible"
    assert record["result"]["text"] == "result remains visible"


def test_header_values_and_multisegment_cookie_lines_are_fully_redacted(tmp_path) -> None:
    archive = TaskArchive(tmp_path, "task-1")
    archive.log_raw(
        "http",
        {
            "headers": [
                "Cookie: sid=one; refresh=two",
                "Authorization: Bearer top-secret-token",
            ],
            "nested": {
                "Cookie": "sid=three; refresh=four",
                "note": "credential sk-secret-value and Bearer abc.def",
            },
        },
        {"ok": True},
    )

    text = archive.raw_path.read_text(encoding="utf-8")
    arguments = read_jsonl(archive.raw_path)[0]["arguments"]

    assert arguments["headers"] == [
        "Cookie: <redacted>",
        "Authorization: <redacted>",
    ]
    assert arguments["nested"]["Cookie"] == "<redacted>"
    for secret in ("sid=one", "refresh=two", "top-secret-token", "refresh=four"):
        assert secret not in text
    assert "sk-secret-value" not in text
    assert "abc.def" not in text


def test_multiline_headers_preserve_names_newlines_and_normal_lines(tmp_path) -> None:
    archive = TaskArchive(tmp_path, "task-1")
    headers = (
        "Accept: application/json\r\n"
        "  Authorization: Basic QWxhZGRpbjpvcGVuIHNlc2FtZQ==\r\n"
        "Cookie: sid=one; refresh=two\n"
        "X-Normal: kept"
    )
    archive.log_raw("http", {"headers": headers}, {"ok": True})

    sanitized = read_jsonl(archive.raw_path)[0]["arguments"]["headers"]

    assert sanitized == (
        "Accept: application/json\r\n"
        "  Authorization: <redacted>\r\n"
        "Cookie: <redacted>\n"
        "X-Normal: kept"
    )
    assert "QWxhZGRpbjpvcGVuIHNlc2FtZQ==" not in archive.raw_path.read_text(encoding="utf-8")
    assert "refresh=two" not in archive.raw_path.read_text(encoding="utf-8")


def test_multiline_trace_error_is_redacted_before_debug_logging(
    tmp_path,
    caplog,
) -> None:
    archive = TaskArchive(tmp_path, "task-1")
    raw = (
        "request failed\r\n"
        "Authorization: Basic dXNlcjpwYXNz\r\n"
        " Cookie: sid=secret; refresh=hidden\n"
        "X-Request: visible"
    )

    with caplog.at_level(logging.DEBUG, logger="agent_assistant.tools.sanitize"):
        archive.log_trace("tool_failed", {"error": raw})

    trace_text = archive.trace_path.read_text(encoding="utf-8")

    for secret in ("dXNlcjpwYXNz", "sid=secret", "refresh=hidden"):
        assert secret not in trace_text
        assert secret not in caplog.text
    assert "Authorization: <redacted>" in caplog.text
    assert "Cookie: <redacted>" in caplog.text
    assert "X-Request: visible" in caplog.text


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (
            "api_key=plain-secret password=hunter2 normal=kept",
            "api_key=<redacted> password=<redacted> normal=kept",
        ),
        (
            "https://service.test/run?token=query-token&next=visible",
            "https://service.test/run?token=<redacted>&next=visible",
        ),
        (
            "passwd: 'quoted secret' mode=ok",
            "passwd: '<redacted>' mode=ok",
        ),
        (
            'SECRET="double quoted" count=2',
            'SECRET="<redacted>" count=2',
        ),
        (
            "authorization = basic-value cookie=chip-value tail=ok",
            "authorization = <redacted> cookie=<redacted> tail=ok",
        ),
    ],
)
def test_inline_sensitive_assignments_redact_only_values(tmp_path, raw, expected) -> None:
    archive = TaskArchive(tmp_path, "task-1")
    archive.log_raw("tool", {"nested": [{"message": raw}]}, {"ok": True})

    record = read_jsonl(archive.raw_path)[0]

    assert record["arguments"]["nested"][0]["message"] == expected


@pytest.mark.parametrize(
    ("raw", "expected", "secret"),
    [
        ("api-key=variant-one", "api-key=<redacted>", "variant-one"),
        ("api_key: 'variant two'", "api_key: '<redacted>'", "variant two"),
        ('api key = "variant three"', 'api key = "<redacted>"', "variant three"),
        (
            "https://service.test/?apikey=query-four&safe=yes",
            "https://service.test/?apikey=<redacted>&safe=yes",
            "query-four",
        ),
        (
            "x-api-key: variant-five safe=yes",
            "x-api-key: <redacted> safe=yes",
            "variant-five",
        ),
    ],
)
def test_inline_api_key_variants_are_safe_in_l0_l1_and_debug_logs(
    tmp_path,
    caplog,
    raw,
    expected,
    secret,
) -> None:
    archive = TaskArchive(tmp_path, "task-1")

    with caplog.at_level(logging.DEBUG, logger="agent_assistant.tools.sanitize"):
        archive.log_raw("tool", {"note": raw}, {})
        archive.log_trace("tool_failed", {"error": raw})

    l0_record = read_jsonl(archive.raw_path)[0]
    l0_text = archive.raw_path.read_text(encoding="utf-8")
    l1_text = archive.trace_path.read_text(encoding="utf-8")

    assert l0_record["arguments"]["note"] == expected
    assert secret not in l0_text
    assert secret not in l1_text
    assert secret not in caplog.text


@pytest.mark.parametrize(
    ("raw", "expected", "secret"),
    [
        (
            '{"api_key":"plain-secret","safe":"kept"}',
            '{"api_key":"<redacted>","safe":"kept"}',
            "plain-secret",
        ),
        (
            '{"api-key":"variant-secret","safe":"kept"}',
            '{"api-key":"<redacted>","safe":"kept"}',
            "variant-secret",
        ),
        (
            "{'api key':'single-secret','safe':'kept'}",
            "{'api key':'<redacted>','safe':'kept'}",
            "single-secret",
        ),
    ],
)
def test_quoted_sensitive_keys_are_safe_in_l0_l1_and_debug_logs(
    tmp_path,
    caplog,
    raw,
    expected,
    secret,
) -> None:
    archive = TaskArchive(tmp_path, "task-1")

    with caplog.at_level(logging.DEBUG, logger="agent_assistant.tools.sanitize"):
        archive.log_raw("tool", {"note": raw}, {})
        archive.log_trace("tool_failed", {"error": raw})

    assert read_jsonl(archive.raw_path)[0]["arguments"]["note"] == expected
    assert secret not in archive.raw_path.read_text(encoding="utf-8")
    assert secret not in archive.trace_path.read_text(encoding="utf-8")
    assert secret not in caplog.text


@pytest.mark.parametrize(
    ("raw", "expected", "secret"),
    [
        (
            "prefix Authorization: Basic dXNlcjpwYXNz",
            "prefix Authorization: <redacted>",
            "dXNlcjpwYXNz",
        ),
        (
            "context Cookie: sid=one; refresh=two",
            "context Cookie: <redacted>",
            "refresh=two",
        ),
    ],
)
def test_prefixed_header_values_are_safe_in_l0_l1_and_debug_logs(
    tmp_path,
    caplog,
    raw,
    expected,
    secret,
) -> None:
    archive = TaskArchive(tmp_path, "task-1")

    with caplog.at_level(logging.DEBUG, logger="agent_assistant.tools.sanitize"):
        archive.log_raw("tool", {"note": raw}, {})
        archive.log_trace("tool_failed", {"error": raw})

    assert read_jsonl(archive.raw_path)[0]["arguments"]["note"] == expected
    assert secret not in archive.raw_path.read_text(encoding="utf-8")
    assert secret not in archive.trace_path.read_text(encoding="utf-8")
    assert secret not in caplog.text


def test_trace_error_is_credential_clean_before_sanitize_error_logging(
    tmp_path,
    caplog,
) -> None:
    archive = TaskArchive(tmp_path, "task-1")
    raw = (
        "failure api_key=plain-secret password='hunter 2' "
        "token: query-token normal=visible"
    )

    with caplog.at_level(logging.DEBUG, logger="agent_assistant.tools.sanitize"):
        archive.log_trace(
            "tool_failed",
            {
                "nested": [{"error": raw}],
                "summary": "retry token=summary-token after validation",
            },
        )

    trace_text = archive.trace_path.read_text(encoding="utf-8")
    record = read_jsonl(archive.trace_path)[0]

    assert record["data"]["nested"][0]["error"]
    assert record["data"]["summary"] == "retry token=<redacted> after validation"
    for secret in ("plain-secret", "hunter 2", "query-token", "summary-token"):
        assert secret not in trace_text
        assert secret not in caplog.text
    assert "normal=visible" in caplog.text


def test_log_trace_sanitizes_exceptions_without_internal_details(tmp_path) -> None:
    archive = TaskArchive(tmp_path, "task-1")
    archive.log_trace(
        "tool_failed",
        {
            "tool": "read",
            "error": RuntimeError(
                r"Bearer token at C:\private\app.py using sqlite 3.99 sk-secret-value"
            ),
        },
    )

    text = archive.trace_path.read_text(encoding="utf-8")
    record = read_jsonl(archive.trace_path)[0]

    assert record["event"] == "tool_failed"
    assert record["data"]["error"] == "An unexpected error occurred during this operation."
    assert "Bearer token" not in text
    assert "private" not in text
    assert "sqlite" not in text
    assert "sk-secret-value" not in text


def test_log_trace_sanitizes_string_errors_but_preserves_normal_summary(tmp_path) -> None:
    archive = TaskArchive(tmp_path, "task-1")
    archive.log_trace(
        "tool_failed",
        {
            "error": (
                r"sqlite 3.45 Python 3.11 at C:\private\app.py and /home/user/app.py "
                "SELECT token FROM private_table; Bearer token sk-secret-value"
            ),
            "summary": "Could not finish report; credential Bearer summary-token removed.",
        },
    )

    text = archive.trace_path.read_text(encoding="utf-8")
    data = read_jsonl(archive.trace_path)[0]["data"]

    assert data["error"] == "An unexpected error occurred during this operation."
    assert data["summary"] == "Could not finish report; credential <redacted> removed."
    for internal in (
        "private",
        "/home/user",
        "sqlite",
        "Python 3.11",
        "SELECT",
        "private_table",
        "summary-token",
        "sk-secret-value",
    ):
        assert internal not in text


def test_checkpoint_output_and_concurrent_jsonl_writes(tmp_path) -> None:
    archive = TaskArchive(tmp_path, "task-1")
    archive.write_checkpoint({"step": 1, "token": "sk-secret-value"})
    assert archive.read_checkpoint() == {"step": 1, "token": "<redacted>"}
    assert not archive.checkpoint_path.with_suffix(".tmp").exists()

    archive.write_output("# 完成\n")
    assert archive.output_path.read_text(encoding="utf-8") == "# 完成\n"

    with ThreadPoolExecutor(max_workers=12) as pool:
        list(
            pool.map(
                lambda index: archive.log_raw(
                    "tool",
                    {"index": index, "authorization": f"Bearer {index}"},
                    {"index": index},
                ),
                range(100),
            )
        )

    records = read_jsonl(archive.raw_path)
    assert len(records) == 100
    assert {record["result"]["index"] for record in records} == set(range(100))


def test_two_archive_instances_share_jsonl_and_checkpoint_locks(tmp_path) -> None:
    first = TaskArchive(tmp_path, "task-1")
    second = TaskArchive(tmp_path, "task-1")

    def write(index: int) -> None:
        archive = first if index % 2 else second
        archive.log_raw("tool", {"index": index}, {"ok": True})
        archive.write_checkpoint({"index": index})

    with ThreadPoolExecutor(max_workers=16) as pool:
        list(pool.map(write, range(200)))

    records = read_jsonl(first.raw_path)
    assert len(records) == 200
    assert {record["arguments"]["index"] for record in records} == set(range(200))
    assert first.read_checkpoint()["index"] in range(200)


def test_write_output_uses_replace_and_cleans_temporary_on_failure(
    tmp_path,
    monkeypatch,
) -> None:
    archive = TaskArchive(tmp_path, "task-1")
    archive.write_output("old")
    replace_calls: list[tuple[Path, Path]] = []

    def fail_replace(source: Path, target: Path) -> None:
        replace_calls.append((source, target))
        raise OSError("replace failed")

    monkeypatch.setattr("agent_assistant.subagents.archive.os.replace", fail_replace)

    with pytest.raises(TaskArchiveError, match="Unable to write task output"):
        archive.write_output("new")

    assert len(replace_calls) == 1
    temporary, target = replace_calls[0]
    assert target == archive.output_path
    assert temporary.parent == archive.directory
    assert temporary.suffix == ".tmp"
    assert archive.output_path.read_text(encoding="utf-8") == "old"
    assert not temporary.exists()

