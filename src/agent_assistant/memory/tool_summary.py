"""工具结果的固定摘要（零成本，不调用模型）。

用途：把**老的工具结果**压成一行可读摘要，保留「做过什么、结果如何」的线索，
把膨胀的正文交给 tool_archive（模型可用 ``recall_tool_result`` 精确取回）。

设计约束（与 docs/context-management-refactor.md §6 一致）：

1. **零模型调用** —— 纯代码提取，只做字段级归纳；
2. **失败写分类，不写原始报错** —— ``"HTTP 500"`` 模型看不出该怎么办，
   而 ``"此站反爬"`` 能引导它换源（沿用 ``tools.failure_feedback`` 的分类）；
3. **永不抛异常** —— 解析不了就退化为一行兜底文案，绝不能因摘要失败而中断压缩。

摘要形态：``ok=<bool> | <正文>``
"""

from __future__ import annotations

import json
from typing import Any

# ── 工具大类 ──────────────────────────────────────────────────────────────────

_CATEGORY_BY_TOOL: dict[str, str] = {
    # 文件读取
    "read_file": "file_read",
    "read_document": "file_read",
    "read_attached_source": "file_read",
    "read_local_source": "file_read",
    # 文件写入 / 修改
    "write_file": "file_write",
    "edit_file": "file_write",
    "apply_patch": "file_write",
    "move_file": "file_write",
    "save_note": "file_write",
    # 本地检索
    "search_local_files": "search_local",
    "list_files": "search_local",
    "glob": "search_local",
    "grep": "search_local",
    # 网络
    "web_search": "web_search",
    "read_page": "web_read",
    "extract_content": "web_read",
    "webfetch": "web_read",
    # 命令 / 应用
    "run_command": "command",
    "launch_app": "app",
    "focus_window": "app",
    "kill_process": "app",
    # UI
    "ui_inspect": "ui_snapshot",
    "perf_snapshot": "ui_snapshot",
    "ui_click": "ui_action",
    "ui_type": "ui_action",
    "ui_hotkey": "ui_action",
    "ui_scroll": "ui_action",
    "get_selection": "ui_action",
    # 子 Agent
    "dispatch_research": "subagent",
    "dispatch_subagents": "subagent",
    "await_subagents": "subagent",
    "control_subagent": "subagent",
    # 知识 / 记忆
    "read_note": "file_read",
    "search_papers": "web_search",
    "search_knowledge": "knowledge",
    "recall_summary": "recall",
    "recall_tool_result": "recall",
    "recall_memory": "memory",
    "save_memory": "memory",
    "update_profile": "memory",
    # 系统杂项
    "set_volume": "system",
    "toggle_notifications": "system",
    "notify": "system",
    "create_session": "system",
}

_MAX_FIELD = 60          # 单个字段值的展示上限
_MAX_SUMMARY = 200       # 整行摘要上限


def tool_category(tool: str) -> str:
    """工具名 → 大类；未知工具归入 ``other``。"""
    return _CATEGORY_BY_TOOL.get(tool or "", "other")


# ── 字段提取 ──────────────────────────────────────────────────────────────────

def _pick(data: Any, *keys: str) -> Any:
    """按 key 顺序取第一个非空值（大小写与嵌套不敏感的第一层查找）。"""
    if not isinstance(data, dict):
        return None
    lowered = {str(k).lower(): v for k, v in data.items()}
    for key in keys:
        val = lowered.get(key.lower())
        if val not in (None, "", [], {}):
            return val
    return None


def _short(value: Any, limit: int = _MAX_FIELD) -> str:
    """把任意值压成单行短文本。"""
    if isinstance(value, (dict, list)):
        text = json.dumps(value, ensure_ascii=False)
    else:
        text = str(value)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit] + "…"


def _display_path(path: Any, limit: int = 52) -> str:
    """长路径只留尾部（``…\\memory\\tool_summary.py``）。

    绝对路径动辄八九十字符，直接截断会把「行数 / 字符数」这些真正有用的
    信息挤掉——保留文件名的辨识度就够了。
    """
    text = str(path or "").strip()
    if len(text) <= limit:
        return text
    tail = text.replace("\\", "/").split("/")
    kept = tail[-1]
    for part in reversed(tail[:-1]):
        if len(part) + len(kept) + 1 > limit - 1:
            break
        kept = f"{part}/{kept}"
    return "…/" + kept


def _count_of(data: Any, *keys: str) -> int | None:
    """取列表长度（命中数 / 结果数 / 行数）。"""
    val = _pick(data, *keys)
    if isinstance(val, list):
        return len(val)
    if isinstance(val, str):
        return len(val.splitlines())
    return None


def _first_items(data: Any, *keys: str, n: int = 3) -> list[Any]:
    """取列表字段的前 n 项。"""
    val = _pick(data, *keys)
    return val[:n] if isinstance(val, list) else []


def _item_label(item: Any) -> str:
    """把一条检索结果压成「标题 — URL」或路径样式。"""
    if isinstance(item, dict):
        title = _pick(item, "title", "name", "path", "file", "url")
        url = _pick(item, "url", "link", "path")
        if title and url and str(title) != str(url):
            return f"{_short(title, 40)} — {_short(url, 60)}"
        return _short(title or url or item, 70)
    return _short(item, 70)


def _arg_text(arguments: Any, *keys: str, limit: int = _MAX_FIELD) -> str:
    """从工具参数里取字段（参数比结果更可靠，优先用）。"""
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except Exception:
            return _short(arguments, limit)
    return _short(_pick(arguments, *keys) or "", limit) if arguments else ""


# 参数占位里的"定位提示"键：只看能放进一行的定位信息。
# 长内容字段（content / body / text…）刻意不在其中——那正是要被压掉的部分。
_ARG_HINT_KEYS = (
    "path", "file", "src", "filename", "dst", "dir", "directory",
    "url", "link", "command", "cmd", "script", "name", "app", "process",
    "window", "target", "selector", "title", "query", "q", "keyword",
)


def argument_hint(arguments: Any, limit: int = 40) -> str:
    """从工具参数里取一个"定位提示"：路径 / 网址 / 命令 / 关键词。

    参数被占位替换后，模型仍能一眼看出"这次调用作用于什么"，不必为此
    发起一次召回。结果摘要并非每个大类都含输入侧信息（例如 web_search 的
    摘要是"命中 N 条：标题 — URL"，里面没有 query），此处补上。
    """
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except Exception:
            return _short(arguments, limit)
    if not isinstance(arguments, dict):
        return ""
    return _short(_pick(arguments, *_ARG_HINT_KEYS) or "", limit)


# ── 分类化失败原因 ────────────────────────────────────────────────────────────

def _failure_reason(error: Any, error_category: str | None) -> str:
    """失败原因：优先用已有分类，其次从分类器推断，最后退化为截断的原文。"""
    if error_category:
        return str(error_category)
    text = str(error or "")
    try:  # 复用既有分类器；任何异常都不影响摘要生成
        from agent_assistant.tools.failure_feedback import classify_tool_error

        for candidate in (text,):
            cat = classify_tool_error(candidate)
            if cat:
                return str(cat)
    except Exception:
        pass
    return _short(text, 80) or "未知原因"


# ── 主入口 ────────────────────────────────────────────────────────────────────

def summarize_tool_result(
    tool: str,
    arguments: Any = None,
    result_json: str | dict[str, Any] | None = None,
) -> str:
    """生成一行工具结果摘要：``ok=<bool> | <正文>``。

    ``result_json`` 是主循环写入 ``role="tool"`` 消息的那个 JSON 字符串
    （``ToolResult.to_model_str()`` 的产物）。任何解析失败都退化为兜底文案。
    """
    payload: dict[str, Any] = {}
    if isinstance(result_json, dict):
        payload = result_json
    elif isinstance(result_json, str) and result_json.strip():
        try:
            parsed = json.loads(result_json)
            if isinstance(parsed, dict):
                payload = parsed
        except Exception:
            payload = {"data": result_json}

    ok = bool(payload.get("ok", True))
    if not ok:
        reason = _failure_reason(payload.get("error"), payload.get("error_category"))
        return f"ok=false | {_short(tool, 30)} 失败：{reason}"[:_MAX_SUMMARY]

    data = payload.get("data", payload)
    body = _success_body(tool, arguments, data)
    return (f"ok=true | {body}" if body else f"ok=true | {_short(tool, 30)} 完成")[:_MAX_SUMMARY]


def _success_body(tool: str, arguments: Any, data: Any) -> str:
    """按工具大类生成成功摘要正文。"""
    cat = tool_category(tool)

    if cat == "file_read":
        path = _display_path(
            _arg_text(arguments, "path", "file", "src", "filename", limit=200)
            or _pick(data, "path", "file", "url", "title")
            or ""
        )
        lines = _count_of(data, "content", "text", "lines")
        size = _pick(data, "chars", "length")
        bits = [b for b in (path, f"{lines} 行" if lines else "", f"{size} 字符" if size else "") if b]
        return "读了 " + "，".join(bits) if bits else "读取完成"

    if cat == "file_write":
        path = _display_path(
            _arg_text(arguments, "path", "file", "src", "dst", "title", limit=200)
            or _pick(data, "path", "file")
            or ""
        )
        size = _pick(data, "chars", "bytes", "lines")
        return f"写入 {path}" + (f"，{size} 字符" if size else "") if path else "写入完成"

    if cat == "search_local":
        if tool == "list_files":
            # 列目录不是"命中"：报条数 + 目录/文件构成 + 路径，让模型一眼
            # 知道"这个目录大不大、有什么"。entries 是本工具的实际字段名。
            entries = _pick(data, "entries", "files", "items")
            n = len(entries) if isinstance(entries, list) else _count_of(
                data, "entries", "files", "items"
            )
            dirs = files = 0
            if isinstance(entries, list):
                dirs = sum(
                    1 for e in entries
                    if isinstance(e, dict) and str(e.get("type", "")).startswith("dir")
                )
                files = sum(
                    1 for e in entries
                    if isinstance(e, dict) and not str(e.get("type", "")).startswith("dir")
                )
            path = _display_path(
                _arg_text(arguments, "path", "dir", "directory")
                or str(_pick(data, "path") or "")
            )
            bits = [f"列出 {n} 项" if n is not None else "列出目录"]
            if dirs or files:
                bits.append(f"{dirs} 目录 / {files} 文件")
            if path:
                bits.append(path)
            return "，".join(bits)

        n = _count_of(data, "matches", "results", "files", "items")
        top = "；".join(_item_label(i) for i in _first_items(data, "matches", "results", "files", "items"))
        head = f"命中 {n} 处" if n is not None else "检索完成"
        return f"{head}，前三：{top}" if top else head

    if cat == "web_search":
        n = _count_of(data, "results", "items")
        top = "；".join(_item_label(i) for i in _first_items(data, "results", "items"))
        head = f"命中 {n} 条" if n is not None else "搜索完成"
        return f"{head}：{top}" if top else head

    if cat == "web_read":
        title = _short(_pick(data, "title", "heading") or "", 50)
        url = _short(_pick(data, "url", "final_url") or _arg_text(arguments, "url", "link"), 70)
        size = _pick(data, "chars", "length")
        bits = [b for b in (title, f"{size} 字符" if size else "", f"来自 {url}" if url else "") if b]
        return "，".join(bits) if bits else f"读取完成（{url}）" if url else "读取完成"

    if cat == "command":
        cmd = _arg_text(arguments, "command", "cmd", "script", limit=50)
        code = _pick(data, "exit_code", "code", "returncode")
        out_lines = _count_of(data, "stdout", "output")
        bits = [b for b in (f"执行 {cmd}" if cmd else "执行完成",
                            f"退出码 {code}" if code is not None else "",
                            f"输出 {out_lines} 行" if out_lines else "") if b]
        return "，".join(bits)

    if cat == "app":
        target = _arg_text(arguments, "name", "title", "app", "process") or _short(
            _pick(data, "name", "title", "window", "app") or "", 50
        )
        return f"{tool} {target}".strip() if target else f"{tool} 完成"

    if cat == "ui_snapshot":
        title = _short(_pick(data, "title", "window", "name") or _arg_text(arguments, "title", "window"), 50)
        n = _count_of(data, "controls", "elements", "items", "tree")
        bits = [b for b in (f"窗口「{title}」" if title else "界面快照",
                            f"{n} 个控件" if n is not None else "") if b]
        return "，".join(bits)

    if cat == "ui_action":
        target = _arg_text(arguments, "target", "name", "title", "selector")
        text = _arg_text(arguments, "text", limit=40)
        if text:
            return f"输入 {text}"
        return f"{tool} {target}".strip() if target else f"{tool} 完成"

    if cat == "recall":
        # Recall pulls archived text BACK into context, so the summary has to
        # say what came back — "检索完成" here would hide the one thing the
        # model needs to know (did I get the thing I asked for?).
        ref = _arg_text(arguments, "call_id", "name", "id", limit=40) or _short(
            _pick(data, "call_id", "name") or "", 40
        )
        archived = _pick(data, "archived")
        if isinstance(archived, list):
            return f"列出 {len(archived)} 个归档层"
        records = _pick(data, "records", "results", "hits")
        if isinstance(records, list):
            head = f"召回 {len(records)} 条记录"
            return f"{head}（{ref}）" if ref else head
        text = _pick(data, "text", "content")
        if isinstance(text, str):
            return f"召回 {ref or '内容'}，{len(text)} 字符"
        return f"召回 {ref or '内容'}"

    if cat == "knowledge":
        n = _count_of(data, "results", "chunks", "items")
        src = "；".join(_item_label(i) for i in _first_items(data, "results", "chunks", "items", n=2))
        head = f"命中 {n} 片段" if n is not None else "检索完成"
        return f"{head}，来自 {src}" if src else head

    if cat == "subagent":
        chars = _pick(data, "report_length", "full_report_length", "chars")
        n = _count_of(data, "findings")
        bits = [b for b in ("调研完成" if tool == "dispatch_research" else "子任务完成",
                            f"报告 {chars} 字符" if chars else "",
                            f"{n} 条来源" if n is not None else "") if b]
        return "，".join(bits)

    if cat == "memory":
        return f"{tool} 完成"

    # other / system：尽量给一点信息
    summary = _pick(data, "summary", "message", "text", "title", "path", "url")
    return _short(summary, 90) if summary else f"{tool} 完成"


__all__ = ["argument_hint", "summarize_tool_result", "tool_category"]
