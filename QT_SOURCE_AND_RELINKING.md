# Qt Source and Relinking Information

The Windows `v2.2.0` distribution contains unmodified upstream binaries from
Qt 6.11.1, PySide6 Essentials 6.11.1, and Shiboken6 6.11.1. JM-Downloader
uses these components under the GNU Lesser General Public License version 3
only. The LGPL and the GPL text incorporated by it are provided in
`LICENSES/LGPL-3.0-only.txt` and `LICENSES/GPL-3.0-only.txt`.

## Corresponding Source

The exact upstream source releases are available from The Qt Company:

- Qt 6.11.1:
  `https://download.qt.io/official_releases/qt/6.11/6.11.1/single/qt-everywhere-src-6.11.1.tar.xz`
  (SHA256 `252acef8c5ae68074d91cadba2ee4a83465051bbb970dd26e8f0daa0f3904e03`).
- PySide6 and Shiboken6 6.11.1:
  `https://download.qt.io/official_releases/QtForPython/pyside6/PySide6-6.11.1-src/pyside-setup-everywhere-src-6.11.1.tar.xz`
  (SHA256 `6ffd9835bb0dd2c56f061d62f1616bb1707cfc0202b80e3165d6be087f3965e2`).
- The Windows wheel used for the build:
  `https://download.qt.io/official_releases/QtForPython/pyside6-essentials/pyside6_essentials-6.11.1-cp310-abi3-win_amd64.whl`
  (SHA256 `63311bd48e32c584599ab04b9ef7c324082374cd2c9fa533f978fb893bb47e40`).

If an official source link becomes unavailable, any recipient of the v2.2.0
Windows binaries may request the corresponding source for at least three
years after the last public distribution of this version by opening an issue
at `https://github.com/Hh20070324/JMproject/issues`. The source will be
provided for no more than the reasonable cost of physically conveying it.

## Replacing the Libraries

The application is distributed as a PyInstaller `onedir` folder. Qt and the
Python bindings remain separate dynamically loaded DLL and PYD files. A
recipient may use an ABI-compatible community build by closing the
application, backing up the original files, and replacing the applicable
files while preserving their names and relative locations:

- `_internal/PySide6/Qt6*.dll` and `_internal/PySide6/plugins/`
- `_internal/PySide6/*.pyd` and `_internal/PySide6/pyside6.abi3.dll`
- `_internal/shiboken6/*.pyd` and `_internal/shiboken6/shiboken6.abi3.dll`

`JM-Downloader-Debug.exe` can then be used to inspect startup errors. The
project applies no DRM, signature check, or integrity check that prevents a
recipient from running a compatible modified library. Its license also does
not restrict reverse engineering performed to debug modifications to the
LGPL-covered components.

Additional upstream attributions and license texts are reproduced in
`QT_THIRD_PARTY_NOTICES.txt`. This document records the project's engineering
compliance measures and is not legal advice.
