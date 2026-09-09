# PyInstaller spec — lean hosted MCP engine ("AgentEngine").
#
# Ships the agent loop + tools + confirm framework, WITHOUT voice
# (sherpa-onnx / edge-tts / sounddevice), WITHOUT the pywebview UI, and
# WITHOUT chromadb. Those are never on the hosted entry's runtime path;
# excluding them keeps the package small (no onnxruntime pulled in).
#
# Build:  pyinstaller packaging/engine.spec --noconfirm
# Output: dist/AgentEngine/AgentEngine.exe  (onedir — fast startup, spawned
#         repeatedly by the LianYu host)

import os

from PyInstaller.utils.hooks import collect_submodules

block_cipher = None

# SPECPATH is injected by PyInstaller = directory containing this spec.
_ENTRY = os.path.join(SPECPATH, "engine_entry.py")
_SRC = os.path.abspath(os.path.join(SPECPATH, "..", "src"))

hiddenimports = []
# tiktoken registers encodings via plugin modules discovered at runtime;
# PyInstaller can't see them statically.
hiddenimports += ["tiktoken_ext", "tiktoken_ext.openai_public"]
# pywinauto backend is imported dynamically by name.
hiddenimports += collect_submodules("pywinauto")

# Heavy / UI / voice deps that must NOT be bundled. Anything importing these
# (ui.window, voice.*) is dead code in hosted mode.
excludes = [
    "pywebview", "webview",
    "sherpa_onnx", "sherpa_onnx_core",
    "edge_tts", "sounddevice",
    "chromadb", "onnxruntime",
    "tkinter", "matplotlib", "PIL", "pytest",
]

a = Analysis(
    [_ENTRY],
    pathex=[_SRC],
    binaries=[],
    datas=[],
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
    name="AgentEngine",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,          # stdio JSON-RPC server — needs a console/pipe
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
    name="AgentEngine",
)
