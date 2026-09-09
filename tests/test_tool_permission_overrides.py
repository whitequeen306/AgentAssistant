"""User per-tool permission overrides (settings page: 自动运行 / 需要询问)."""

from __future__ import annotations

from agent_assistant.tools.permission import (
    PermissionAction,
    UserOverridePolicy,
)


def _policy_with(overrides: dict[str, str]) -> UserOverridePolicy:
    return UserOverridePolicy(get_override=lambda name: overrides.get(name))


class TestUserOverridePolicy:
    def test_auto_override_allows_default_ask_tool(self):
        policy = _policy_with({"kill_process": "auto"})
        d = policy.evaluate("kill_process", {"pid": 123})
        assert d.action is PermissionAction.ALLOW
        assert "user override" in d.reason

    def test_ask_override_forces_default_allow_tool(self):
        policy = _policy_with({"web_search": "ask"})
        d = policy.evaluate("web_search", {"query": "x"})
        assert d.action is PermissionAction.ASK

    def test_no_override_keeps_defaults(self):
        policy = _policy_with({})
        assert policy.evaluate("kill_process", {}).action is PermissionAction.ASK
        assert policy.evaluate("web_search", {}).action is PermissionAction.ALLOW
        assert policy.evaluate("ui_inspect", {}).action is PermissionAction.ALLOW
        assert policy.evaluate("ui_click", {}).action is PermissionAction.ASK

    def test_guardrail_beats_auto_override(self):
        """Dangerous commands still ASK even when run_command is set to auto."""
        policy = _policy_with({"run_command": "auto"})
        safe = policy.evaluate("run_command", {"command": "dir"})
        assert safe.action is PermissionAction.ALLOW
        dangerous = policy.evaluate("run_command", {"command": "rm -rf /"})
        assert dangerous.action is PermissionAction.ASK
        assert "dangerous" in dangerous.reason

    def test_guardrail_system_path_beats_auto_override(self):
        policy = _policy_with({"write_file": "auto"})
        normal = policy.evaluate("write_file", {"path": "notes/a.md"})
        assert normal.action is PermissionAction.ALLOW
        system = policy.evaluate("write_file", {"path": r"C:\Windows\x.dll"})
        assert system.action is PermissionAction.ASK

    def test_invalid_override_ignored(self):
        policy = _policy_with({"kill_process": "yolo"})
        assert policy.evaluate("kill_process", {}).action is PermissionAction.ASK

    def test_override_getter_exception_falls_back(self):
        def boom(_name: str) -> str:
            raise RuntimeError("store unavailable")

        policy = UserOverridePolicy(get_override=boom)
        assert policy.evaluate("kill_process", {}).action is PermissionAction.ASK

    def test_requires_confirm_fallback_preserved(self):
        """Tools with requires_confirm but no rule still ASK without override."""
        policy = _policy_with({})
        d = policy.evaluate("unknown_tool", {}, requires_confirm=True)
        assert d.action is PermissionAction.ASK
        d2 = policy.evaluate("unknown_tool", {}, requires_confirm=False)
        assert d2.action is PermissionAction.ALLOW


class TestGetToolOverride:
    def test_reads_store_value(self, tmp_path, monkeypatch):
        from agent_assistant.ui import store as store_mod

        monkeypatch.setattr(store_mod, "ui_store", store_mod.UIStore(tmp_path / "t.db"))
        from agent_assistant.tools.permission import get_tool_override

        assert get_tool_override("ui_click") is None
        store_mod.ui_store.set_setting("tool_perm.ui_click", "auto")
        assert get_tool_override("ui_click") == "auto"
        store_mod.ui_store.set_setting("tool_perm.ui_click", "garbage")
        assert get_tool_override("ui_click") is None


class TestBridgeApi:
    def _bridge(self, tmp_path, monkeypatch):
        from agent_assistant.tools.register import register_all_tools
        from agent_assistant.ui import store as store_mod
        from agent_assistant.ui.bridge import ApiBridge

        register_all_tools()  # idempotent (overwrites with a warning)
        monkeypatch.setattr(store_mod, "ui_store", store_mod.UIStore(tmp_path / "b.db"))
        return ApiBridge()

    def test_get_tool_permissions_shape(self, tmp_path, monkeypatch):
        bridge = self._bridge(tmp_path, monkeypatch)
        rows = bridge.get_tool_permissions()
        assert rows, "expected registered tools"
        for row in rows:
            assert set(row.keys()) == {"name", "label", "permission"}
            assert row["permission"] in ("auto", "ask")
        by_name = {r["name"]: r for r in rows}
        # Defaults: read-only inspect is auto, click is ask
        assert by_name["ui_inspect"]["permission"] == "auto"
        assert by_name["ui_click"]["permission"] == "ask"
        # Labels are Chinese
        assert by_name["ui_click"]["label"] == "点击控件"

    def test_set_then_get_roundtrip(self, tmp_path, monkeypatch):
        bridge = self._bridge(tmp_path, monkeypatch)
        result = bridge.set_tool_permission("ui_click", "auto")
        assert result["ok"]
        rows = {r["name"]: r for r in bridge.get_tool_permissions()}
        assert rows["ui_click"]["permission"] == "auto"

    def test_set_rejects_bad_mode(self, tmp_path, monkeypatch):
        bridge = self._bridge(tmp_path, monkeypatch)
        assert not bridge.set_tool_permission("ui_click", "yolo")["ok"]

    def test_set_rejects_unknown_tool(self, tmp_path, monkeypatch):
        bridge = self._bridge(tmp_path, monkeypatch)
        assert not bridge.set_tool_permission("nope_tool", "auto")["ok"]
