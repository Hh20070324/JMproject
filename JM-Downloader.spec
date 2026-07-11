# -*- mode: python ; coding: utf-8 -*-

from PyInstaller.utils.hooks import collect_all


jm_datas, jm_binaries, jm_hiddenimports = collect_all("jmcomic")
curl_datas, curl_binaries, curl_hiddenimports = collect_all("curl_cffi")
webview_datas, webview_binaries, webview_hiddenimports = collect_all("webview")

a = Analysis(
    ["desktop.py"],
    pathex=[],
    binaries=jm_binaries + curl_binaries + webview_binaries,
    datas=[("static", "static")] + jm_datas + curl_datas + webview_datas,
    hiddenimports=jm_hiddenimports + curl_hiddenimports + webview_hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="JM-Downloader",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    version="version_info.txt",
)

debug_exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="JM-Downloader-Debug",
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
    version="version_info.txt",
)

coll = COLLECT(
    exe,
    debug_exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="JM-Downloader",
)
