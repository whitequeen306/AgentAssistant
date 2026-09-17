"""归档溢出的滚动摘要层（永不删除），供 ``recall_summary`` 检索。

为什么要归档而不是"再压一遍"：滚动摘要采用**拼接**策略——每层都是当时
在完整上下文下写成的，旧层永不重写，这是防止细节逐层衰减的关键。但层数
终究会累积到上限；此时若把整段摘要交给模型再压一次，恰恰就是我们要避免的
"摘要的摘要"。

所以到上限时改为：**把最老的层移到磁盘，上下文里只留一行指针**。模型需要
那些内容时用 ``recall_summary`` 取回——与工具结果的 ``recall_tool_result``
同构（归档 + 召回成对设计，缺一个就等于清掉即永久丢失）。

归档目录按会话隔离（与 ``tool_details.jsonl`` 同一 ``sessions/<conv>/``
布局），避免跨会话的摘要互相泄漏。
"""

from __future__ import annotations

import json
import logging
import re
import threading
from datetime import datetime
from pathlib import Path

from agent_assistant.config import settings
from agent_assistant.memory.session_trace import safe_session_name

logger = logging.getLogger(__name__)

_LAYER_SEP = "\n\n---\n\n"
_MAX_FILENAME = 80
_lock = threading.Lock()


def _resolve_conversation_id(conversation_id: str | None) -> str:
    """显式传入优先；否则取当前 turn 绑定的会话；都没有时落到 "local"。"""
    if conversation_id:
        return conversation_id
    from agent_assistant.subagents.context import current_execution_context

    context = current_execution_context()
    return context.conversation_id if context is not None else "local"


def summaries_dir(conversation_id: str | None = None) -> Path:
    """会话级归档目录；不存在时创建。"""
    path = (
        settings.data_dir
        / "sessions"
        / safe_session_name(_resolve_conversation_id(conversation_id))
        / "summaries"
    )
    path.mkdir(parents=True, exist_ok=True)
    return path


def _slug(text: str, limit: int = 40) -> str:
    """把一层摘要压成文件名片段（跳过「## 小节标题」行，取首个内容行，
    保留可读性，去掉危险字符——文件名是模型的归档索引，必须可辨识）。"""
    lines = (text or "").strip().splitlines()
    first_line = next(
        (ln.strip() for ln in lines if ln.strip() and not ln.strip().startswith("#")),
        "layer",
    )
    cleaned = re.sub(r"[^\w\u4e00-\u9fff]+", "-", first_line).strip("-")
    return (cleaned or "layer")[:_MAX_FILENAME]


def archive_layers(
    layers: list[str],
    *,
    conversation_id: str | None = None,
    stamp: str | None = None,
) -> list[str]:
    """把若干层摘要写入归档目录，返回文件名列表（顺序与输入一致）。

    写入失败不抛异常：归档是辅助能力，绝不能因此中断正在进行的对话。
    """
    if not layers:
        return []
    stamp = stamp or datetime.now().strftime("%Y%m%d-%H%M%S")
    written: list[str] = []
    try:
        with _lock:
            base = summaries_dir(conversation_id)
            for i, layer in enumerate(layers, start=1):
                name = f"{stamp}-{i:02d}-{_slug(layer)}.md"
                target = base / name
                if target.exists():
                    name = f"{stamp}-{i:02d}-{len(layer)}-{_slug(layer)}.md"
                    target = base / name
                target.write_text(layer, encoding="utf-8")
                written.append(name)
    except Exception:  # noqa: BLE001 — 归档失败不能影响对话
        logger.warning("failed to archive summary layers", exc_info=True)
    return written


def pointer_line(files: list[str], covered_hint: str = "") -> str:
    """上下文里保留的那行指针（没有它，模型不知道归档存在）。"""
    if not files:
        return ""
    names = "、".join(files[:5]) + ("…" if len(files) > 5 else "")
    hint = f"（覆盖 {covered_hint}）" if covered_hint else ""
    return (
        f"（更早的摘要已归档：{names}{hint}；"
        f"需要时可用 recall_summary 查看任意一份）"
    )


def split_layers(summary: str) -> list[str]:
    """把一个滚动摘要按分隔线拆回各层。"""
    if not summary:
        return []
    return [part for part in summary.split(_LAYER_SEP) if part.strip()]


def list_summaries(
    limit: int = 50,
    *,
    conversation_id: str | None = None,
) -> list[dict[str, object]]:
    """归档索引：文件名 + 首行摘录 + 大小（供模型快速定位）。"""
    try:
        files = sorted(
            summaries_dir(conversation_id).glob("*.md"), reverse=True
        )[:limit]
    except Exception:  # noqa: BLE001
        return []
    out: list[dict[str, object]] = []
    for f in files:
        try:
            head = f.read_text(encoding="utf-8", errors="replace").strip().splitlines()
            excerpt = (head[0] if head else "")[:120]
            out.append({"name": f.name, "excerpt": excerpt, "bytes": f.stat().st_size})
        except Exception:  # noqa: BLE001
            continue
    return out


def read_summary(
    name: str,
    *,
    offset: int = 0,
    limit: int = 4000,
    conversation_id: str | None = None,
) -> str:
    """读取一份归档摘要（分页）。"""
    safe = Path(name).name          # 防目录穿越
    if not safe.endswith(".md"):
        return ""
    target = summaries_dir(conversation_id) / safe
    if not target.exists():
        return ""
    try:
        text = target.read_text(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        return ""
    return text[offset : offset + limit]


def search_summaries(
    keyword: str,
    limit: int = 10,
    *,
    conversation_id: str | None = None,
) -> list[dict[str, object]]:
    """按关键词在归档摘要里检索（返回命中文件 + 上下文片段）。"""
    key = (keyword or "").strip()
    if not key:
        return []
    key_low = key.lower()
    hits: list[dict[str, object]] = []
    for entry in list_summaries(limit=200, conversation_id=conversation_id):
        name = str(entry["name"])
        text = read_summary(
            name, offset=0, limit=200_000, conversation_id=conversation_id
        )
        idx = text.lower().find(key_low)
        if idx < 0:
            continue
        start = max(0, idx - 80)
        hits.append({
            "name": name,
            "snippet": text[start : idx + len(key) + 120].replace("\n", " "),
        })
        if len(hits) >= limit:
            break
    return hits


__all__ = [
    "archive_layers",
    "list_summaries",
    "pointer_line",
    "read_summary",
    "search_summaries",
    "split_layers",
    "summaries_dir",
]
