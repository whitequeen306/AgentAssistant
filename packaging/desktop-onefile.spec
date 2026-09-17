# PyInstaller spec — desktop client, SINGLE-FILE build ("词元新学" / AgentAssistant).
#
# Difference from desktop.spec (onedir): this one bundles everything into one
# AgentAssistant.exe. On launch it unpacks itself into %TEMP%\_MEIxxxx, so
# startup is a few seconds slower than onedir, but distribution is a single file.
#
# Build:  pyinstaller packaging/desktop-onefile.spec --noconfirm
# Output: dist/AgentAssistant.exe

import os

from PyInstaller.utils.hooks import collect_submodules

block_cipher = None

_ENTRY = os.path.abspath(os.path.join(SPECPATH, "..", "src", "agent_assistant", "main.py"))
_SRC = os.path.abspath(os.path.join(SPECPATH, "..", "src"))
_WEB = os.path.join(_SRC, "agent_assistant", "ui", "web")
_ICON = os.path.abspath(os.path.join(SPECPATH, "..", "assets", "logo.ico"))

hiddenimports = []
hiddenimports += ["tiktoken_ext", "tiktoken_ext.openai_public"]
hiddenimports += collect_submodules("pywinauto")
hiddenimports += ["chromadb", "chromadb.api", "chromadb.telemetry"]

# Voice stack (onnxruntime ~200MB) — not needed for the core learning loop.
# IPython / jedi / parso / prompt_toolkit are dev-time tooling, never imported by the app.
# NOTE: pythonnet must NOT be excluded. On Windows pywebview resolves its backend
# via guilib.py which hardcodes `guis = [import_winforms]` — WinForms is the only
# path, and it imports `clr` from pythonnet. Excluding it makes the app die at
# startup with "You must have pythonnet installed in order to use pywebview".
# (Verified the hard way on 2026-09-12.)
excludes = [
    "sherpa_onnx", "sherpa_onnx_core",
    "edge_tts", "sounddevice",
    "tkinter", "matplotlib", "pytest",
    "IPython", "jedi", "parso", "traitlets", "prompt_toolkit",
    "ipython_pygments_lexers",
]

datas = [(os.path.join(_WEB, "index.html"), "agent_assistant/ui/web")]

a = Analysis(
    [_ENTRY],
    pathex=[_SRC],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

# ---- Trim oversized payloads the app never touches at runtime ----
# babel's locale-data (~29MB) is pulled in by courlan/trafilatura; only the tiny
# babel package code is ever used, the locale tables are not.
a.datas = [
    d for d in a.datas
    if not ("babel" in d[0].lower() and "locale-data" in d[0].lower())
]
# Pillow's AVIF plugin (~7.5MB) — study material is PDF/PNG/JPEG, never AVIF.
a.binaries = [b for b in a.binaries if "_avif" not in b[0].lower()]

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="AgentAssistant",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,          # UPX trips antivirus heuristics on PyInstaller builds
    runtime_tmpdir=None,
    console=False,      # GUI app — no console window
    icon=_ICON,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
