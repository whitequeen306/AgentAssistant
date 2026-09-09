"""Fixed role profiles: tool allowlists, prompts, budgets, resource needs.

The four personal-assistant roles are fixed templates (user decision:
fixed_profiles). A subagent NEVER selects its own tools — the runtime builds
its registry strictly from the profile allowlist plus explicit, caller-supplied
conditional grants (attached sources / knowledge base for Researcher).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .models import SubagentRole

# Explorer / Organizer share the read-only local exploration set.
_LOCAL_READ_TOOLS = frozenset(
    {
        "list_files",
        "read_file",
        "search_local_files",
        "search_knowledge",
        "read_attached_source",
    }
)


@dataclass(frozen=True, slots=True)
class RoleProfile:
    """Immutable capability template for one subagent role."""

    role: SubagentRole
    tools: frozenset[str]
    system_prompt: str
    max_turns: int
    needs_desktop: bool = False
    needs_filesystem_write: bool = False
    # Explicit caller-granted extras (never model-selected): flag → tool name.
    conditional_tools: dict[str, str] = field(default_factory=dict)

    def tools_for(
        self,
        *,
        has_local_sources: bool = False,
        use_knowledge: bool = False,
    ) -> frozenset[str]:
        """Effective allowlist including explicitly granted conditional tools."""
        extra: set[str] = set()
        if has_local_sources and "has_local_sources" in self.conditional_tools:
            extra.add(self.conditional_tools["has_local_sources"])
        if use_knowledge and "use_knowledge" in self.conditional_tools:
            extra.add(self.conditional_tools["use_knowledge"])
        return self.tools | frozenset(extra)


_EXPLORER_PROMPT = """\
You are the Explorer subagent of a personal desktop assistant. Your only job \
is to find and read LOCAL information (files, notes, knowledge base) for the \
given goal, then report findings.

Rules:
- Read-only. You cannot modify, move, or create files.
- Every claim must be backed by a path you actually inspected. NEVER claim you \
read a file when you only saw its filename in a listing — say "found but not \
read" instead.
- Prefer search_local_files to locate candidates, then read_file to confirm \
content. Do not read binary files.
- Stay inside the user's allowed directories; if a path is refused, report it \
and move on rather than retrying.
- Finish with a concise report: what was found (with absolute paths), what was \
verified by reading, and what is missing."""

_RESEARCHER_PROMPT = """\
You are the Researcher subagent of a personal desktop assistant. Your only \
job is iterative web research for the given goal: search, read pages, \
cross-check, then write a report with source URLs.

Rules:
- Use web_search for discovery; read_page / extract_content for depth. Do not \
fetch search-engine result pages directly with read_page.
- Every claim in the report needs at least one source URL.
- Mark unverified or conflicting information explicitly.
- A partial but honest report beats no report — always produce the report \
before your budget runs out."""

_OPERATOR_PROMPT = """\
You are the Operator subagent of a personal desktop assistant. Your only job \
is to operate desktop applications through UI automation to achieve the given \
goal.

Rules:
- You own the desktop exclusively while running; the user sees a control \
banner and may pause you at any time.
- ALWAYS ui_inspect before the first action and after anything that may have \
changed the screen. Never act on a stale tree.
- After every click/type/hotkey, verify the effect (re-inspect or check \
state); report honestly when an action did not take effect.
- Never type into fields you have not identified. Never enter passwords or \
payment information — stop and report instead.
- If the target element cannot be found after scrolling and re-inspecting, \
report what you saw instead of guessing coordinates."""

_ORGANIZER_PROMPT = """\
You are the Organizer subagent of a personal desktop assistant. Your job is \
to organize files: first PLAN (read-only scan producing a move manifest), \
then — only after the user approves the manifest — APPLY the approved moves.

Rules:
- During planning you have NO write tools. Produce a complete manifest: every \
move with absolute source and destination, plus conflicts and skips.
- Never plan deletes or overwrites; conflicts default to skip.
- Group by clear, explainable criteria (type, topic, date) and say which you \
used.
- During apply, execute ONLY the approved manifest — no improvisation.
- save_note is only for a final summary note when the user asked for one."""


_PROFILES: dict[SubagentRole, RoleProfile] = {
    SubagentRole.EXPLORER: RoleProfile(
        role=SubagentRole.EXPLORER,
        tools=_LOCAL_READ_TOOLS,
        system_prompt=_EXPLORER_PROMPT,
        max_turns=25,
    ),
    SubagentRole.RESEARCHER: RoleProfile(
        role=SubagentRole.RESEARCHER,
        tools=frozenset({"web_search", "read_page", "extract_content", "save_note"}),
        system_prompt=_RESEARCHER_PROMPT,
        max_turns=60,
        conditional_tools={
            "has_local_sources": "read_local_source",
            "use_knowledge": "search_knowledge",
        },
    ),
    SubagentRole.OPERATOR: RoleProfile(
        role=SubagentRole.OPERATOR,
        tools=frozenset(
            {
                "launch_app",
                "focus_window",
                "ui_inspect",
                "ui_click",
                "ui_type",
                "ui_hotkey",
                "ui_scroll",
            }
        ),
        system_prompt=_OPERATOR_PROMPT,
        max_turns=40,
        needs_desktop=True,
    ),
    SubagentRole.ORGANIZER: RoleProfile(
        role=SubagentRole.ORGANIZER,
        tools=_LOCAL_READ_TOOLS | frozenset({"move_file", "save_note"}),
        system_prompt=_ORGANIZER_PROMPT,
        max_turns=30,
        needs_filesystem_write=True,
    ),
}


def profile_for(role: SubagentRole | str) -> RoleProfile:
    """Return the fixed profile for ``role``; raises ValueError for unknown roles."""
    return _PROFILES[SubagentRole(role)]
