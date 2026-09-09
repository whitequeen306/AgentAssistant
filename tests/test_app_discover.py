"""App registry: on-demand discovery when not pre-registered."""

from __future__ import annotations

from pathlib import Path

from agent_assistant.app_registry import AppEntry, AppRegistry, _normalize_key


def test_normalize_key_strips_spaces():
    assert _normalize_key("Qoder CN") == "qodercn"
    assert _normalize_key("Qoder-CN") == "qodercn"


def test_matches_normalized_alias():
    app = AppEntry(name="Qoder", aliases=["qoder cn"], exe_path=r"C:\Qoder\Qoder.exe")
    assert app.matches("Qoder CN")
    assert app.matches("qodercn")


def test_discover_from_install_dir(tmp_path: Path, monkeypatch):
    # Fake LocalAppData\Programs\Qoder CN\Qoder CN.exe
    root = tmp_path / "Local" / "Programs" / "Qoder CN"
    root.mkdir(parents=True)
    exe = root / "Qoder CN.exe"
    exe.write_bytes(b"MZ")

    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    monkeypatch.setenv("ProgramFiles", str(tmp_path / "missing_pf"))
    monkeypatch.setenv("ProgramFiles(x86)", str(tmp_path / "missing_pf86"))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

    reg = AppRegistry(path=tmp_path / "apps.json")
    reg._apps = []  # empty registry

    found = reg.discover_app("Qoder CN", persist=True)
    assert found is not None
    assert found.exe_path
    assert "Qoder" in found.exe_path or "qoder" in found.exe_path.lower()
    # persisted
    assert reg.find_app("Qoder CN") is not None


def test_discover_via_explicit_exe_path(tmp_path: Path):
    exe = tmp_path / "MyApp.exe"
    exe.write_bytes(b"MZ")
    reg = AppRegistry(path=tmp_path / "apps.json")
    reg._apps = []
    found = reg.discover_app(str(exe), persist=False)
    assert found is not None
    assert Path(found.exe_path).resolve() == exe.resolve()
