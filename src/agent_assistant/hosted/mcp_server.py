"""Hosted MCP server entry point.

Runs the agent engine as an MCP stdio server that exposes a single coarse
tool, ``computer_task(instruction)``. The host (LianYu) delegates a whole
task in one call; internally the full agent loop + all tools drive it,
bubbling dangerous-op confirmations back to the host via elicitation.

Run:
    python -m agent_assistant.hosted.mcp_server        (from source)
    agent-assistant-mcp                                (installed script)

Protocol: newline-delimited JSON-RPC 2.0 on stdin/stdout. stdout is the
protocol channel ONLY — all logging goes to stderr + the file log.

Model config (base url / key / model) is read from env vars by
``config.Settings`` (DEEPSEEK_API_KEY / DEEPSEEK_BASE_URL / DEEPSEEK_MODEL),
so the host injects the user's provider credentials when spawning us — no
code change needed here.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
import uuid
from typing import Any

logger = logging.getLogger("agent_assistant.hosted")

PROTOCOL_VERSION = "2025-06-18"
_CONFIRM_TIMEOUT_S = 55.0  # < host's 45s dialog? host denies first; keep generous

# 描述会经工具桥进入恋语角色的上下文（后端截断上限 1024 字符），
# 是恋语模型「自己干还是委托」的唯一路由依据：能力要列全，不该来的要点名挡掉。
COMPUTER_TASK_TOOL = {
    "name": "computer_task",
    "description": (
        "把一个需要在【用户本机电脑】上完成的任务整体交给本地电脑助手执行，并返回结果。"
        "它能做："
        "① 打开/切换应用，并操作应用界面——点击、输入、快捷键、滚动"
        "（例如在网易云音乐里搜索某首歌并播放）；"
        "② 本地文件：查找/读取/写入/移动，按要求整理归档；"
        "③ 运行终端命令；查看系统状态（进程/内存/性能）、结束卡死的进程、调节音量、开关勿扰；"
        "④ 大任务可并行拆派（找文件＋整理＋操作应用同时进行）。"
        "传入 instruction 字段（一句自然语言任务，越具体越好），助手会自行规划多步骤完成；"
        "涉及删除、结束进程等危险操作时会先弹窗请用户确认。任务在本机执行，通常需要几十秒。"
        "用户改口换任务（换成另一首歌、换个应用）必须本轮立刻再调用；"
        "禁止只回复「我去换/稍等/让助手去」而不带 tool call。"
        "以下情况不要调用：单纯的联网搜索/查资料/深度调研（用你自己的联网与知识能力回答）；"
        "闲聊、情感陪伴、写作、翻译等不需要动电脑的请求。"
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "instruction": {
                "type": "string",
                "description": "要在用户电脑上完成的任务（自然语言，越具体越好）",
            }
        },
        "required": ["instruction"],
    },
    # 关键：标记为非危险，避免宿主对每次 computer_task 都预弹确认——
    # 细粒度危险操作在执行过程中通过 elicitation 单独确认。
    "annotations": {
        "title": "电脑任务",
        "readOnlyHint": False,
        "destructiveHint": False,
        "openWorldHint": True,
    },
}


def _setup_logging() -> None:
    """Stderr + file logging. NEVER attach a stdout handler (protocol channel)."""
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    if not any(getattr(h, "_hosted_stderr", False) for h in root.handlers):
        handler = logging.StreamHandler(sys.stderr)
        handler.setLevel(logging.INFO)
        handler.setFormatter(logging.Formatter(
            "%(asctime)s [%(name)s] %(levelname)s: %(message)s",
            datefmt="%H:%M:%S",
        ))
        handler._hosted_stderr = True  # type: ignore[attr-defined]
        root.addHandler(handler)
    # Best-effort: also mirror to the standard file log if available.
    try:
        from agent_assistant.config import settings

        settings.ensure_dirs()
        file_handler = logging.FileHandler(
            settings.logs_dir / "hosted-mcp.log", mode="a", encoding="utf-8"
        )
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(logging.Formatter(
            "%(asctime)s [%(name)s] %(levelname)s: %(message)s"
        ))
        root.addHandler(file_handler)
    except Exception:
        logger.debug("file logging unavailable", exc_info=True)
    from agent_assistant.main import quiet_third_party_loggers

    quiet_third_party_loggers()


# 进度解说里展示的关键参数（按优先级取第一个非空字符串）。
# purpose（run_command 的中文说明）最像人话；text/query/target 次之。
_PROGRESS_ARG_KEYS = (
    "purpose", "query", "text", "target", "name", "title",
    "goal", "path", "command", "keys", "title_pattern",
)
_PROGRESS_DETAIL_MAX = 24
_NARRATION_MAX = 80

# Outer models sometimes ignore the schema and send a raw sentence, or use
# task/query instead of instruction. 0.1.3 only read dict['instruction'] and
# bounced those calls in ~1s with no computer_task start log.
_INSTRUCTION_KEYS = ("instruction", "task", "query", "prompt", "input", "text")


def instruction_from_arguments(arguments: Any) -> str:
    """Extract the task text from tools/call arguments (dict, aliases, or string)."""
    if isinstance(arguments, str):
        text = arguments.strip()
        if text.startswith("{") and text.endswith("}"):
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, dict):
                return instruction_from_arguments(parsed)
        return text
    if isinstance(arguments, dict):
        for key in _INSTRUCTION_KEYS:
            value = arguments.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


def describe_tool_progress(name: str, arguments: Any) -> str:
    """One short Chinese status line for a tool call (shown as chat bubble)."""
    from agent_assistant.tools.labels import TOOL_LABELS_ZH

    label = TOOL_LABELS_ZH.get(name, name or "工具")
    args = arguments
    if isinstance(arguments, str):
        try:
            args = json.loads(arguments)
        except json.JSONDecodeError:
            args = {}
    detail = ""
    if isinstance(args, dict):
        for key in _PROGRESS_ARG_KEYS:
            value = args.get(key)
            if isinstance(value, str) and value.strip():
                detail = value.strip().splitlines()[0]
                break
    if len(detail) > _PROGRESS_DETAIL_MAX:
        detail = detail[:_PROGRESS_DETAIL_MAX] + "…"
    return f"正在{label}「{detail}」…" if detail else f"正在{label}…"


class _ProgressNarrator:
    """Turn agent-loop events into user-facing MCP progress messages.

    - ``response_chunk`` deltas are buffered and flushed as ONE narration line
      when the round's first tool_call (or the final response) arrives — the
      model's own milestone commentary, zero extra LLM cost. A narrated round
      suppresses the templated status line (no double-talk).
    - ``tool_call`` without narration yields a templated status line
      (「正在输入「大海」…」); pure-observation tools (ui_inspect) stay silent.
    - Failed ``tool_result`` yields a brief retry note; successes stay silent
      (the next milestone narration covers it).
    - ``response`` (interim progress summary / final text) forwards its first
      line — the final one is immediately superseded by the task result.
    """

    # Pure-observation tools: frequent and uninformative — never templated.
    _SILENT_TOOLS = frozenset({"ui_inspect"})

    def __init__(self, conn: "StdioConnection", progress_token: Any) -> None:
        self._conn = conn
        self._token = progress_token
        self._buf: list[str] = []

    def _send(self, text: str) -> None:
        text = (text or "").strip()
        if not text:
            return
        try:
            self._conn.send_progress(text, progress_token=self._token)
        except Exception:
            logger.debug("progress emit failed", exc_info=True)

    def _flush_narration(self) -> bool:
        """Send the buffered narration line (if any). True when one was sent."""
        text = "".join(self._buf).strip()
        self._buf.clear()
        if not text:
            return False
        self._send(text.splitlines()[0][:_NARRATION_MAX])
        return True

    def handle(self, event: "AgentEvent") -> None:
        try:
            if event.type == "thinking":
                # New round starting: drop narration orphaned by an aborted
                # stream so it can't surface one round late.
                self._buf.clear()
            elif event.type == "response_chunk" and isinstance(event.content, str):
                self._buf.append(event.content)
            elif event.type == "tool_call" and isinstance(event.content, dict):
                narrated = self._flush_narration()
                name = str(event.content.get("name") or "")
                if narrated or name in self._SILENT_TOOLS:
                    return
                self._send(describe_tool_progress(name, event.content.get("arguments")))
            elif event.type == "tool_result" and isinstance(event.content, dict):
                result = event.content.get("result")
                if isinstance(result, dict) and result.get("ok") is False:
                    self._send("这一步没成功，换个方式试试…")
            elif event.type == "response":
                self._buf.clear()
                text = str(event.content or "").strip()
                if text:
                    self._send(text.splitlines()[0][:_NARRATION_MAX])
        except Exception:
            logger.debug("progress narrate failed", exc_info=True)


class HostedMcpServer:
    """Single-tool MCP server wrapping the agent loop."""

    def __init__(self, connection) -> None:
        from .stdio_connection import StdioConnection

        self._conn: StdioConnection = connection
        self._initialized = False
        # rpc_id -> AgentLoop (active tasks), for cancellation
        self._active: dict[Any, Any] = {}
        self._active_lock = threading.Lock()

    # ---- request handlers ----

    def _handle_initialize(self, msg: dict[str, Any]) -> None:
        self._initialized = True
        self._conn.respond(msg["id"], {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "agent-assistant-hosted", "version": "1.0.0"},
        })

    def _handle_tools_list(self, msg: dict[str, Any]) -> None:
        self._conn.respond(msg["id"], {"tools": [COMPUTER_TASK_TOOL]})

    def _handle_tools_call(self, msg: dict[str, Any]) -> None:
        params = msg.get("params") or {}
        name = params.get("name")
        if name != "computer_task":
            self._conn.respond(msg["id"], {
                "content": [{"type": "text", "text": f"未知工具：{name}"}],
                "isError": True,
            })
            return
        arguments = params.get("arguments") if "arguments" in params else {}
        instruction = instruction_from_arguments(arguments)
        if not instruction:
            preview = ""
            keys = "-"
            if isinstance(arguments, dict):
                keys = ",".join(str(k) for k in arguments.keys()) or "-"
                preview = str(arguments)[:120]
            elif isinstance(arguments, str):
                preview = arguments[:120]
            else:
                preview = type(arguments).__name__
            logger.warning(
                "computer_task missing instruction: type=%s keys=%s preview=%s",
                type(arguments).__name__, keys, preview,
            )
            self._conn.respond(msg["id"], {
                "content": [{"type": "text", "text": "缺少 instruction 参数"}],
                "isError": True,
            })
            return
        progress_token = (params.get("_meta") or {}).get("progressToken")
        worker = threading.Thread(
            target=self._run_task,
            args=(msg["id"], instruction, progress_token),
            name=f"computer_task-{msg['id']}",
            daemon=True,
        )
        worker.start()

    def _run_task(self, rpc_id: Any, instruction: str, progress_token: Any) -> None:
        from agent_assistant.agent.loop import AgentLoop
        from agent_assistant.tools.sanitize import sanitize_error

        narrator = _ProgressNarrator(self._conn, progress_token)
        loop = AgentLoop(conversation_id=f"hosted-{uuid.uuid4().hex}", on_event=narrator.handle)
        with self._active_lock:
            self._active[rpc_id] = loop
        try:
            logger.info("computer_task start: %s", instruction[:120])
            result = loop.chat(instruction)
            self._conn.respond(rpc_id, {
                "content": [{"type": "text", "text": result or "（任务完成，无文本输出）"}],
                "isError": False,
            })
            logger.info("computer_task done: %s", rpc_id)
        except Exception as exc:  # never let a task crash take down the server
            logger.exception("computer_task failed")
            safe = sanitize_error(str(exc), context="computer_task", code=500)
            self._conn.respond(rpc_id, {
                "content": [{"type": "text", "text": f"任务执行失败：{safe.safe_message}"}],
                "isError": True,
            })
        finally:
            with self._active_lock:
                self._active.pop(rpc_id, None)

    def _handle_cancelled(self, params: dict[str, Any]) -> None:
        request_id = params.get("requestId")
        with self._active_lock:
            loops = (
                [self._active.get(request_id)] if request_id is not None
                else list(self._active.values())
            )
        for loop in loops:
            if loop is not None:
                try:
                    loop.request_cancel()
                except Exception:
                    logger.debug("cancel failed", exc_info=True)

    # ---- dispatch ----

    def handle_message(self, msg: dict[str, Any]) -> None:
        method = msg.get("method")
        if isinstance(method, str):
            self._dispatch_request(method, msg)
            return
        # No method → it's a response to one of our server→client requests.
        msg_id = msg.get("id")
        if msg_id is not None and self._conn.is_pending(msg_id):
            self._conn.resolve_response(msg_id, msg.get("result"), msg.get("error"))

    def _dispatch_request(self, method: str, msg: dict[str, Any]) -> None:
        has_id = "id" in msg and msg["id"] is not None
        try:
            if method == "initialize":
                self._handle_initialize(msg)
            elif method == "notifications/initialized":
                pass
            elif method == "tools/list":
                self._handle_tools_list(msg)
            elif method == "tools/call":
                self._handle_tools_call(msg)
            elif method == "ping":
                if has_id:
                    self._conn.respond(msg["id"], {})
            elif method == "notifications/cancelled":
                self._handle_cancelled(msg.get("params") or {})
            elif has_id:
                self._conn.respond_error(msg["id"], -32601, f"Method not found: {method}")
        except Exception:
            logger.exception("handler failed: %s", method)
            if has_id:
                self._conn.respond_error(msg["id"], -32603, "internal error")


def _stdin_lines(stdin):
    """Yield JSON-RPC lines without TextIO.reconfigure().

    The host writes ``initialize`` as soon as the process is spawned. Calling
    ``sys.stdin.reconfigure()`` after that can discard the already-buffered
    first line; the server then sits on stdin and the host times out.
    """
    buf = getattr(stdin, "buffer", None) if stdin is sys.stdin else None
    if buf is not None:
        while True:
            raw = buf.readline()
            if not raw:
                return
            yield raw.decode("utf-8", errors="replace")
        return
    yield from stdin


def serve(stdin=None, stdout=None) -> None:
    """Read newline-delimited JSON-RPC from stdin until EOF."""
    from .stdio_connection import StdioConnection

    stdin = stdin or sys.stdin
    out = stdout or sys.stdout

    write_lock_stream = out

    def write_line(line: str) -> None:
        write_lock_stream.write(line + "\n")
        write_lock_stream.flush()

    conn = StdioConnection(write_line)
    server = HostedMcpServer(conn)

    # Wire the confirm gate to the MCP client BEFORE any tool can execute.
    from agent_assistant.tools.confirm import confirm_gate

    from .bridge_confirm import BridgeConfirmProvider

    confirm_gate.provider = BridgeConfirmProvider(conn, timeout=_CONFIRM_TIMEOUT_S)

    logger.info("hosted MCP server ready (stdio)")
    try:
        for raw in _stdin_lines(stdin):
            line = raw.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                logger.warning("non-json stdin line ignored: %s", line[:200])
                continue
            server.handle_message(msg)
    finally:
        conn.fail_all_pending()
        logger.info("hosted MCP server stdin closed, exiting")


def _apply_hosted_runtime_defaults() -> None:
    """Latency-sensitive defaults for hosted mode (one computer_task per call).

    - Thinking OFF by default: UI-automation micro-steps don't need CoT; with
      it on, many rounds spend 3-8s generating reasoning AND the echoed
      reasoning_content bloats history every round. Opt back in with
      HOSTED_THINKING=1.
    - Larger in-turn memory budget: agentic UI turns legitimately carry a few
      control-tree snapshots. Desktop default is 12000; override with
      HOSTED_MEMORY_BUDGET.
    - Live narration ON: each tool round's content deltas are forwarded as MCP
      progress so the LianYu chat shows a live bubble while the task runs.
      Opt out with HOSTED_NARRATION=0.
    """
    from agent_assistant.config import settings

    thinking = os.environ.get("HOSTED_THINKING", "").strip().lower()
    settings.thinking_enabled = thinking in ("1", "true", "yes", "on")
    if not settings.thinking_enabled:
        settings.reasoning_effort = "off"

    narration = os.environ.get("HOSTED_NARRATION", "").strip().lower()
    settings.hosted_narration = narration not in ("0", "false", "no", "off")

    raw_budget = os.environ.get("HOSTED_MEMORY_BUDGET", "").strip()
    try:
        settings.memory_token_budget = max(2000, int(raw_budget)) if raw_budget else 12000
    except ValueError:
        settings.memory_token_budget = 12000

    logger.info(
        "hosted runtime: model=%s thinking=%s memory_budget=%d",
        settings.deepseek_model,
        settings.thinking_enabled,
        settings.memory_token_budget,
    )


def main() -> None:
    # utf-8 stdout only. Do NOT reconfigure stdin — the host may already have
    # written initialize into the pipe; reconfigure can drop that line.
    stdout = getattr(sys, "stdout", None)
    reconfigure = getattr(stdout, "reconfigure", None)
    if reconfigure:
        try:
            reconfigure(encoding="utf-8")
        except Exception:
            pass

    _setup_logging()
    _apply_hosted_runtime_defaults()

    from agent_assistant.tools.register import register_hosted_tools

    register_hosted_tools()
    serve()


if __name__ == "__main__":
    main()
