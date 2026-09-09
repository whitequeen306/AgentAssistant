"""Right-click context menu registration — Windows Registry shell extension.

Two entries (HKCU, no admin needed):

1. "交给助手" (Hand to Assistant) — legacy text-grab entry for files and
   desktop/folder background. Per docs/04-features.md §4.2.
2. "Learn with Assistant" — ChatGPT-style entry on files AND folders.
   Clicked → `--right-click <path>` → single-instance IPC → the running
   assistant opens a conversation that reads the file / lists the folder.

Both ride the same J23 plumbing (main.py `_handle_right_click`); the
distinction is only which shell keys they occupy.
"""

from __future__ import annotations

import logging
import sys
import winreg
from pathlib import Path

logger = logging.getLogger(__name__)

# ─── 交给助手 (Hand to Assistant) ──────────────────────────────────────────────
# "*\shell" covers file right-click; "Directory\Background\shell" covers
# right-click on empty space inside a folder / on the desktop.
SHELL_KEY_PATHS = [
    r"Software\Classes\*\shell\AgentAssistant",
    r"Software\Classes\Directory\Background\shell\AgentAssistant",
]

MENU_LABEL = "交给助手 (&A)"

# ─── Learn with Assistant ─────────────────────────────────────────────────────
# "*\shell" matches files; "Directory\shell" matches folder icons themselves
# (Background only covers empty space — a folder entry needs Directory\shell).
LEARN_SHELL_KEY_PATHS = [
    r"Software\Classes\*\shell\AgentAssistantLearn",
    r"Software\Classes\Directory\shell\AgentAssistantLearn",
]

LEARN_MENU_LABEL = "Learn with Assistant (&L)"


def get_assistant_exe_path() -> str:
    """Get the path to the assistant executable/script for the command."""
    # When running as installed package
    python_exe = sys.executable
    return f'"{python_exe}" -m agent_assistant.main --right-click "%1"'


def _app_icon_path() -> Path | None:
    """Brand icon shipped inside the package (assets/app.ico)."""
    ico = Path(__file__).resolve().parents[1] / "assets" / "app.ico"
    return ico if ico.exists() else None


def _learn_command() -> str:
    """Command for the Learn entry — prefer pythonw so no console flashes.

    The right-click process either forwards via IPC and exits or boots the UI;
    neither needs a console window. pythonw discards the error prints in
    main.py harmlessly (stdout is None under pythonw).
    """
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    exe = pythonw if pythonw.exists() else Path(sys.executable)
    return f'"{exe}" -m agent_assistant.main --right-click "%1"'


def _learn_icon() -> str:
    """Icon for the Learn entry — the brand .ico; fallback: the Python
    runtime's own icon (always present next to sys.executable)."""
    ico = _app_icon_path()
    if ico is not None:
        return f'"{ico}",0'
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    exe = pythonw if pythonw.exists() else Path(sys.executable)
    return f'"{exe}",0'


def _write_shell_entries(key_paths: list[str], label: str, command: str, icon: str) -> None:
    """Write one verb (label + optional icon + command) under each key path."""
    for key_path in key_paths:
        key = winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, key_path)
        winreg.SetValueEx(key, "", 0, winreg.REG_SZ, label)
        if icon:
            winreg.SetValueEx(key, "Icon", 0, winreg.REG_SZ, icon)
        winreg.CloseKey(key)

        cmd_key = winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, f"{key_path}\\command")
        winreg.SetValueEx(cmd_key, "", 0, winreg.REG_SZ, command)
        winreg.CloseKey(cmd_key)

        logger.info("Registered context menu: %s", key_path)


def _delete_shell_entries(key_paths: list[str]) -> None:
    for key_path in key_paths:
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, f"{key_path}\\command")
        except FileNotFoundError:
            pass
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key_path)
        except FileNotFoundError:
            pass
        logger.info("Removed context menu: %s", key_path)


def _keys_exist(key_paths: list[str]) -> bool:
    try:
        key = winreg.OpenKeyEx(winreg.HKEY_CURRENT_USER, key_paths[0])
        winreg.CloseKey(key)
        return True
    except FileNotFoundError:
        return False


def register_context_menu() -> bool:
    """Register the 交给助手 entry in Windows Registry (HKCU, no admin)."""
    try:
        _write_shell_entries(SHELL_KEY_PATHS, MENU_LABEL, get_assistant_exe_path(), "shell32.dll,14")
        return True
    except OSError as e:
        logger.error("Failed to register context menu: %s", e)
        return False


def unregister_context_menu() -> bool:
    """Remove the 交给助手 entry."""
    try:
        _delete_shell_entries(SHELL_KEY_PATHS)
        return True
    except OSError as e:
        logger.error("Failed to unregister context menu: %s", e)
        return False


def is_registered() -> bool:
    """Check if the 交给助手 entry is already registered."""
    return _keys_exist(SHELL_KEY_PATHS)


def register_learn_context_menu() -> bool:
    """Register the Learn with Assistant entry for files and folders (HKCU)."""
    try:
        _write_shell_entries(
            LEARN_SHELL_KEY_PATHS, LEARN_MENU_LABEL, _learn_command(), _learn_icon()
        )
        return True
    except OSError as e:
        logger.error("Failed to register Learn context menu: %s", e)
        return False


def unregister_learn_context_menu() -> bool:
    """Remove the Learn with Assistant entry."""
    try:
        _delete_shell_entries(LEARN_SHELL_KEY_PATHS)
        return True
    except OSError as e:
        logger.error("Failed to unregister Learn context menu: %s", e)
        return False


def learn_is_registered() -> bool:
    """Check if the Learn with Assistant entry is already registered."""
    return _keys_exist(LEARN_SHELL_KEY_PATHS)
