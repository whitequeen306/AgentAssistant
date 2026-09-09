"""User-editable instruction files (P2-A): SOUL / USER / AGENTS.

These are optional Markdown files under the data dir. When present and
non-empty (ignoring HTML comments), their content is appended to the
system prompt — same assembly pipeline as P0 sections, but authored by
the user without code changes.

Does not replace SQLite ProfileStore (agent-maintained facts) or the
built-in persona sections (product defaults).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# Filenames under settings.data_dir (stable, case-sensitive on some FS)
INSTRUCTION_FILES: tuple[tuple[str, str], ...] = (
    ("SOUL.md", "Soul（人格 / 语气）"),
    ("USER.md", "User（关于你）"),
    ("AGENTS.md", "Agents（做事规则）"),
)

_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)

# Defaults align with OpenClaw-style bootstrap budgets (chars, not tokens)
DEFAULT_MAX_CHARS_PER_FILE = 8_000
DEFAULT_MAX_TOTAL_CHARS = 20_000

_SOUL_STUB = """\
<!--
AgentAssistant — SOUL.md（可选）
在下方写你希望助手的人格、语气、禁忌。
删光或只留本注释 = 不注入，继续用产品默认人设。
-->

"""

_USER_STUB = """\
<!--
AgentAssistant — USER.md（可选）
在下方写关于你自己的说明：称呼、时区、工作习惯、偏好等。
与「资料库里的 User Profile」互补：这里是你手写的长说明，Profile 是助手用工具记的短事实。
-->

"""

_AGENTS_STUB = """\
<!--
AgentAssistant — AGENTS.md（可选）
在下方写做事规则：例如默认用中文、报告要带来源、某些目录不要动等。
-->

"""

_STUBS: dict[str, str] = {
    "SOUL.md": _SOUL_STUB,
    "USER.md": _USER_STUB,
    "AGENTS.md": _AGENTS_STUB,
}


@dataclass(frozen=True)
class LoadedInstruction:
    filename: str
    title: str
    content: str  # effective body (comments stripped), possibly truncated
    truncated: bool = False


def instructions_dir(data_dir: Path | None = None) -> Path:
    """Root directory for instruction files (= app data dir)."""
    if data_dir is not None:
        return data_dir
    from agent_assistant.config import settings

    return settings.data_dir


def ensure_instruction_stubs(data_dir: Path | None = None) -> list[Path]:
    """Create stub SOUL/USER/AGENTS.md if missing. Never overwrites existing."""
    root = instructions_dir(data_dir)
    root.mkdir(parents=True, exist_ok=True)
    created: list[Path] = []
    for name, stub in _STUBS.items():
        path = root / name
        if path.exists():
            continue
        path.write_text(stub, encoding="utf-8")
        created.append(path)
        logger.info("Created instruction stub: %s", path)
    return created


def _effective_body(raw: str) -> str:
    """Strip HTML comments; leftover whitespace-only → empty."""
    return _COMMENT_RE.sub("", raw or "").strip()


def _read_file(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as e:
        logger.warning("Failed to read instruction file %s: %s", path, e)
        return ""


def load_instructions(
    data_dir: Path | None = None,
    *,
    max_chars_per_file: int = DEFAULT_MAX_CHARS_PER_FILE,
    max_total_chars: int = DEFAULT_MAX_TOTAL_CHARS,
) -> list[LoadedInstruction]:
    """Load non-empty instruction files with truncation caps."""
    root = instructions_dir(data_dir)
    loaded: list[LoadedInstruction] = []
    total = 0

    for filename, title in INSTRUCTION_FILES:
        if total >= max_total_chars:
            break
        path = root / filename
        if not path.is_file():
            continue
        body = _effective_body(_read_file(path))
        if not body:
            continue

        budget = min(max_chars_per_file, max_total_chars - total)
        truncated = len(body) > budget
        content = body[:budget]
        if truncated:
            content = content.rstrip() + "\n…(truncated)"
        loaded.append(
            LoadedInstruction(
                filename=filename,
                title=title,
                content=content,
                truncated=truncated,
            )
        )
        total += len(content)

    return loaded


def build_instructions_section(
    data_dir: Path | None = None,
    *,
    max_chars_per_file: int = DEFAULT_MAX_CHARS_PER_FILE,
    max_total_chars: int = DEFAULT_MAX_TOTAL_CHARS,
    lang: str = "zh",
) -> str:
    """Render loaded files as one system-prompt section (or empty)."""
    items = load_instructions(
        data_dir,
        max_chars_per_file=max_chars_per_file,
        max_total_chars=max_total_chars,
    )
    if not items:
        return ""

    if lang == "en":
        header = (
            "# Workspace Instructions\n"
            "User-authored files below supplement (do not silently ignore) "
            "the built-in rules. If they conflict with Safety, Safety wins.\n"
        )
    else:
        header = (
            "# 工作区指令（用户可编辑文件）\n"
            "以下内容由用户在数据目录的 Markdown 中编写，补充产品默认规则。"
            "若与「安全」冲突，以安全为准。\n"
        )

    parts = [header]
    for item in items:
        parts.append(f"## {item.title}（{item.filename}）\n{item.content}\n")
    return "\n".join(parts)
