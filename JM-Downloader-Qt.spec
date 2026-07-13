# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ["desktop_qt.py"],
    pathex=[],
    binaries=[],
    datas=[
        (
            "jm_downloader/qt/resources/styles_light.qss",
            "jm_downloader/qt/resources",
        ),
        (
            "jm_downloader/qt/resources/styles_dark.qss",
            "jm_downloader/qt/resources",
        )
    ],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "clr",
        "clr_loader",
        "curl_cffi",
        "flask",
        "jmcomic",
        "PyQt5",
        "PyQt6",
        "PySide2",
        "pythonnet",
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
    name="JM-Downloader-Qt",
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
)

debug_exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="JM-Downloader-Qt-Debug",
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
)

coll = COLLECT(
    exe,
    debug_exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="JM-Downloader-Qt-Prototype",
)
