"""Unified tool permission policy: allow / ask / deny.

Single place for "can this tool call run?" decisions. The registry evaluates
the policy before execute; confirm_gate handles ASK. Tools should not call
confirm_gate themselves (except during migration / tests).

Rule matching: first matching rule wins. If none match, ``tool.requires_confirm``
falls back to ASK; otherwise ALLOW.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable

# ── Dangerous command patterns (moved from run_command) ───────────────────────

DANGEROUS_COMMAND_PATTERNS = [
    r"\bRemove-Item\b",
    r"\brm\b",
    r"\bdel\b",
    r"\bformat\b",
    r"\bFormat-Volume\b",
    r"\bClear-Disk\b",
    r"\bStop-Process\b",
    r"\btaskkill\b",
    r"\breg\s+(delete|add)\b",
    r"\bSet-ItemProperty\b.*-Path.*HKLM",
    r"\bShutdown\b",
    r"\bRestart-Computer\b",
    r"\bStop-Computer\b",
    r"\bnet\s+user\b.*/delete",
    r"\bicacls\b.*/reset",
    r"\btakeown\b",
    r"\bInvoke-Expression\b",
    r"\biex\b",
    r"\bStart-Process\b",
    r"\bNew-Item\b.*-Path.*HKLM",
    r"\bRemove-ItemProperty\b",
    r"\bwmic\b.*\bdelete\b",
    r"\bvssadmin\b.*\bdelete\b",
    r"\bbcdedit\b",
    r"\bcipher\b.*/w",
]

_DANGEROUS_COMMAND_RE = re.compile(
    "|".join(DANGEROUS_COMMAND_PATTERNS), re.IGNORECASE
)

# System directories that need confirmation for move/write destinations
DANGEROUS_DIRS = [
    "c:\\windows",
    "c:\\program files",
    "c:\\program files (x86)",
    "c:\\programdata",
]


def is_dangerous_command(command: str) -> bool:
    """True if a shell command matches a destructive/sensitive pattern."""
    return bool(_DANGEROUS_COMMAND_RE.search(command or ""))


def path_in_dangerous_dir(path_str: str) -> bool:
    """True if path is under a protected system directory."""
    if not path_str:
        return False
    try:
        resolved = str(Path(path_str).expanduser().resolve()).lower()
    except OSError:
        resolved = str(path_str).lower()
    return any(resolved.startswith(d) for d in DANGEROUS_DIRS)


# ── Command tiering (L2): safe allowlist vs gray zone ─────────────────────────
#
# Three tiers for run_command:
#   1. dangerous patterns → always ASK (guardrail, beats user override)
#   2. safe allowlist (read-only / inspection) → ALLOW
#   3. everything else (gray) → ASK by default; user can set run_command to
#      "auto" in tool permissions to silence the gray zone

SAFE_COMMAND_PREFIXES: frozenset[str] = frozenset({
    # Shell read-only
    "dir", "ls", "echo", "pwd", "type", "cat", "where", "which", "ver",
    "whoami", "hostname", "ipconfig", "systeminfo", "tasklist",
    # PowerShell read-only cmdlets
    "get-date", "get-childitem", "get-location", "get-process", "get-content",
    "get-item", "get-service", "get-psdrive", "get-host", "get-command",
    "write-output", "write-host",
    # Dev tooling inspection
    "python --version", "python -v", "python -c \"import", "pip list",
    "pip show", "pip --version", "node --version", "npm --version",
    "npm list", "npm view", "git status", "git log", "git diff",
    "git branch", "git show", "git remote", "git rev-parse",
})

# Tokens that make a command non-trivial: chaining, pipes, substitution,
# and redirection (`echo x > file` WRITES — must never count as safe).
_UNSAFE_TOKENS = (";", "&&", "||", "|", "`", "$(", ">", "<")


def is_safe_command(command: str) -> bool:
    """True if the command is a plain, single, allowlisted read-only command."""
    cmd = (command or "").strip().lower()
    if not cmd:
        return False
    if any(tok in cmd for tok in _UNSAFE_TOKENS):
        return False
    return any(cmd == p or cmd.startswith(p + " ") for p in SAFE_COMMAND_PREFIXES)


# ── Filesystem jail (L2 软沙箱) ───────────────────────────────────────────────
#
# File tools (read/write/edit/list/move) stay inside user-approved roots.
# Outside → ASK (guardrail, beats user "auto" override). Configured via the
# ``file_jail_roots`` setting: semicolon-separated paths; empty = built-in
# defaults; ``off`` = jail disabled.


def default_jail_roots() -> list[str]:
    home = Path.home()
    roots = [home / d for d in ("Desktop", "Documents", "Downloads", "Pictures", "Music", "Videos")]
    try:
        from agent_assistant.config import settings

        roots.append(settings.data_dir)
    except Exception:
        pass
    return [str(r) for r in roots]


def jail_roots() -> list[str]:
    """Effective jail roots (live-read from settings; empty list = disabled)."""
    try:
        from agent_assistant.ui.store import ui_store

        raw = ui_store.get_setting("file_jail_roots", "").strip()
    except Exception:
        raw = ""
    if raw.lower() in ("off", "*", "none"):
        return []
    if not raw:
        return default_jail_roots()
    return [p.strip() for p in raw.split(";") if p.strip()]


def path_in_allowed_roots(path_str: str) -> bool:
    """True if path resolves inside one of the jail roots (or jail is off)."""
    roots = jail_roots()
    if not roots:
        return True
    if not path_str or not path_str.strip():
        return False
    try:
        resolved = Path(path_str).expanduser().resolve()
    except OSError:
        return False
    for root in roots:
        try:
            r = Path(root).expanduser().resolve()
            if resolved == r or resolved.is_relative_to(r):
                return True
        except OSError:
            continue
    return False


def _match_file_outside_jail(param: str) -> MatchFn:
    def _m(kwargs: dict[str, Any]) -> bool:
        return not path_in_allowed_roots(str(kwargs.get(param, "")))

    return _m


def _match_move_outside_jail(kwargs: dict[str, Any]) -> bool:
    return not path_in_allowed_roots(str(kwargs.get("src", ""))) or (
        not path_in_allowed_roots(str(kwargs.get("dst", "")))
    )


class PermissionAction(str, Enum):
    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


@dataclass(frozen=True)
class PermissionDecision:
    action: PermissionAction
    reason: str = ""


MatchFn = Callable[[dict[str, Any]], bool]


@dataclass(frozen=True)
class PermissionRule:
    """One policy rule. ``match`` None means all calls to ``tool``."""

    tool: str
    action: PermissionAction
    match: MatchFn | None = None
    reason: str = ""


class PermissionPolicy:
    """Ordered rule list; first match wins."""

    def __init__(self, rules: list[PermissionRule] | None = None) -> None:
        self.rules: list[PermissionRule] = list(rules or [])

    def evaluate(
        self,
        tool_name: str,
        kwargs: dict[str, Any] | None = None,
        *,
        requires_confirm: bool = False,
    ) -> PermissionDecision:
        kwargs = kwargs or {}
        for rule in self.rules:
            if rule.tool != "*" and rule.tool != tool_name:
                continue
            if rule.match is not None and not rule.match(kwargs):
                continue
            return PermissionDecision(rule.action, rule.reason or rule.action.value)
        if requires_confirm:
            return PermissionDecision(
                PermissionAction.ASK, "tool.requires_confirm"
            )
        return PermissionDecision(PermissionAction.ALLOW, "default")


def _match_dangerous_command(kwargs: dict[str, Any]) -> bool:
    return is_dangerous_command(str(kwargs.get("command", "")))


def _match_move_to_system(kwargs: dict[str, Any]) -> bool:
    return path_in_dangerous_dir(str(kwargs.get("dst", "")))


def _match_write_to_system(kwargs: dict[str, Any]) -> bool:
    return path_in_dangerous_dir(str(kwargs.get("path", "")))


def guardrail_rules() -> list[PermissionRule]:
    """Conditional safety rules — always apply, beat user overrides."""
    ask = PermissionAction.ASK
    return [
        PermissionRule(
            "run_command",
            ask,
            match=_match_dangerous_command,
            reason="dangerous command",
        ),
        PermissionRule(
            "move_file",
            ask,
            match=_match_move_to_system,
            reason="destination in system directory",
        ),
        PermissionRule(
            "write_file",
            ask,
            match=_match_write_to_system,
            reason="path in system directory",
        ),
        PermissionRule(
            "edit_file",
            ask,
            match=_match_write_to_system,
            reason="path in system directory",
        ),
        # Filesystem jail: file tools stay inside user-approved roots.
        # After the system-dir rules so those keep their specific reason.
        PermissionRule(
            "read_file",
            ask,
            match=_match_file_outside_jail("path"),
            reason="路径不在允许的工作区内",
        ),
        PermissionRule(
            "write_file",
            ask,
            match=_match_file_outside_jail("path"),
            reason="路径不在允许的工作区内",
        ),
        PermissionRule(
            "edit_file",
            ask,
            match=_match_file_outside_jail("path"),
            reason="路径不在允许的工作区内",
        ),
        PermissionRule(
            "list_files",
            ask,
            match=_match_file_outside_jail("path"),
            reason="路径不在允许的工作区内",
        ),
        PermissionRule(
            "move_file",
            ask,
            match=_match_move_outside_jail,
            reason="路径不在允许的工作区内",
        ),
    ]


def default_tool_rules() -> list[PermissionRule]:
    """Per-tool baseline rules (used when the user has no override)."""
    ask = PermissionAction.ASK
    return [
        # Command tiering: gray-zone commands ask; safe allowlist falls through
        # to ALLOW; dangerous patterns already caught by guardrails.
        PermissionRule(
            "run_command",
            ask,
            match=lambda kw: not is_safe_command(str(kw.get("command", ""))),
            reason="未在白名单内的命令",
        ),
        PermissionRule("kill_process", ask, reason="terminate process"),
        PermissionRule("save_note", ask, reason="persist note to disk"),
        # Desktop UI automation: inspect is read-only; mutations need confirm
        PermissionRule("ui_inspect", PermissionAction.ALLOW, reason="read UI tree"),
        PermissionRule("ui_click", ask, reason="click desktop UI"),
        PermissionRule("ui_type", ask, reason="type into desktop UI"),
        PermissionRule("ui_hotkey", ask, reason="send keys to desktop UI"),
        PermissionRule("ui_scroll", ask, reason="scroll desktop UI"),
    ]


def default_permission_policy() -> PermissionPolicy:
    """Built-in desktop-agent policy (OpenCode-style allow/ask/deny)."""
    return PermissionPolicy(guardrail_rules() + default_tool_rules())


# ── User overrides (settings page: 自动运行 / 需要询问) ────────────────────────

OverrideFn = Callable[[str], str | None]  # tool name → "auto" | "ask" | None


def get_tool_override(tool_name: str) -> str | None:
    """Read the user's per-tool override from the UI settings store.

    Stored as ``tool_perm.<tool_name>`` = "auto" | "ask". Returns None when
    unset or when the store is unavailable (tests, headless runs).
    """
    try:
        from agent_assistant.ui.store import ui_store

        value = ui_store.get_setting(f"tool_perm.{tool_name}", "").strip().lower()
        return value if value in ("auto", "ask") else None
    except Exception:
        return None


class UserOverridePolicy(PermissionPolicy):
    """Guardrails → user override (auto/ask) → built-in defaults.

    Guardrail rules (dangerous commands, system paths) always win — setting a
    tool to "auto" never silences them.
    """

    def __init__(self, get_override: OverrideFn = get_tool_override) -> None:
        self._guardrails = guardrail_rules()
        self._defaults = default_tool_rules()
        self._get_override = get_override
        super().__init__(self._guardrails + self._defaults)

    def evaluate(
        self,
        tool_name: str,
        kwargs: dict[str, Any] | None = None,
        *,
        requires_confirm: bool = False,
    ) -> PermissionDecision:
        kwargs = kwargs or {}
        for rule in self._guardrails:
            if rule.tool in ("*", tool_name) and rule.match is not None and rule.match(kwargs):
                return PermissionDecision(rule.action, rule.reason or rule.action.value)

        try:
            override = (self._get_override(tool_name) or "").strip().lower()
        except Exception:
            override = ""
        if override == "auto":
            return PermissionDecision(PermissionAction.ALLOW, "user override: auto")
        if override == "ask":
            return PermissionDecision(PermissionAction.ASK, "user override: ask")

        for rule in self._defaults:
            if rule.tool in ("*", tool_name) and (rule.match is None or rule.match(kwargs)):
                return PermissionDecision(rule.action, rule.reason or rule.action.value)
        if requires_confirm:
            return PermissionDecision(PermissionAction.ASK, "tool.requires_confirm")
        return PermissionDecision(PermissionAction.ALLOW, "default")


# Process-wide default (tests may replace on a ToolRegistry instance)
permission_policy = default_permission_policy()
