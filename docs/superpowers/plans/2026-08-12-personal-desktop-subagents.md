# Personal Desktop Subagents Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build Cursor-style, independently running Explorer, Researcher, Operator, and Organizer subagents for the AgentAssistant personal desktop assistant.

**Architecture:** Add an in-process `SubagentManager` with persistent task state, task-local runtimes, event-driven waiting, a four-slot scheduler, and global desktop/filesystem resource locks. Keep the existing research loop through an adapter, route all frontend updates by `task_id`, and add Explorer, Operator, and Organizer incrementally behind fixed tool allowlists.

**Tech Stack:** Python 3.11, dataclasses, `sqlite3`, `threading`, `concurrent.futures`, OpenAI-compatible DeepSeek client, pytest, React 18, TypeScript, Zustand-like external store, Tailwind CSS, pywebview.

**Source spec:** `docs/superpowers/specs/2026-08-12-personal-desktop-subagents-design.md`

**Repository note:** This workspace is not currently a Git repository. Do not create commits; use the verification checkpoints below.

---

## File Structure

Backend files to create:

- `src/agent_assistant/subagents/__init__.py` — public exports.
- `src/agent_assistant/subagents/models.py` — roles, statuses, task/event/result records.
- `src/agent_assistant/subagents/context.py` — parent turn and task execution context.
- `src/agent_assistant/subagents/archive.py` — redacted L0/L1/checkpoint artifacts.
- `src/agent_assistant/subagents/store.py` — parameterized SQLite persistence.
- `src/agent_assistant/subagents/profiles.py` — fixed role prompts and tool allowlists.
- `src/agent_assistant/subagents/resources.py` — desktop/filesystem resource ownership.
- `src/agent_assistant/subagents/runtime.py` — generic task-focused LLM/tool loop.
- `src/agent_assistant/subagents/manager.py` — scheduling, control, waiting, recovery.
- `src/agent_assistant/subagents/service.py` — lazy process-wide manager wiring.
- `src/agent_assistant/subagents/runners/__init__.py` — runner exports.
- `src/agent_assistant/subagents/runners/researcher.py` — existing research-loop adapter.
- `src/agent_assistant/subagents/runners/explorer.py` — local exploration adapter.
- `src/agent_assistant/subagents/runners/operator.py` — desktop operation adapter.
- `src/agent_assistant/subagents/runners/organizer.py` — two-phase organization flow.
- `src/agent_assistant/tools/subagent_tools.py` — dispatch/await/control tools.
- `src/agent_assistant/tools/search_local_files.py` — bounded jail-aware local search.

Backend files to modify:

- `src/agent_assistant/config.py` — task database/run paths and concurrency settings.
- `src/agent_assistant/agent/loop.py` — bind parent turn context around tool execution.
- `src/agent_assistant/agent/pool.py` — pass conversation and turn IDs.
- `src/agent_assistant/tools/base.py` — support nested array/object tool schemas.
- `src/agent_assistant/tools/registry.py` — resource guard around protected tools.
- `src/agent_assistant/tools/register.py` — register orchestration and local-search tools.
- `src/agent_assistant/tools/research.py` — injected task cancellation/progress callbacks.
- `src/agent_assistant/agent/prompt.py` — personal-assistant delegation guidance.
- `src/agent_assistant/tools/labels.py` — Chinese orchestration labels.
- `src/agent_assistant/ui/launch.py` — start manager and route task events.
- `src/agent_assistant/ui/bridge.py` — init hydration and task control APIs.

Frontend files to create:

- `frontend/src/components/SubagentList.tsx` — Cursor-style task rows and timelines.
- `frontend/src/components/OperatorControlBar.tsx` — persistent desktop-control banner.
- `frontend/src/components/OrganizerApproval.tsx` — manifest review and decision card.

Frontend files to modify:

- `frontend/src/types.ts` — typed tasks, results, events, and bridge APIs.
- `frontend/src/lib/store.ts` — task maps and sequence-safe reducers.
- `frontend/src/components/ChatMessage.tsx` — render generic task groups.
- `frontend/src/App.tsx` — mount the Operator control banner.
- `frontend/src/lib/bridge.ts` — frontend task-control helpers.

Test files to create:

- `tests/test_subagent_models.py`
- `tests/test_subagent_store.py`
- `tests/test_subagent_resources.py`
- `tests/test_subagent_manager.py`
- `tests/test_subagent_tools.py`
- `tests/test_subagent_researcher.py`
- `tests/test_search_local_files.py`
- `tests/test_subagent_explorer.py`
- `tests/test_subagent_operator.py`
- `tests/test_subagent_organizer.py`
- `tests/test_subagent_bridge.py`
- `tests/test_subagent_security.py`

---

## Milestone A — Core Manager, Researcher Compatibility, and Generic UI

### Task 1: Define Stable Task Contracts

**Files:**
- Create: `src/agent_assistant/subagents/__init__.py`
- Create: `src/agent_assistant/subagents/models.py`
- Test: `tests/test_subagent_models.py`

- [ ] **Step 1: Write failing model tests**

```python
from agent_assistant.subagents.models import (
    SubagentEvent,
    SubagentResult,
    SubagentRole,
    SubagentSpec,
    SubagentStatus,
)


def test_spec_validates_fixed_role_and_required_text():
    spec = SubagentSpec.create(
        conversation_id="conv-1",
        parent_turn_id="turn-1",
        role="explorer",
        title="查找考研资料",
        goal="在 Documents 中查找考研资料",
    )
    assert spec.role is SubagentRole.EXPLORER
    assert spec.status is SubagentStatus.CREATED
    assert spec.task_id


def test_result_contains_control_plane_fields():
    result = SubagentResult.completed(
        task_id="task-1",
        role=SubagentRole.EXPLORER,
        attempt=1,
        summary="找到 3 个文件",
        output="三个文件均位于 Documents",
        evidence=[{"type": "file", "ref": "C:/Users/u/Documents/a.pdf"}],
    )
    payload = result.to_dict()
    assert payload["status"] == "completed"
    assert payload["attempt"] == 1
    assert payload["partial_result"] is None
    assert "error_category" in payload


def test_event_sequence_is_serialized():
    event = SubagentEvent(
        event_id="e1",
        conversation_id="c1",
        parent_turn_id="p1",
        task_id="t1",
        sequence=2,
        role=SubagentRole.RESEARCHER,
        type="activity",
        payload={"label": "读取网页"},
    )
    assert event.to_dict()["sequence"] == 2
```

- [ ] **Step 2: Verify the tests fail**

Run: `python -m pytest tests/test_subagent_models.py -q`

Expected: import failure for `agent_assistant.subagents.models`.

- [ ] **Step 3: Implement enums and dataclasses**

Implement these exact public types:

```python
class SubagentRole(StrEnum):
    EXPLORER = "explorer"
    RESEARCHER = "researcher"
    OPERATOR = "operator"
    ORGANIZER = "organizer"


class SubagentStatus(StrEnum):
    CREATED = "created"
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_USER = "waiting_user"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"


TERMINAL_STATUSES = {
    SubagentStatus.COMPLETED,
    SubagentStatus.FAILED,
    SubagentStatus.CANCELLED,
}
```

`SubagentSpec.create()` must strip and validate title/goal, parse the role enum,
generate a 12-character UUID-derived task ID, initialize attempt `1`, and store
timestamps in UTC ISO format. `SubagentResult.to_dict()` must always include
`task_id`, `role`, `status`, `attempt`, `summary`, `output`, `artifacts`,
`evidence`, `partial_result`, `next_action`, `stats`, `error_category`,
`error`, `l0_raw`, and `l1_trace`.

- [ ] **Step 4: Run the model tests**

Run: `python -m pytest tests/test_subagent_models.py -q`

Expected: all tests pass.

### Task 2: Bind Conversation and Turn Context

**Files:**
- Create: `src/agent_assistant/subagents/context.py`
- Modify: `src/agent_assistant/agent/loop.py`
- Modify: `src/agent_assistant/agent/pool.py`
- Modify: `src/agent_assistant/ui/bridge.py`
- Test: `tests/test_agent_pool.py`
- Test: `tests/test_subagent_tools.py`

- [ ] **Step 1: Add failing context propagation tests**

```python
def test_pool_passes_conversation_and_turn_id(monkeypatch):
    seen = {}

    def fake_chat(self, text, *, turn_id=None):
        seen["conversation_id"] = self.conversation_id
        seen["turn_id"] = turn_id
        return "ok"

    monkeypatch.setattr(AgentLoop, "chat", fake_chat)
    pool = AgentPool()
    assert pool.chat("conv-a", "hello", turn_id="msg-1") == "ok"
    assert seen == {"conversation_id": "conv-a", "turn_id": "msg-1"}
```

Also test that `current_execution_context()` is available inside a function
run through `asyncio.to_thread`.

- [ ] **Step 2: Verify the tests fail**

Run: `python -m pytest tests/test_agent_pool.py tests/test_subagent_tools.py -q`

Expected: `AgentLoop` lacks `conversation_id` and `turn_id` arguments.

- [ ] **Step 3: Implement task-safe context variables**

Create:

```python
@dataclass(frozen=True)
class AgentExecutionContext:
    conversation_id: str
    parent_turn_id: str
    owner_id: str
    cancel_event: threading.Event


_CURRENT: ContextVar[AgentExecutionContext | None] = ContextVar(
    "agent_execution_context", default=None
)
```

Provide `bind_execution_context(context)` and `current_execution_context()`.
Add optional `conversation_id` to `AgentLoop.__init__`, optional `turn_id` to
`chat()`/`achat()`, and bind the context for the complete turn. Update
`AgentPool.ensure()` and `AgentPool.chat()` to construct loops with the
conversation ID. Capture the ID returned by `ui_store.add_message()` in
`ApiBridge.send_message()` and pass it as `turn_id` through
`_process_message()` and `chat_in_conversation()`.

- [ ] **Step 4: Run context and pool tests**

Run: `python -m pytest tests/test_agent_pool.py tests/test_subagent_tools.py -q`

Expected: all tests pass.

### Task 3: Persist Tasks and Redacted Artifacts

**Files:**
- Modify: `src/agent_assistant/config.py`
- Create: `src/agent_assistant/subagents/archive.py`
- Create: `src/agent_assistant/subagents/store.py`
- Test: `tests/test_subagent_store.py`
- Test: `tests/test_subagent_security.py`

- [ ] **Step 1: Write failing persistence tests**

Test:

```python
def test_store_round_trip_and_sequence(tmp_path):
    store = SubagentStore(tmp_path / "subagents.db")
    task = make_spec(task_id="task-1")
    store.create_task(task)
    assert store.get_task("task-1").title == task.title
    assert store.next_sequence("task-1") == 1
    assert store.next_sequence("task-1") == 2


def test_recovery_marks_every_nonterminal_task_interrupted(tmp_path):
    store = SubagentStore(tmp_path / "subagents.db")
    for status in ("queued", "running", "waiting_user", "paused"):
        task = make_spec(task_id=f"t-{status}", status=status)
        store.create_task(task)
    recovered = store.mark_nonterminal_interrupted()
    assert {task.previous_status for task in recovered} == {
        "queued", "running", "waiting_user", "paused"
    }


def test_archive_redacts_secret_arguments(tmp_path):
    archive = TaskArchive(tmp_path, "task-1")
    archive.log_raw(
        tool="ui_type",
        arguments={"text": "sk-secret-value"},
        result={"ok": True},
    )
    assert "sk-secret-value" not in archive.raw_path.read_text("utf-8")
```

- [ ] **Step 2: Verify the tests fail**

Run: `python -m pytest tests/test_subagent_store.py tests/test_subagent_security.py -q`

Expected: store/archive imports fail.

- [ ] **Step 3: Implement storage**

Add settings:

```python
subagent_max_concurrency: int = 4

@property
def subagent_runs_dir(self) -> Path:
    return self.data_dir / "subagent_runs"

@property
def subagent_db_path(self) -> Path:
    return self.data_dir / "subagents.db"
```

Create parameterized SQLite tables `subagent_tasks` and `subagent_events`.
Use one `threading.RLock`, `check_same_thread=False`, and `?` placeholders for
every value. `TaskArchive` must create `raw.jsonl`, `trace.jsonl`,
`checkpoint.json`, and `output.md` under
`settings.subagent_runs_dir / task_id`. Redact key names matching
`password|passwd|secret|token|api_key|authorization|cookie`; redact the whole
`ui_type.text` value; sanitize exceptions before writing trace summaries.

- [ ] **Step 4: Run persistence and security tests**

Run: `python -m pytest tests/test_subagent_store.py tests/test_subagent_security.py -q`

Expected: all tests pass.

### Task 4: Add Fixed Role Profiles and Nested Tool Schemas

**Files:**
- Create: `src/agent_assistant/subagents/profiles.py`
- Modify: `src/agent_assistant/tools/base.py`
- Test: `tests/test_subagent_models.py`
- Test: `tests/test_tools.py`

- [ ] **Step 1: Add failing profile/schema tests**

```python
def test_profiles_have_exact_first_release_allowlists():
    assert profile_for("explorer").tools == frozenset({
        "list_files", "read_file", "search_local_files",
        "search_knowledge", "read_attached_source",
    })
    assert profile_for("researcher").tools == frozenset({
        "web_search", "read_page", "extract_content", "save_note",
    })
    assert profile_for("operator").tools == frozenset({
        "launch_app", "focus_window", "ui_inspect", "ui_click",
        "ui_type", "ui_hotkey", "ui_scroll",
    })
    assert profile_for("organizer").tools == frozenset({
        "list_files", "read_file", "search_local_files",
        "search_knowledge", "read_attached_source", "move_file", "save_note",
    })


def test_tool_parameter_supports_array_item_schema():
    parameter = ToolParameter(
        name="tasks",
        type="array",
        description="Tasks",
        schema={"type": "array", "items": {"type": "object"}},
    )
    assert parameter.schema["items"]["type"] == "object"
```

- [ ] **Step 2: Verify the tests fail**

Run: `python -m pytest tests/test_subagent_models.py tests/test_tools.py -q`

Expected: missing profile module and `schema` field.

- [ ] **Step 3: Implement profiles and schema override**

Add optional `schema: dict[str, Any] | None = None` to `ToolParameter`.
`Tool.to_openai_schema()` must start from `dict(p.schema)` when present, then
set the human description without discarding nested `items` or `properties`.

Define immutable `RoleProfile` records with exact allowlists, task-focused
system prompts, max-turn budgets, and whether the role needs the desktop or
filesystem resource. Researcher may additionally receive `read_local_source`
only when the user attached local sources, and `search_knowledge` only when the
knowledge base was enabled for that turn; these conditional additions are
explicit inputs, not model-selected privilege escalation.

- [ ] **Step 4: Run profile and existing tool-schema tests**

Run: `python -m pytest tests/test_subagent_models.py tests/test_tools.py -q`

Expected: all tests pass.

### Task 5: Implement Resource Ownership

**Files:**
- Create: `src/agent_assistant/subagents/resources.py`
- Modify: `src/agent_assistant/tools/registry.py`
- Test: `tests/test_subagent_resources.py`
- Test: `tests/test_permission.py`

- [ ] **Step 1: Write failing ownership tests**

Cover:

```python
def test_only_one_desktop_owner():
    resources = ResourceCoordinator()
    first = resources.try_acquire("desktop", "task-a")
    assert first is not None
    assert resources.try_acquire("desktop", "task-b") is None
    first.release()
    assert resources.try_acquire("desktop", "task-b") is not None


def test_same_owner_can_execute_guarded_tool():
    resources = ResourceCoordinator()
    lease = resources.try_acquire("desktop", "task-a")
    with resources.guard_tool("ui_click", "task-a"):
        pass
    lease.release()


def test_main_ui_tool_fails_when_operator_owns_desktop(monkeypatch):
    # Expected ToolResult.error_category == "resource_busy".
```

- [ ] **Step 2: Verify the tests fail**

Run: `python -m pytest tests/test_subagent_resources.py -q`

Expected: resource module missing.

- [ ] **Step 3: Implement coordinator and registry guard**

Protect:

```python
DESKTOP_TOOLS = {
    "launch_app", "focus_window", "ui_inspect", "ui_click",
    "ui_type", "ui_hotkey", "ui_scroll",
}
FILESYSTEM_WRITE_TOOLS = {"move_file", "write_file", "edit_file", "save_note"}
```

Track owner IDs under a mutex instead of relying on `threading.Lock` ownership.
The guard must allow an owner that already holds the resource, try-acquire and
release for an unowned main-agent call, and raise `ResourceBusy` otherwise.
`ToolRegistry.execute()` must convert `ResourceBusy` to a sanitized
`ToolResult.failure(..., error_category="resource_busy")` before calling the
tool. Permission guardrails still run first.

- [ ] **Step 4: Run resource and permission tests**

Run: `python -m pytest tests/test_subagent_resources.py tests/test_permission.py tests/test_tool_permission_overrides.py -q`

Expected: all tests pass.

### Task 6: Implement the Scheduler and Event-Driven Await

**Files:**
- Create: `src/agent_assistant/subagents/manager.py`
- Create: `src/agent_assistant/subagents/service.py`
- Test: `tests/test_subagent_manager.py`

- [ ] **Step 1: Write failing manager tests with an injected fake runner**

Tests must prove:

```python
def test_dispatch_persists_before_queueing(manager, store):
    ids = manager.dispatch([make_spec(task_id="t1")])
    assert ids == ["t1"]
    assert store.get_task("t1").status.value in {"queued", "running"}


def test_four_run_concurrently_and_fifth_waits():
    # Barrier-controlled fake runners record maximum simultaneous calls.
    assert observed_max == 4


def test_await_uses_condition_not_polling(manager):
    # Complete from a worker thread; await_tasks returns once with one result.


def test_late_event_sequence_never_regresses(manager):
    manager.emit("t1", "activity", {"label": "new"}, sequence=2)
    manager.emit("t1", "activity", {"label": "old"}, sequence=1)
    assert manager.last_sequence("t1") == 2


def test_cancel_preserves_partial_result(manager):
    manager.control("t1", action="cancel")
    result = manager.await_tasks(["t1"], timeout=2)
    assert result[0].status.value == "cancelled"
    assert result[0].partial_result is not None
```

- [ ] **Step 2: Verify the tests fail**

Run: `python -m pytest tests/test_subagent_manager.py -q`

Expected: manager module missing.

- [ ] **Step 3: Implement manager**

Use:

- `ThreadPoolExecutor(max_workers=4)`.
- One scheduler condition and FIFO queue.
- Per-task `threading.Event` cancellation tokens.
- Injectable `runner_for(role)` and event sink for tests.
- Persist-before-publish ordering.
- Resource acquisition before worker submission so lock waiters do not occupy
  worker slots.
- `await_tasks()` condition waits that also inspect the parent cancel event.
- `control()` actions `cancel`, `pause`, `resume`, `retry`, and `continue`.
- Attempt increment on retry/continue while retaining the task ID.
- Event sequence allocated by `SubagentStore.next_sequence()`.

`pause` is cooperative. For Operator, the runner returns at the next safe tool
boundary, releases `desktop_lock`, stores a checkpoint, and transitions to
`paused`.

- [ ] **Step 4: Run manager tests**

Run: `python -m pytest tests/test_subagent_manager.py -q`

Expected: all tests pass without timing flakes.

### Task 7: Add Dispatch, Await, and Control Tools

**Files:**
- Create: `src/agent_assistant/tools/subagent_tools.py`
- Modify: `src/agent_assistant/tools/register.py`
- Modify: `src/agent_assistant/tools/labels.py`
- Test: `tests/test_subagent_tools.py`

- [ ] **Step 1: Write failing orchestration-tool tests**

Verify:

```python
def test_dispatch_requires_bound_parent_context(fake_manager):
    result = DispatchSubagentsTool(fake_manager).execute(tasks=[{
        "title": "查文件", "role": "explorer", "goal": "查找资料"
    }])
    assert not result.ok
    assert result.error_category == "missing_execution_context"


def test_dispatch_accepts_at_most_four(fake_manager, bound_context):
    result = DispatchSubagentsTool(fake_manager).execute(tasks=[task()] * 5)
    assert not result.ok
    assert result.code == 400


def test_await_returns_unified_results(fake_manager, bound_context):
    result = AwaitSubagentsTool(fake_manager).execute(task_ids=["t1", "t2"])
    assert result.ok
    assert len(result.data["results"]) == 2


def test_control_allowlists_actions(fake_manager, bound_context):
    result = ControlSubagentTool(fake_manager).execute(
        task_id="t1", action="delete"
    )
    assert not result.ok
```

- [ ] **Step 2: Verify the tests fail**

Run: `python -m pytest tests/test_subagent_tools.py -q`

Expected: orchestration tool classes missing.

- [ ] **Step 3: Implement tools**

`dispatch_subagents` takes an array schema whose item properties are
`title`, `role`, `goal`, and optional `context`. It uses the bound
conversation/turn IDs rather than trusting model-supplied IDs.

`await_subagents` accepts `task_ids: string[]` and delegates one condition wait
using the bound parent cancellation event.

`control_subagent` accepts:

```python
action in {"continue", "retry", "pause", "resume", "cancel"}
```

Require non-empty `instruction` for `continue`. Verify every target task belongs
to the bound conversation before control. Register all three tools and Chinese
labels.

- [ ] **Step 4: Run orchestration tests**

Run: `python -m pytest tests/test_subagent_tools.py tests/test_tools.py -q`

Expected: all tests pass.

### Task 8: Adapt the Existing Researcher Without Regressing It

**Files:**
- Create: `src/agent_assistant/subagents/runners/__init__.py`
- Create: `src/agent_assistant/subagents/runners/researcher.py`
- Modify: `src/agent_assistant/tools/research.py`
- Test: `tests/test_subagent_researcher.py`
- Test: `tests/test_research_progress.py`
- Test: `tests/test_research_resume.py`

- [ ] **Step 1: Write failing adapter/cancellation tests**

Test that:

- A Researcher task maps the existing ToolResult into the unified result.
- Existing `run_id`, `resume_from`, `report`, `l0_raw`, `l1_trace`, and
  `l2_findings` remain available in artifacts/output.
- Two parallel researchers use distinct cancellation callbacks.
- Cancelling researcher A does not cancel researcher B.
- Progress events carry A or B's exact task ID.

- [ ] **Step 2: Verify the tests fail**

Run: `python -m pytest tests/test_subagent_researcher.py -q`

Expected: adapter missing and existing research loop lacks callback parameters.

- [ ] **Step 3: Inject callbacks into the research loop**

Extend:

```python
def run_research_subagent(
    goal: str,
    context: str = "",
    max_turns: int | None = None,
    local_sources: list[LocalSource] | None = None,
    use_knowledge: bool = False,
    resume_from: str | None = None,
    *,
    cancel_check: Callable[[], bool] | None = None,
    on_progress: Callable[[str, str, int, int], None] | None = None,
) -> ToolResult:
```

Default `cancel_check` to the legacy `is_cancelled` function only when no task
callback was supplied. Pass it into `_call_llm_guarded`. Replace direct
`_emit_progress` calls with `on_progress` when supplied, preserving legacy UI
events otherwise.

Implement `ResearcherRunner.run(task, controls, emit)` and map the result into
`SubagentResult` without copying the full report into `summary`.

- [ ] **Step 4: Run all research tests**

Run: `python -m pytest tests/test_subagent_researcher.py tests/test_research_progress.py tests/test_research_resume.py tests/test_research_robustness.py tests/test_research_memory.py -q`

Expected: all tests pass.

### Task 9: Preserve `dispatch_research` as a Compatibility Wrapper

**Files:**
- Modify: `src/agent_assistant/tools/research.py`
- Test: `tests/test_research_resume.py`
- Test: `tests/test_subagent_researcher.py`

- [ ] **Step 1: Add failing wrapper tests**

Assert that `DispatchResearchTool.execute()` creates one Researcher task through
the manager, awaits it, and converts the unified result back to the existing
ToolResult fields. Verify direct legacy fallback remains available when no
execution context is bound, which keeps CLI and isolated tests working.

- [ ] **Step 2: Verify the test fails**

Run: `python -m pytest tests/test_subagent_researcher.py -q`

Expected: the existing tool still calls `run_research_subagent` directly.

- [ ] **Step 3: Implement wrapper mapping**

When a parent context exists:

1. Create one Researcher task.
2. Await it once.
3. Return existing fields: `summary`, `report`, `findings`, `run_id`,
   `resume_from`, `next_action`, `l0_raw`, `l1_trace`, `l2_findings`,
   `report_path`, `turns_used`, and `findings_count`.

Without a parent context, retain the current direct call for CLI compatibility.

- [ ] **Step 4: Run compatibility tests**

Run: `python -m pytest tests/test_subagent_researcher.py tests/test_research_resume.py tests/test_research_progress.py -q`

Expected: all tests pass.

### Task 10: Wire Manager Lifecycle and Bridge Controls

**Files:**
- Modify: `src/agent_assistant/ui/launch.py`
- Modify: `src/agent_assistant/ui/bridge.py`
- Test: `tests/test_subagent_bridge.py`

- [ ] **Step 1: Write failing bridge tests**

Cover:

- `get_init_data()` returns tasks only for the active conversation.
- `switch_conversation()` causes frontend task hydration for the selected
  conversation.
- `control_subagent(task_id, action, instruction)` refuses tasks owned by a
  different conversation.
- `respond_organizer_plan()` is declared but returns a clear `wrong_role`
  failure until Organizer is implemented.
- Manager events are pushed unchanged with task ID and sequence.

- [ ] **Step 2: Verify the tests fail**

Run: `python -m pytest tests/test_subagent_bridge.py -q`

Expected: bridge methods and init payload are missing.

- [ ] **Step 3: Wire startup and APIs**

In `launch.py`, configure the lazy manager with an event sink:

```python
def on_subagent_event(event: SubagentEvent) -> None:
    api_bridge.push_event("subagent_event", event.to_dict())
```

Start recovery after the bridge/window is bound. In `ApiBridge`, add
`set_subagent_manager`, include `subagents` in init data, and expose validated
`control_subagent` and `respond_organizer_plan` methods. Never include raw
secret-bearing artifact content in init data.

- [ ] **Step 4: Run bridge tests**

Run: `python -m pytest tests/test_subagent_bridge.py tests/test_integration.py -q`

Expected: all tests pass.

### Task 11: Add Generic Cursor-Style Frontend State and Rows

**Files:**
- Modify: `frontend/src/types.ts`
- Modify: `frontend/src/lib/store.ts`
- Create: `frontend/src/components/SubagentList.tsx`
- Modify: `frontend/src/components/ChatMessage.tsx`
- Modify: `frontend/src/lib/bridge.ts`

- [ ] **Step 1: Add exact TypeScript contracts**

Define:

```typescript
export type SubagentRole = "explorer" | "researcher" | "operator" | "organizer";
export type SubagentStatus =
  | "created" | "queued" | "running" | "waiting_user" | "paused"
  | "completed" | "failed" | "cancelled" | "interrupted";

export interface SubagentTaskView {
  task_id: string;
  conversation_id: string;
  parent_turn_id: string;
  role: SubagentRole;
  title: string;
  goal: string;
  status: SubagentStatus;
  attempt: number;
  sequence: number;
  label?: string;
  events: SubagentTimelineEvent[];
  result?: SubagentResultView;
}
```

Add `subagent_event` to `AgentEvent`, `subagents` to `InitData`, and bridge
methods for control and Organizer decisions.

- [ ] **Step 2: Implement sequence-safe reducers**

Add `subagentsById`, `subagentOrderByParentTurn`, and
`activeOperatorTaskId`. The reducer must ignore an event when:

```typescript
event.sequence <= (existing?.sequence ?? 0)
```

The first event for a parent turn inserts one `ChatItem` of kind
`"subagents"`; later events only update maps. Remove the old
`updateResearchProgress` guessing logic after the compatibility Researcher
event is mapped by task ID.

- [ ] **Step 3: Implement rows and timelines**

`SubagentList` must render:

- Dynamic title.
- Role label.
- Status text and chip.
- Shimmer only for `queued`/`running`.
- Expandable timeline and result.
- Stop for running.
- Pause for running Operator.
- Resume/stop for paused.
- Retry for failed/cancelled/interrupted.
- Continue input for completed/failed.

Do not show L0 inline. Use artifact links/labels only.

- [ ] **Step 4: Build the frontend**

Run: `npm run build`

Working directory: `frontend`

Expected: TypeScript and Vite build succeed.

### Task 12: Update Main-Agent Delegation Guidance

**Files:**
- Modify: `src/agent_assistant/agent/prompt.py`
- Modify: `src/agent_assistant/tools/labels.py`
- Test: `tests/test_prompt_contract.py`

- [ ] **Step 1: Add failing prompt assertions**

Assert the full prompt:

- Describes the four personal-assistant roles.
- Tells the model to use `dispatch_subagents` only for substantial,
  parallelizable or context-heavy work.
- Says one `await_subagents`, never poll.
- Says use `control_subagent` for retry/continue/cancel.
- Forbids Coder and nested subagents.
- Keeps memory/profile decisions with the main Agent.

- [ ] **Step 2: Verify the assertions fail**

Run: `python -m pytest tests/test_prompt_contract.py -q`

Expected: new orchestration guidance absent.

- [ ] **Step 3: Add concise prompt sections**

Keep routing agentic rather than encoding a rigid sequence. Include role
selection examples for local search, web research, desktop operation, and
file organization. Preserve existing `dispatch_research` guidance as a
compatibility path for one deep-research task.

- [ ] **Step 4: Run prompt and core milestone tests**

Run: `python -m pytest tests/test_prompt_contract.py tests/test_subagent_models.py tests/test_subagent_store.py tests/test_subagent_resources.py tests/test_subagent_manager.py tests/test_subagent_tools.py tests/test_subagent_researcher.py tests/test_subagent_bridge.py -q`

Expected: all tests pass.

---

## Milestone B — Explorer

### Task 13: Add Bounded Local File Search

**Files:**
- Create: `src/agent_assistant/tools/search_local_files.py`
- Modify: `src/agent_assistant/tools/register.py`
- Modify: `src/agent_assistant/tools/labels.py`
- Test: `tests/test_search_local_files.py`

- [ ] **Step 1: Write failing local-search tests**

Test:

- Name search finds nested files.
- Search refuses a jail-outside root.
- Symlink/reparse traversal outside the root is skipped.
- Binary files are never content-scanned.
- Text scanning respects `max_files`, `max_file_bytes`, and timeout.
- Result snippets are capped and do not expose secrets.
- Invalid extensions and negative limits return validation failures.

- [ ] **Step 2: Verify the tests fail**

Run: `python -m pytest tests/test_search_local_files.py -q`

Expected: tool module missing.

- [ ] **Step 3: Implement `SearchLocalFilesTool`**

Parameters:

```python
root: str
query: str
pattern: str = "*"
search_content: bool = False
max_results: int = 50
```

Use `Path.resolve(strict=True)` and `path_in_allowed_roots`. Walk with
`os.scandir`, do not follow symlinks/reparse points, cap visited entries, and
check a monotonic deadline. Content scan only UTF-8-compatible files in a fixed
safe extension set and below 1 MiB. Return canonical path, size, modified time,
match kind, and a 240-character redacted snippet.

- [ ] **Step 4: Run local-search and jail tests**

Run: `python -m pytest tests/test_search_local_files.py tests/test_sandbox_l2.py tests/test_permission.py -q`

Expected: all tests pass.

### Task 14: Implement Explorer Runner

**Files:**
- Create: `src/agent_assistant/subagents/runtime.py`
- Create: `src/agent_assistant/subagents/runners/explorer.py`
- Test: `tests/test_subagent_explorer.py`

- [ ] **Step 1: Write failing runtime/Explorer tests**

Use a fake LLM that first calls `search_local_files`, then `read_file`, then
returns a final answer. Assert:

- Only Explorer allowlisted tools are exposed.
- Tool parameters/results are archived.
- Events carry the correct task ID.
- Old tool results are summarized in context while L0 retains redacted content.
- Cancellation returns a partial result.
- Output evidence contains canonical local paths.

- [ ] **Step 2: Verify the tests fail**

Run: `python -m pytest tests/test_subagent_explorer.py -q`

Expected: generic runtime and Explorer runner missing.

- [ ] **Step 3: Implement task-focused runtime**

Implement a synchronous loop with:

- Guarded LLM call timeout.
- Explicit task cancellation/pause checks.
- OpenAI assistant/tool message pairing.
- Per-role registry factory.
- L0 logging before context truncation.
- Recent full tool results plus L1 summaries for older results.
- Final no-tools wrap-up turn.
- Unified partial/final result creation.

Explorer prompt must require path-backed evidence and forbid claims that a file
was read when only its filename was observed.

- [ ] **Step 4: Run Explorer tests**

Run: `python -m pytest tests/test_subagent_explorer.py tests/test_search_local_files.py -q`

Expected: all tests pass.

---

## Milestone C — Operator

### Task 15: Implement Operator Runner and Global Desktop Lock

**Files:**
- Create: `src/agent_assistant/subagents/runners/operator.py`
- Modify: `src/agent_assistant/subagents/profiles.py`
- Modify: `src/agent_assistant/tools/ui_driver.py`
- Test: `tests/test_subagent_operator.py`

- [ ] **Step 1: Write failing Operator tests**

Test:

- Only launch/focus/UIA tools are exposed.
- Two Operators never run desktop actions concurrently.
- Main-agent `ui_click` receives `resource_busy` while an Operator owns desktop.
- Pause checkpoints and releases the desktop lock.
- Resume reacquires the lock and forces `ui_inspect` before the next mutation.
- Success requires a post-action state observation.
- `ui_type` text is absent from visible events and L0 archives.

- [ ] **Step 2: Verify the tests fail**

Run: `python -m pytest tests/test_subagent_operator.py -q`

Expected: Operator runner missing.

- [ ] **Step 3: Implement Operator behavior**

Use the generic runtime with the exact Operator allowlist. Track whether the UI
has been inspected since acquisition/resume and reject mutating calls until it
has. Record action type, target descriptor, pre/post state hashes, and a
sanitized outcome; never record typed text.

- [ ] **Step 4: Run Operator and UI automation tests**

Run: `python -m pytest tests/test_subagent_operator.py tests/test_ui_automation.py tests/test_ui_automation_tools.py -q`

Expected: all tests pass.

### Task 16: Add the Operator Control Banner

**Files:**
- Create: `frontend/src/components/OperatorControlBar.tsx`
- Modify: `frontend/src/App.tsx`
- Modify: `frontend/src/lib/store.ts`
- Modify: `frontend/src/lib/bridge.ts`
- Modify: `frontend/src/types.ts`

- [ ] **Step 1: Implement derived active-Operator state**

Set `activeOperatorTaskId` only for an Operator in `running` state. Clear it
for paused/terminal states or when another conversation is active.

- [ ] **Step 2: Implement persistent banner controls**

Render target application, latest sanitized activity label, interference
warning, Pause, and Stop. Buttons call `control_subagent`; they do not append
chat messages and do not switch conversations.

- [ ] **Step 3: Build the frontend**

Run: `npm run build`

Working directory: `frontend`

Expected: build succeeds.

---

## Milestone D — Organizer

### Task 17: Implement Manifest Approval and Safe Execution

**Files:**
- Create: `src/agent_assistant/subagents/runners/organizer.py`
- Create: `src/agent_assistant/subagents/manifest.py`
- Modify: `src/agent_assistant/subagents/manager.py`
- Modify: `src/agent_assistant/ui/bridge.py`
- Test: `tests/test_subagent_organizer.py`

- [ ] **Step 1: Write failing Organizer tests**

Test:

- Planning phase has no write tools.
- Task reaches `waiting_user` with a canonical manifest.
- No filesystem mutation occurs before approval.
- Manifest hash changes if any operation changes.
- Approval is single-use, expiring, and bound to task ID/hash.
- Apply only executes approved moves under jail roots.
- Extra or altered operations are rejected.
- File write lock prevents concurrent Organizer apply phases.
- Rollback refuses overwrite or externally modified targets.
- Denial transitions the task to cancelled with preserved manifest.

- [ ] **Step 2: Verify the tests fail**

Run: `python -m pytest tests/test_subagent_organizer.py -q`

Expected: Organizer runner and manifest module missing.

- [ ] **Step 3: Implement canonical manifest**

Each move contains:

```python
{
    "operation_id": "...",
    "src": "canonical absolute path",
    "dst": "canonical absolute path",
    "source_fingerprint": {"size": 123, "mtime_ns": 456},
    "conflict": "skip"
}
```

Sort operations by operation ID before hashing canonical UTF-8 JSON. Store only
the approval-token hash and expiry. Revalidate jail, source fingerprint,
destination absence, and manifest hash immediately before every move. Record a
rollback ledger after each successful move.

- [ ] **Step 4: Expose approval bridge**

`respond_organizer_plan(task_id, manifest_hash, approved)` must:

- Require the task's conversation to be active.
- Reload the authoritative manifest from disk.
- Compare hashes with constant-time comparison.
- Never trust paths from the frontend.
- Resume apply only on approval.

- [ ] **Step 5: Run Organizer and sandbox tests**

Run: `python -m pytest tests/test_subagent_organizer.py tests/test_sandbox_l2.py tests/test_permission.py -q`

Expected: all tests pass.

### Task 18: Add Organizer Approval UI

**Files:**
- Create: `frontend/src/components/OrganizerApproval.tsx`
- Modify: `frontend/src/components/SubagentList.tsx`
- Modify: `frontend/src/lib/store.ts`
- Modify: `frontend/src/lib/bridge.ts`

- [ ] **Step 1: Render the authoritative plan summary**

Show move count, destination folders, conflicts, skipped files, and an
expandable operation list. Do not render approval tokens or internal database
paths.

- [ ] **Step 2: Add explicit approve/reject controls**

Only button clicks send a decision. Outside clicks and Escape do nothing.
Disable both buttons after submission until the backend emits the next task
state.

- [ ] **Step 3: Build the frontend**

Run: `npm run build`

Working directory: `frontend`

Expected: build succeeds.

---

## Milestone E — Recovery, Integration, and Release Verification

### Task 19: Implement Startup Recovery and Conversation Hydration

**Files:**
- Modify: `src/agent_assistant/subagents/manager.py`
- Modify: `src/agent_assistant/ui/launch.py`
- Modify: `src/agent_assistant/ui/bridge.py`
- Modify: `frontend/src/lib/store.ts`
- Test: `tests/test_subagent_manager.py`
- Test: `tests/test_subagent_bridge.py`

- [ ] **Step 1: Add failing recovery tests**

Create queued/running/waiting_user/paused persisted tasks, restart the manager,
and assert all become interrupted with `previous_status` and intact checkpoint
paths. Verify no task automatically resumes.

- [ ] **Step 2: Implement recovery**

Run recovery once after store initialization and before UI hydration. Publish
one recovery event per task. `get_init_data` and conversation switching must
load persisted task summaries and their latest sequence.

- [ ] **Step 3: Run recovery tests**

Run: `python -m pytest tests/test_subagent_manager.py tests/test_subagent_bridge.py -q`

Expected: all tests pass.

### Task 20: End-to-End Personal Assistant Scenario

**Files:**
- Create: `tests/test_subagent_end_to_end.py`
- Modify: only files required by failures found in this task.

- [ ] **Step 1: Add the four-role integration test**

Use fake LLM/tool backends for:

> 帮我找电脑里的考研资料，查今年政策，整理到一个文件夹，同时在网易云播放周杰伦的《夜曲》。

Assert:

- Four named tasks are created with exact roles.
- Explorer, Researcher, and Organizer planning overlap.
- Operator owns desktop exclusively.
- Organizer performs zero moves before approval.
- Every emitted event has conversation, turn, task, and increasing sequence.
- Await returns one result per task.
- Main receives partial results on injected child failure.
- No task asks the user to paste progress.

- [ ] **Step 2: Run the end-to-end test**

Run: `python -m pytest tests/test_subagent_end_to_end.py -q`

Expected: pass.

### Task 21: Full Security and Regression Verification

**Files:**
- Modify: only files required by concrete test failures.

- [ ] **Step 1: Run Ruff**

Run: `python -m ruff check src tests`

Expected: no lint errors.

- [ ] **Step 2: Run all Python tests**

Run: `python -m pytest tests -q`

Expected: all tests pass; existing platform-specific skips remain documented.

- [ ] **Step 3: Run frontend build**

Run: `npm run build`

Working directory: `frontend`

Expected: TypeScript and Vite build succeed.

- [ ] **Step 4: Perform the six-part security check**

Verify with tests and focused diff review:

1. User input is enum/range/path validated.
2. Every SQL value is parameterized.
3. Keys, tokens, passwords, cookies, and typed secrets are absent from events,
   task archives, model prompts, and visible errors.
4. Errors are sanitized and expose no stack, internal DB schema, or versions.
5. File search/moves remain inside jail roots; no delete/upload is introduced.
6. Research URLs still pass URL guard and SSRF checks.

- [ ] **Step 5: Run a local UI smoke test**

Start the application, dispatch two fake/read-only subagents, verify two
independent rows, expand timelines, stop one task, retry it, switch
conversations, and verify events never appear in the wrong conversation.

Expected: Cursor-style rows remain responsive and no active task hijacks another
conversation.

