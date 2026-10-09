from pathlib import Path
from PyInstaller.utils.hooks import collect_submodules, collect_data_files

ROOT = Path(SPECPATH).parent
BACKEND = ROOT / "scout-backend"
FRONTEND = ROOT / "frontend"
ICON = ROOT / "resources" / "icon.ico"

hiddenimports = [
    "uvicorn.logging",
    "uvicorn.loops.auto",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan.on",
    "websockets",
    "multipart",
    "python_multipart",
    "aiohttp",
    # Windows asyncio loads these native/stdlib modules dynamically.
    # Explicit collection prevents the frozen executable from reaching
    # uvicorn -> asyncio.windows_events and failing with missing _overlapped.
    "_overlapped",
    "_asyncio",
    "_winapi",
    "asyncio.windows_events",
    "asyncio.windows_utils",
    "asyncio.proactor_events",
    "asyncio.selector_events",
    "selectors",
    "socket",
    "concurrent.futures.thread",
]
hiddenimports += collect_submodules("webview")

datas = [
    (str(FRONTEND), "frontend"),
]
for package in ("webview", "tzdata", "yt_dlp"):
    try:
        datas += collect_data_files(package)
    except Exception:
        pass

analysis = Analysis(
    [str(BACKEND / "desktop_launcher.py")],
    pathex=[str(BACKEND)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "numpy", "pandas"],
    noarchive=False,
)

pyz = PYZ(analysis.pure)

exe = EXE(
    pyz,
    analysis.scripts,
    analysis.binaries,
    analysis.datas,
    [],
    name="ScoutBot",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    icon=str(ICON),
)
