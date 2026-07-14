# -*- mode: python ; coding: utf-8 -*-

from PyInstaller.utils.hooks import collect_all


jm_datas, jm_binaries, jm_hiddenimports = collect_all("jmcomic")

a = Analysis(
    ["desktop.py"],
    pathex=[],
    binaries=jm_binaries,
    datas=[
        (
            "jm_downloader/qt/resources/styles_light.qss",
            "jm_downloader/qt/resources",
        ),
        (
            "jm_downloader/qt/resources/styles_dark.qss",
            "jm_downloader/qt/resources",
        ),
        (
            "jm_downloader/qt/resources/icons/*.svg",
            "jm_downloader/qt/resources/icons",
        ),
    ] + jm_datas,
    hiddenimports=jm_hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "clr",
        "clr_loader",
        "flask",
        "PyQt5",
        "PyQt6",
        "PySide2",
        "packaging",
        "pythonnet",
        "setuptools",
        "tkinter",
        "webview",
        "werkzeug",
    ],
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
    upx=False,
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
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    version="version_info_debug.txt",
)

coll = COLLECT(
    exe,
    debug_exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="JM-Downloader",
)
