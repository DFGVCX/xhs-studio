# -*- mode: python ; coding: utf-8 -*-

import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules


selenium_datas, selenium_binaries, selenium_hiddenimports = collect_all("selenium")
bundled_browser_dir = os.environ.get("XHS_BUNDLED_BROWSER_DIR")
bundled_browser_datas = []
if bundled_browser_dir:
    bundled_browser_path = Path(bundled_browser_dir).resolve()
    if not (bundled_browser_path / "chrome-win64" / "chrome.exe").is_file():
        raise RuntimeError("Bundled Chrome executable is missing")
    if not (bundled_browser_path / "chromedriver-win64" / "chromedriver.exe").is_file():
        raise RuntimeError("Bundled ChromeDriver executable is missing")
    bundled_browser_datas.append((str(bundled_browser_path), "browser"))

a = Analysis(
    ["run_console.py"],
    pathex=[],
    binaries=selenium_binaries,
    datas=[
        ("static", "static"),
        ("xhs_console/extract_note.js", "xhs_console"),
    ] + selenium_datas + bundled_browser_datas,
    hiddenimports=selenium_hiddenimports + collect_submodules("uvicorn"),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["pytest", "httpx"],
    noarchive=False,
    optimize=1,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="XHS-Studio",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
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
    upx=True,
    upx_exclude=[],
    name="XHS-Studio",
)
