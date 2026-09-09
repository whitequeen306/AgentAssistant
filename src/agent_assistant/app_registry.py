"""App registry — maps app names/aliases to launch info.

Stored as a JSON file. Supports:
- exe paths (regular desktop apps)
- protocol URIs (UWP/Store apps)
- launcher paths (game with launchers)
- window title patterns (for focus detection)

First-run: scans Start Menu shortcuts to auto-populate.
On miss: discover_app() re-scans Start Menu / PATH / common install dirs
and auto-registers the match.
Manually editable by the user.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from agent_assistant.config import settings

logger = logging.getLogger(__name__)


def _normalize_key(text: str) -> str:
    """Lowercase + strip spaces/punctuation for loose matching (Qoder CN ↔ QoderCN)."""
    return re.sub(r"[\s\-_.]+", "", (text or "").lower())


@dataclass
class AppEntry:
    """One registered application."""

    name: str  # display name
    aliases: list[str] = field(default_factory=list)  # alternative names
    exe_path: str | None = None  # path to .exe
    protocol_uri: str | None = None  # e.g. "whatsapp:" for UWP
    window_title_pattern: str | None = None  # for detecting running instance
    workdir: str | None = None

    def matches(self, query: str) -> bool:
        """Check if query matches this app's name or aliases (case-insensitive)."""
        q = query.lower().strip()
        if not q:
            return False
        if q == self.name.lower():
            return True
        if any(q == a.lower() for a in self.aliases):
            return True
        qn = _normalize_key(query)
        if qn and qn == _normalize_key(self.name):
            return True
        return any(qn == _normalize_key(a) for a in self.aliases)


class AppRegistry:
    """Manages the app registry JSON file."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or settings.resolved_app_registry
        self._apps: list[AppEntry] = []
        self._load()

    def _load(self) -> None:
        if self._path.exists():
            try:
                data = json.loads(self._path.read_text(encoding="utf-8"))
                self._apps = [AppEntry(**item) for item in data.get("apps", [])]
                logger.info("Loaded %d apps from registry", len(self._apps))
            except (json.JSONDecodeError, TypeError) as e:
                logger.warning("Failed to load app registry: %s", e)
                self._apps = []
        else:
            self._apps = []
            self._init_default_registry()

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "apps": [
                {
                    "name": a.name,
                    "aliases": a.aliases,
                    "exe_path": a.exe_path,
                    "protocol_uri": a.protocol_uri,
                    "window_title_pattern": a.window_title_pattern,
                    "workdir": a.workdir,
                }
                for a in self._apps
            ]
        }
        self._path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def _init_default_registry(self) -> None:
        """Create a starter registry with common apps + scan Start Menu."""
        defaults = [
            AppEntry(
                name="Cursor",
                aliases=["cursor"],
                exe_path=None,
                window_title_pattern="Cursor",
            ),
            AppEntry(
                name="Notepad",
                aliases=["记事本", "notepad"],
                exe_path="notepad.exe",
                window_title_pattern="Notepad",
            ),
            AppEntry(
                name="Calculator",
                aliases=["计算器", "calc"],
                protocol_uri="ms-calculator:",
                window_title_pattern="Calculator",
            ),
            AppEntry(
                name="Explorer",
                aliases=["文件管理器", "explorer", "资源管理器"],
                exe_path="explorer.exe",
                window_title_pattern="",
            ),
            AppEntry(
                name="Chrome",
                aliases=["谷歌浏览器", "chrome", "google chrome"],
                exe_path=None,
                window_title_pattern="Google Chrome",
            ),
            AppEntry(
                name="Edge",
                aliases=["edge", "microsoft edge"],
                exe_path="msedge.exe",
                window_title_pattern="Microsoft Edge",
            ),
        ]
        self._apps = defaults
        self._scan_start_menu()
        self._save()

    def _start_menu_dirs(self) -> list[Path]:
        return [
            Path(os.environ.get("APPDATA", ""))
            / "Microsoft"
            / "Windows"
            / "Start Menu"
            / "Programs",
            Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData"))
            / "Microsoft"
            / "Windows"
            / "Start Menu"
            / "Programs",
        ]

    def _scan_start_menu(self) -> None:
        """Scan Start Menu shortcuts to auto-populate the registry."""
        existing_names = {a.name.lower() for a in self._apps}

        for sm_dir in self._start_menu_dirs():
            if not sm_dir.exists():
                continue
            for lnk_file in sm_dir.rglob("*.lnk"):
                app_name = lnk_file.stem
                if app_name.lower() in existing_names:
                    continue
                target = self._resolve_shortcut(lnk_file)
                if target:
                    entry = AppEntry(
                        name=app_name,
                        aliases=[app_name.lower()],
                        exe_path=target,
                        window_title_pattern=app_name,
                    )
                    self._apps.append(entry)
                    existing_names.add(app_name.lower())

        logger.info("Registry now has %d apps after Start Menu scan", len(self._apps))

    @staticmethod
    def _resolve_shortcut(lnk_path: Path) -> str | None:
        """Resolve a .lnk shortcut to its target path using COM."""
        try:
            import pythoncom  # type: ignore[import-untyped]
            from win32com.shell import shell  # type: ignore[import-untyped]

            link = pythoncom.CoCreateInstance(
                shell.CLSID_ShellLink,
                None,
                pythoncom.CLSCTX_INPROC_SERVER,
                shell.IID_IShellLink,
            )
            link.QueryInterface(pythoncom.IID_IPersistFile).Load(str(lnk_path))
            target, _ = link.GetPath(shell.SLGP_UNCPRIORITY)
            return target if target else None
        except Exception:
            return None

    def find_app(self, query: str) -> AppEntry | None:
        """Find an app by name or alias (registry only, no disk scan)."""
        for app in self._apps:
            if app.matches(query):
                return app
        q = query.lower().strip()
        qn = _normalize_key(query)
        for app in self._apps:
            if q in app.name.lower() or any(q in a.lower() for a in app.aliases):
                return app
            if qn and (
                qn in _normalize_key(app.name)
                or any(qn in _normalize_key(a) for a in app.aliases)
            ):
                return app
        return None

    def discover_app(self, query: str, *, persist: bool = True) -> AppEntry | None:
        """Find app on this machine when not in registry; optionally auto-register.

        Order: registry → Start Menu .lnk → where.exe → common install dirs.
        """
        query = (query or "").strip()
        if not query:
            return None

        hit = self.find_app(query)
        if hit and (hit.exe_path or hit.protocol_uri):
            return hit

        discovered = (
            self._discover_from_start_menu(query)
            or self._discover_via_where(query)
            or self._discover_in_install_dirs(query)
        )
        if discovered is None:
            return hit  # may be a title-only stub without path

        if persist:
            self.add_app(discovered)
            logger.info(
                "Discovered and registered app %r → %s",
                discovered.name,
                discovered.exe_path or discovered.protocol_uri,
            )
        return discovered

    def _query_matches_name(self, query: str, name: str) -> bool:
        q = query.lower().strip()
        n = name.lower().strip()
        if not q or not n:
            return False
        if q == n or q in n or n in q:
            return True
        qn, nn = _normalize_key(query), _normalize_key(name)
        return bool(qn and nn and (qn == nn or qn in nn or nn in qn))

    def _discover_from_start_menu(self, query: str) -> AppEntry | None:
        """Match Start Menu shortcuts by stem (live scan, not only first-run)."""
        best: tuple[int, AppEntry] | None = None
        qn = _normalize_key(query)
        for sm_dir in self._start_menu_dirs():
            if not sm_dir.exists():
                continue
            try:
                lnks = list(sm_dir.rglob("*.lnk"))
            except OSError:
                continue
            for lnk_file in lnks:
                stem = lnk_file.stem
                if not self._query_matches_name(query, stem):
                    continue
                target = self._resolve_shortcut(lnk_file)
                if not target:
                    continue
                score = (
                    3
                    if _normalize_key(stem) == qn
                    else 2
                    if qn in _normalize_key(stem)
                    else 1
                )
                entry = AppEntry(
                    name=stem,
                    aliases=list({stem.lower(), query.lower()}),
                    exe_path=target,
                    window_title_pattern=stem,
                )
                if best is None or score > best[0]:
                    best = (score, entry)
        return best[1] if best else None

    def _discover_via_where(self, query: str) -> AppEntry | None:
        """Use where.exe for bare names (e.g. notepad, code)."""
        token = query.strip().strip('"')
        if not token or any(c in token for c in r'\/:*?"<>|'):
            p = Path(token)
            if p.is_file() and p.suffix.lower() == ".exe":
                return AppEntry(
                    name=p.stem,
                    aliases=[query.lower()],
                    exe_path=str(p.resolve()),
                    window_title_pattern=p.stem,
                )
            return None
        candidates = [token]
        compact = _normalize_key(token)
        if compact and compact != token.lower():
            candidates.append(compact)
        for cand in candidates:
            try:
                r = subprocess.run(
                    ["where.exe", cand],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    encoding="utf-8",
                    errors="replace",
                )
            except (OSError, subprocess.TimeoutExpired):
                continue
            if r.returncode != 0:
                continue
            for line in (r.stdout or "").splitlines():
                path = line.strip()
                if path.lower().endswith(".exe") and Path(path).is_file():
                    return AppEntry(
                        name=Path(path).stem,
                        aliases=[query.lower(), cand.lower()],
                        exe_path=path,
                        window_title_pattern=Path(path).stem,
                    )
        return None

    def _discover_in_install_dirs(self, query: str) -> AppEntry | None:
        """Shallow search under Program Files / LocalAppData for *query*.exe."""
        qn = _normalize_key(query)
        if len(qn) < 2:
            return None
        roots: list[Path] = []
        for key in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
            raw = os.environ.get(key)
            if raw:
                roots.append(Path(raw))
        home = Path.home()
        roots.extend([
            home / "AppData" / "Local" / "Programs",
            home / "AppData" / "Local",
        ])

        hits: list[Path] = []
        for root in roots:
            if not root.is_dir():
                continue
            try:
                for pattern in ("*/*.exe", "*/*/*.exe", "*.exe"):
                    for p in root.glob(pattern):
                        if not p.is_file():
                            continue
                        if qn in _normalize_key(p.stem) or qn in _normalize_key(p.parent.name):
                            hits.append(p)
            except OSError:
                continue
            if len(hits) >= 8:
                break

        if not hits:
            return None
        hits.sort(
            key=lambda p: (
                0 if _normalize_key(p.stem) == qn else 1,
                len(p.stem),
                str(p),
            )
        )
        best = hits[0]
        return AppEntry(
            name=best.stem,
            aliases=[query.lower(), best.stem.lower()],
            exe_path=str(best.resolve()),
            window_title_pattern=best.stem,
            workdir=str(best.parent),
        )

    def add_app(self, entry: AppEntry) -> None:
        """Add or update an app entry."""
        self._apps = [a for a in self._apps if a.name.lower() != entry.name.lower()]
        self._apps.append(entry)
        self._save()

    def list_apps(self) -> list[AppEntry]:
        return list(self._apps)


# Singleton
app_registry = AppRegistry()
