# PyInstaller spec — full desktop client ("词元新学" / AgentAssistant).
#
# Unlike engine.spec (lean hosted MCP engine, no UI), this ships the
# pywebview desktop UI, the knowledge/RAG stack and the agent loop.
#
# Build:  pyinstaller packaging/desktop.spec --noconfirm
# Output: dist/AgentAssistant/AgentAssistant.exe   (onedir)

import os

from PyInstaller.utils.hooks import collect_submodules

block_cipher = None

# SPECPATH is injected by PyInstaller = directory containing this spec.
_ENTRY = os.path.abspath(os.path.join(SPECPATH, "..", "src", "agent_assistant", "main.py"))
_SRC = os.path.abspath(os.path.join(SPECPATH, "..", "src"))
_WEB = os.path.join(_SRC, "agent_assistant", "ui", "web")

hiddenimports = []
# tiktoken registers encodings via plugin modules discovered at runtime.
hiddenimports += ["tiktoken_ext", "tiktoken_ext.openai_public"]
# pywinauto backend is imported dynamically by name.
hiddenimports += collect_submodules("pywinauto")
# Knowledge/RAG stack is imported lazily by the UI path.
hiddenimports += ["chromadb", "chromadb.api", "chromadb.telemetry"]

# Voice stack is heavy (onnxruntime ~200MB) and not required for the core
# learning loop — excluded from the first desktop build.
excludes = [
    "sherpa_onnx", "sherpa_onnx_core",
    "edge_tts", "sounddevice",
    "tkinter", "matplotlib", "pytest",
]

# The built UI is a single self-contained index.html (Vite inlined assets).
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

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="AgentAssistant",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,             # GUI app — no console window
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="AgentAssistant",
)
