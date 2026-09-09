# AgentAssistant 工具详细 Spec (Tool Detailed Spec)

> 版本: v1.0  
> 日期: 2026-07-26  
> 关联: 见项目根 `README.md` 与 `docs/01`–`05` 各分层文档(features 见 `04-features.md`,架构/工具总览见 `03-architecture.md`)  
> 状态: 设计阶段(待审阅)  
> 说明: 工具 spec(name/description/parameters/side effects/failure behavior/idempotency)用英文供模型读取;章节导航和说明用中文便于人读。

---

## 字段定义 (Field Definitions)

每个工具含 6 个字段:

| 字段 | 含义 |
|---|---|
| **name** | 工具名(模型调用的标识) |
| **description** | 做什么 + 何时调用 + 何时别调用(别调用时该用哪个替代工具)+ 关键 caveat。模型路由主要靠这个字段 |
| **parameters** | 参数名 / 类型 / 必填或可选 / 含义 |
| **side effects** | 模型该看见的状态变化(影响 confirm/重试决策)。纯读写"none"。内部实现细节不写(按"副作用内聚"藏起来) |
| **failure behavior** | 出错返回什么(结构化错误,不抛异常),模型据此决定重试/换路/问用户 |
| **idempotency** | 可否安全重试 + 一句理由 |

**错误冒泡原则**: 所有工具失败都返回结构化错误 `{ok: false, error, code}`,不在工具内部吞掉。模型看到错误自己决定下一步。

---

## 1. 主 agent 工具(19 个)

### 1.1 `read_file`

- **description**: Read text content of a local file. **When to call**: view file content, check if a file exists, read config/code/notes, read a note for follow-up questions. **When NOT to call**: reading web pages → use `read_page`; grabbing screen-selected text → use `get_selection`; listing a directory → use `list_files`. Returns only the first 2000 lines by default for large files; use `offset` to paginate.
- **parameters**:
  - `path` (string, required): absolute path
  - `offset` (int, optional): starting line (1-indexed), default 1
  - `limit` (int, optional): number of lines, default 2000
- **side effects**: none (pure read)
- **failure behavior**: file not found → `{error: "file not found", code: 404}`; permission denied → `{error: "permission denied"}`; invalid path → error
- **idempotency**: yes (pure read, safe to retry on failure)

### 1.2 `write_file`

- **description**: Create a new file and write content. **When to call**: generate notes/config/code/reports as new files. **When NOT to call**: modifying an existing file → use `edit_file`; if the file already exists this tool errors (guiding you to `read_file` first then `edit_file`). Parent directories are auto-created.
- **parameters**:
  - `path` (string, required): absolute path
  - `content` (string, required): full content
- **side effects**: creates the file if it does not exist; refuses to overwrite if it exists
- **failure behavior**: file exists → `{error: "file exists, use edit_file to modify"}`; permission/invalid path → error
- **idempotency**: no (fails if file exists); but failure is safe — never overwrites

### 1.3 `edit_file`

- **description**: Replace an exact string in an existing file. **When to call**: modify local content of code/config/notes. **You must `read_file` first** before calling. `old_string` must match exactly (including indentation/spaces). **When NOT to call**: full rewrite → back up then `write_file`; creating a new file → use `write_file`.
- **parameters**:
  - `path` (string, required)
  - `old_string` (string, required): exact text to replace
  - `new_string` (string, required): replacement content
  - `replaceAll` (bool, optional): replace all matches, default false
- **side effects**: modifies file content
- **failure behavior**: `old_string` not found → `{error: "oldString not found"}`; multiple matches without `replaceAll` → `{error: "multiple matches, provide more context"}`; not read first → warning
- **idempotency**: no (after first call `old_string` is gone, second call fails — safe)

### 1.4 `list_files`

- **description**: List files/subdirectories in a directory. **When to call**: scanning before file classification, finding files, understanding directory structure. **When NOT to call**: reading file content → use `read_file`. Returns names only, not content.
- **parameters**:
  - `path` (string, required): absolute directory path
  - `pattern` (string, optional): glob e.g. `*.txt`
- **side effects**: none
- **failure behavior**: directory not found → error; permission denied → error
- **idempotency**: yes

### 1.5 `move_file`

- **description**: Move or rename a file. **When to call**: file classification, organizing desktop/downloads. Source must exist; if target exists this refuses to overwrite. **When NOT to call**: modifying file content → use `edit_file`. Call per-file for batch classification. **Dangerous paths (system directories) require user confirmation at framework layer.**
- **parameters**:
  - `src` (string, required): source absolute path
  - `dst` (string, required): destination absolute path
- **side effects**: changes file location
- **failure behavior**: src not found → `{error: "src not found"}`; dst exists → `{error: "dst exists, won't overwrite"}`; cross-drive permission → error; dangerous path → refused, requires confirmation
- **idempotency**: no (after first call src is gone, second call fails)

### 1.6 `run_command`

- **description**: Execute a command in a PowerShell terminal. **When to call**: install software, run scripts, git operations, system queries. **Has a whitelist**: dangerous commands (delete/format/registry edits/process kills) require user confirmation. **When NOT to call**: reading files → use `read_file`; launching apps → use `launch_app`; grabbing selection → use `get_selection`. Default timeout 120s.
- **parameters**:
  - `command` (string, required): PowerShell command
  - `workdir` (string, optional): working directory
  - `timeout` (int, optional): milliseconds, default 120000
- **side effects**: arbitrary (depends on command; may modify files/system/processes)
- **failure behavior**: non-zero exit → returns stderr + exit code; timeout → `{error: "timeout"}`; dangerous command not confirmed → `{error: "needs confirmation"}`
- **idempotency**: depends on command (default no; agent decides whether to retry)

### 1.7 `launch_app`

- **description**: Launch an app or focus an already-running instance. **When to call**: user says "open X" / "start Y". The agent decides whether it's UWP (protocol URI) / exe / a game with a launcher. **If the window already exists, it focuses instead of relaunching** (idempotent). **When NOT to call**: switching to an already-open window → use `focus_window` (lighter); running a CLI program → use `run_command`.
- **parameters**:
  - `name` (string, required): app name or alias (e.g. "逆战" / "Cursor")
- **side effects**: starts a process (if not running); focuses window (if running)
- **failure behavior**: app not in registry → `{error: "app not registered, provide exe path or protocol URI"}`; launch failed → error
- **idempotency**: yes (focuses if running, launches if not — safe to call repeatedly)

### 1.8 `focus_window`

- **description**: Bring a specified window to the foreground. **When to call**: switch to an already-open app, focus a specific window in a scene mode. Matches by window title pattern. **When NOT to call**: app not running → use `launch_app` (which already includes focus logic, prefer it); reading window content is not supported.
- **parameters**:
  - `title_pattern` (string, required): title substring or regex
- **side effects**: changes the foreground window
- **failure behavior**: no matching window → `{error: "no matching window, try launch_app"}`; permission → error
- **idempotency**: yes

### 1.9 `get_selection`

- **description**: Get the text the user has selected in the currently focused app. **When to call**: entry point for right-click toolbox (before translate/summarize/ask), grabbing content for cross-app actions. Prefers UI Automation (clean); falls back to Ctrl+C reading the clipboard if that fails. **When NOT to call**: reading files → use `read_file`; reading web pages → use `read_page`.
- **parameters**: none
- **side effects**: (external) none; (internal) temporarily uses the clipboard and auto-restores it on exit — **invisible to the model**, hidden per the "side-effect encapsulation" principle
- **failure behavior**: no selection → `{error: "no selection"}`; focused app unsupported → `{error: "unsupported app, user can Ctrl+C manually"}`; empty text treated as no selection
- **idempotency**: yes (pure read of selected text)

### 1.10 `save_note`

- **description**: Save content as a local Markdown note with title/tag/timestamp. **When to call**: the unified wrap-up action for all features — translate/summarize/ask/deep-research/morning-briefing results can all call this. Notes go to a unified directory, later connected to RAG. **When NOT to call**: modifying an existing note → use `edit_file`; storing research intermediate findings (sub-agent) also uses this tool. **Available to: main agent + sub-agent** (main agent for wrap-up, sub-agent for intermediate findings).
- **parameters**:
  - `content` (string, required)
  - `title` (string, required)
  - `tag` (string, optional)
- **side effects**: creates a note file (writes to local notes directory)
- **failure behavior**: write failed → error; illegal title (path chars) → auto-sanitized
- **idempotency**: no (repeated calls create multiple same-named notes); agent should check before calling

### 1.11 `kill_process`

- **description**: Terminate a specified process. **When to call**: free memory during perf diagnosis, close background apps in scene modes. **Requires user confirmation before killing** (killing a system process can blue-screen). System-critical processes (csrss/wininit etc.) are blacklisted and refused. **When NOT to call**: prefer an app's own exit; do not kill the assistant itself.
- **parameters**:
  - `pid` (int, optional): process ID
  - `name` (string, optional): process name (one of pid/name)
- **side effects**: terminates a process
- **failure behavior**: process not found → `{error: "process not found"}`; blacklisted → `{error: "protected system process"}`; not confirmed → `{error: "needs confirmation"}`; no pid/name → error
- **idempotency**: yes (killing a dead process is a no-op)

### 1.12 `set_volume`

- **description**: Set the system master volume (0-100). **When to call**: scene modes (game mode adjust volume, work mode mute). **When NOT to call**: per-app volume not supported yet; reading current volume not provided yet (may add later).
- **parameters**:
  - `level` (int, required): 0-100
- **side effects**: changes system volume
- **failure behavior**: out of range → `{error: "level must be 0-100"}`; system API failed → error
- **idempotency**: yes

### 1.13 `toggle_notifications`

- **description**: Toggle Windows notifications (focus-assist / do-not-disturb). **When to call**: scene modes (mute notifications during games/meetings). `on` = enable DND, `off` = restore. **When NOT to call**: per-app notifications not supported.
- **parameters**:
  - `state` (string, required): `"on"` / `"off"`
- **side effects**: changes system notification settings
- **failure behavior**: system API failed → error; invalid state → error
- **idempotency**: yes

### 1.14 `perf_snapshot`

- **description**: Take a snapshot of current CPU/memory/processes. **When to call**: during perf anomaly diagnosis — after getting the snapshot, the agent explains in natural language and suggests processes to kill. **When NOT to call**: continuous monitoring is the job of the background script, not the agent calling this in a loop; single-process detail not supported.
- **parameters**:
  - `top_n` (int, optional): return top N memory-consuming processes, default 10
- **side effects**: none (reads perf counters only)
- **failure behavior**: read failed → error
- **idempotency**: yes

### 1.15 `create_session`

- **description**: Create a new conversation session and inject initial context. **When to call**: morning briefing "ask about this report" (injects scenario + full report), starting a new topic in continuous-call mode, user says "open a new topic". Returns a new session id. **When NOT to call**: continuing in the current session — just reply directly; modifying existing session context not supported.
- **parameters**:
  - `context` (string, required): system context to inject (scenario description + background material)
  - `topic` (string, optional): topic label
- **side effects**: creates session state (memory / optionally persisted)
- **failure behavior**: creation failed → error
- **idempotency**: no (creates a new session each time)

### 1.16 `notify`

- **description**: Pop a Windows toast notification. **When to call**: morning briefing popup (summary + buttons), long-task completion reminders. Can carry action buttons that trigger preset callbacks on click (e.g. "preview report" → calls `launch_app` to open the .md). **When NOT to call**: for simple alerts in voice mode use TTS to speak; notifications are not a conversation channel.
- **parameters**:
  - `title` (string, required)
  - `body` (string, required)
  - `actions` (list, optional): `[{label, callback_name}]`, callbacks are preset in the framework layer
- **side effects**: shows a notification (may trigger user click → callback)
- **failure behavior**: notification API failed → error; actions callback not registered → warning but not blocking
- **idempotency**: yes (popping the same notification repeatedly is harmless but annoying — agent should avoid)

### 1.17 `dispatch_research`

- **description**: Dispatch the deep-research sub-agent for questions that need "multi-round search + synthesis + a report". **When to call**: user says "research the state of X" / "produce a report" / "deeply investigate X". Pass goal + context, returns the sub-agent's summarized report. **When NOT to call**: single-shot query/read — the main agent calls `web_search`/`read_page` itself; translate/summarize/Q&A are model-native abilities, don't use this tool. The sub-agent has an independent token/turn budget and trips a breaker if exceeded.
- **parameters**:
  - `goal` (string, required): research goal
  - `context` (string, optional): background material (e.g. morning briefing's "new terms + hot GitHub repos")
- **side effects**: spawns a sub-agent (consumes tokens, has a budget cap); does not directly modify local state
- **failure behavior**: sub-agent budget exhausted → returns partial results + `{warning: "budget exhausted, partial"}`; sub-agent failed → `{error: "subagent failed"}`
- **idempotency**: no (each call re-runs the research, results may differ); retry not recommended — change the goal or switch tools

### 1.18 `web_search`

- **description**: Single-shot web search (SearXNG local aggregation, falls back to Tavily on failure). **When to call**: "look up X" / "what does X mean" / "today's news" — single queries. Returns title + URL + snippet list. **When NOT to call**: multi-round search + synthesis + a report → use `dispatch_research`, don't loop `web_search` yourself; reading a specific page → use `read_page`. **Available to: main agent + sub-agent** (main: single-shot, sub-agent: iterative).
- **parameters**:
  - `query` (string, required)
- **side effects**: none (reads external API only)
- **failure behavior**: SearXNG down → auto-fallback to Tavily; both fail → `{error: "all search backends failed"}`
- **idempotency**: yes (same query returns similar results, safe to retry on failure)

### 1.19 `read_page`

- **description**: Read the main text of a single web page (trafilatura auto-extracts main content, strips nav/ads/comments). **When to call**: "summarize this article https://..." / "read this link". Returns body text (truncated if too long). **When NOT to call**: deep multi-page research → use `dispatch_research`; searching keywords → use `web_search`. **Available to: main agent + sub-agent** (main: single page, sub-agent: iterative).
- **parameters**:
  - `url` (string, required)
- **side effects**: none (pure read)
- **failure behavior**: invalid URL / fetch failed → `{error: "fetch failed"}`; no extractable body → returns HTML fragment + `{warning: "extraction low confidence"}`
- **idempotency**: yes

---

## 2. 深度研究子 agent 专用工具(1 个)

### 2.1 `extract_content`

- **description**: Extract a specific part of a web page by CSS selector. **When to call** (sub-agent): when `read_page` auto-extraction misses the target block (a table/list/specific section), use a selector to specify it manually. **When NOT to call**: full page body → use `read_page`; the main agent does not call this directly (single-page `read_page` is enough; precise extraction is a sub-agent research scenario).
- **parameters**:
  - `url` (string, required)
  - `selector` (string, required): CSS selector
- **side effects**: none (pure read)
- **failure behavior**: selector no match → `{ok: true, content: ""}` (empty result, not an error); fetch failed → `{error: "fetch failed"}`
- **idempotency**: yes

---

## 3. 共享工具说明 (Shared Tools)

| 工具 | 主 agent 用法 | 子 agent 用法 |
|---|---|---|
| `web_search` | 单次查询(1-2 次调用) | 迭代搜索(多轮,配合 `read_page`/`extract_content`) |
| `read_page` | 单页阅读("总结这个链接") | 多页阅读(配合搜索循环) |
| `save_note` | 收尾存结果(翻译/摘要/询问/报告) | 存中间发现("在 HN 找到 X 定义") |

同一实现,两边注册,用法由各自 prompt 引导,不写死。

---

## 4. 非工具清单 (Non-Tools)

| 类别 | 内容 | 不工具化的原因 |
|---|---|---|
| 模型原生能力 | 翻译 / 摘要 / 问答 / 写作 / 推理 / 代码生成 | 模型本身就会,工具化=降级成流水线 |
| 管道 | STT(sherpa-onnx+SenseVoice)/ TTS(Edge-TTS)/ VAD(planned phase10)/ turn-taking / barge-in | 是 IO 层和后台组件,agent 只看文本,不调这些 |
| 后台触发器 | 热键监听 / 右键菜单 hook / 性能监控脚本 / 登录延迟触发器 | 后台守护进程,触发后才唤醒 agent,不是 agent 工具 |

---

## 5. 工具分布速查 (Tool Distribution)

| 工具数 | 归属 |
|---|---|
| 19 | 主 agent(含 3 个共享) |
| 1 | 子 agent 专用(`extract_content`) |
| 3 | 共享(`web_search`/`read_page`/`save_note`) |
| 0 | 新增子 agent(仅 1 个深度研究子 agent) |

---

## 6. 下一步 (Next Steps)

1. 实测 deepseek-v4-pro 的 function calling 成熟度(用本 spec 的工具描述跑几个用例)
2. 进入 `writing-plans`:按开发顺序(打开应用 → 右键大全 → ...)拆实现任务
3. TDD: 每个工具先写测试(含 side effects/failure behavior/idempotency 的验证用例)再写实现
4. 代码审查: 每个工具完成后过 `requesting-code-review`
