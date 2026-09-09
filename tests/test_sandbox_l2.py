"""L2 soft-sandbox: command tiering (safe allowlist) + filesystem jail."""

from __future__ import annotations

import pytest

from agent_assistant.tools.permission import (
    PermissionAction,
    UserOverridePolicy,
    default_jail_roots,
    default_permission_policy,
    is_safe_command,
    jail_roots,
    path_in_allowed_roots,
)


class TestSafeCommandAllowlist:
    @pytest.mark.parametrize("cmd", [
        "dir", "ls", "Get-Date", "Write-Output hi", "git status",
        "git log --oneline", "git diff HEAD~1", "pip list", "pip show requests",
        "python --version", "npm list", "tasklist", "ipconfig", "whoami",
        "Get-Process", "Get-ChildItem .\\src",
    ])
    def test_safe_commands(self, cmd):
        assert is_safe_command(cmd), cmd

    @pytest.mark.parametrize("cmd", [
        "pip install requests",           # mutating dev command
        "npm run build",                  # arbitrary script
        "python script.py",               # arbitrary code
        "echo hi > f.txt",                # redirect writes a file
        "dir; rm -rf x",                  # chaining hides dangerous part
        "dir && del x",                   # chaining
        "Get-Process | Stop-Process",     # pipe into mutation
        "rm -rf /",                       # dangerous
        "git push --force",               # not in safe prefixes
        "",                               # empty
    ])
    def test_unsafe_commands(self, cmd):
        assert not is_safe_command(cmd), cmd


class TestCommandTieringPolicy:
    def test_safe_command_allowed_by_default(self):
        policy = default_permission_policy()
        assert policy.evaluate("run_command", {"command": "dir"}).action is PermissionAction.ALLOW

    def test_gray_command_asks_by_default(self):
        policy = default_permission_policy()
        d = policy.evaluate("run_command", {"command": "pip install requests"})
        assert d.action is PermissionAction.ASK
        assert "白名单" in d.reason

    def test_dangerous_still_asks_with_specific_reason(self):
        policy = default_permission_policy()
        d = policy.evaluate("run_command", {"command": "Remove-Item C:\\x"})
        assert d.action is PermissionAction.ASK
        assert "dangerous" in d.reason

    def test_auto_override_silences_gray_but_not_dangerous(self):
        policy = UserOverridePolicy(get_override=lambda n: "auto" if n == "run_command" else None)
        gray = policy.evaluate("run_command", {"command": "npm run build"})
        assert gray.action is PermissionAction.ALLOW
        dangerous = policy.evaluate("run_command", {"command": "rm -rf /"})
        assert dangerous.action is PermissionAction.ASK


@pytest.fixture
def jailed_store(tmp_path, monkeypatch):
    """UI store whose jail roots = tmp_path."""
    from agent_assistant.ui import store as store_mod

    store = store_mod.UIStore(tmp_path / "ui.db")
    store.set_setting("file_jail_roots", str(tmp_path))
    monkeypatch.setattr(store_mod, "ui_store", store)
    return store


class TestFilesystemJail:
    def test_inside_roots_allowed(self, jailed_store, tmp_path):
        assert path_in_allowed_roots(str(tmp_path / "notes" / "a.md"))

    def test_outside_roots_rejected(self, jailed_store):
        assert not path_in_allowed_roots(r"C:\Windows\System32\drivers\etc\hosts")

    def test_relative_path_resolved_against_cwd(self, jailed_store, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        assert path_in_allowed_roots("notes/a.md")
        assert not path_in_allowed_roots("../outside.md")

    def test_jail_off_allows_everything(self, jailed_store):
        jailed_store.set_setting("file_jail_roots", "off")
        assert jail_roots() == []
        assert path_in_allowed_roots(r"C:\Windows\win.ini")

    def test_default_roots_include_desktop_and_data_dir(self):
        roots = default_jail_roots()
        from pathlib import Path

        assert str(Path.home() / "Desktop") in roots
        assert any("AgentAssistant" in r for r in roots)

    def test_policy_asks_outside_jail(self, jailed_store):
        policy = default_permission_policy()
        d = policy.evaluate("write_file", {"path": r"C:\temp\evil.txt"})
        assert d.action is PermissionAction.ASK
        assert "工作区" in d.reason

    def test_policy_allows_inside_jail(self, jailed_store, tmp_path):
        policy = default_permission_policy()
        d = policy.evaluate("write_file", {"path": str(tmp_path / "ok.txt")})
        assert d.action is PermissionAction.ALLOW

    def test_move_asks_when_either_side_outside(self, jailed_store, tmp_path):
        policy = default_permission_policy()
        inside = str(tmp_path / "a.txt")
        assert policy.evaluate(
            "move_file", {"src": inside, "dst": r"C:\temp\b.txt"}
        ).action is PermissionAction.ASK
        assert policy.evaluate(
            "move_file", {"src": r"C:\temp\b.txt", "dst": inside}
        ).action is PermissionAction.ASK
        assert policy.evaluate(
            "move_file", {"src": inside, "dst": str(tmp_path / "b.txt")}
        ).action is PermissionAction.ALLOW

    def test_read_and_list_also_jailed(self, jailed_store, tmp_path):
        policy = default_permission_policy()
        assert policy.evaluate("read_file", {"path": r"C:\Windows\win.ini"}).action is PermissionAction.ASK
        assert policy.evaluate("list_files", {"path": r"C:\Windows"}).action is PermissionAction.ASK
        assert policy.evaluate("read_file", {"path": str(tmp_path / "a.md")}).action is PermissionAction.ALLOW

    def test_jail_is_guardrail_beats_auto_override(self, jailed_store):
        policy = UserOverridePolicy(get_override=lambda n: "auto" if n == "write_file" else None)
        d = policy.evaluate("write_file", {"path": r"C:\temp\evil.txt"})
        assert d.action is PermissionAction.ASK
        assert "工作区" in d.reason
