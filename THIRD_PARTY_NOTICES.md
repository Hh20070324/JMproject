# Third-Party Notices

This project is based in part on JMComic-Crawler-Python.

The portable Windows distribution also includes CPython and dynamically
loaded Qt for Python components, Python runtime packages, and a PyInstaller
bootloader.

## JMComic-Crawler-Python

* Original project: JMComic-Crawler-Python
* Original author: hect0x7
* Source repository: https://github.com/hect0x7/JMComic-Crawler-Python
* License: MIT License
* Original copyright: Copyright (c) 2023 hect0x7

The original project has been modified and extended to provide features such as
a graphical user interface, download management, simplified configuration, and
a portable ready-to-use distribution.

The original MIT License and copyright notice are retained in accordance with
the license requirements.

## Qt, PySide6, and Shiboken6

The Windows distribution uses Qt 6.11.1, PySide6 Essentials 6.11.1, and
Shiboken6 6.11.1 under the GNU Lesser General Public License version 3 only.
These components remain copyright of The Qt Company Ltd. and other
contributors.

The upstream binaries are not modified. They remain separate dynamically
loaded DLL and PYD files in the portable `onedir` distribution and may be
replaced or relinked by recipients with ABI-compatible builds. JM-Downloader
does not restrict reverse engineering performed to debug modifications to
these LGPL-covered components.

See the following files distributed with the application:

* `LICENSES/LGPL-3.0-only.txt`
* `LICENSES/GPL-3.0-only.txt`
* `QT_SOURCE_AND_RELINKING.md`
* `QT_THIRD_PARTY_NOTICES.txt`

Official project and licensing information:

* Qt source and releases: https://download.qt.io/official_releases/qt/
* Qt for Python source: https://download.qt.io/official_releases/QtForPython/
* Qt licensing: https://doc.qt.io/qt-6/licensing.html
* Qt for Python licensing: https://doc.qt.io/qtforpython-6/

## CPython

The portable Windows distribution includes a CPython 3.14 runtime. Python is
copyright Python Software Foundation and other contributors. Its license and
the third-party notices shipped with it are reproduced in
`LICENSES/Python-3.14.txt`.

## Bundled Python Components

The Windows x64 portable distribution includes the following runtime
components. Their original license texts are reproduced under `LICENSES/`.

| Component | Version | License | Source |
| --- | --- | --- | --- |
| commonX | 0.6.40 | MIT | https://github.com/hect0x7/common |
| curl_cffi | 0.15.0 | MIT | https://github.com/lexiforest/curl_cffi |
| certifi | 2026.6.17 | MPL-2.0 | https://github.com/certifi/python-certifi |
| cffi | 2.0.0 | MIT metadata; MIT No Attribution text | https://github.com/python-cffi/cffi |
| Pillow | 12.2.0 | MIT-CMU and incorporated notices | https://github.com/python-pillow/Pillow |
| pycparser | 3.0 | BSD-3-Clause | https://github.com/eliben/pycparser |
| PyCryptodome | 3.23.0 | BSD and public domain | https://github.com/Legrandin/pycryptodome |
| PyYAML | 6.0.3 | MIT | https://github.com/yaml/pyyaml |
| typing_extensions | 4.16.0 | PSF-2.0 | https://github.com/python/typing_extensions |

JMComic-Crawler-Python 2.7.1 is described separately above. Its original
license is also reproduced in
`LICENSES/JMComic-Crawler-Python-2.7.1.txt`.

certifi's bundled CA certificate data is distributed under MPL-2.0. The
applicable notice is retained verbatim in
`LICENSES/certifi-2026.6.17.txt`.

Pillow's installed wheel contains compiled image, color-management, font, and
codec support. `LICENSES/Pillow-12.2.0.txt` is the complete license file from
that wheel, including all incorporated third-party notices.

## curl_cffi Native Components and Adapted Code

The Windows x64 curl_cffi 0.15.0 wheel contains a fully static
`curl_cffi/_wrapper.pyd`. The exact runtime version string used for this
release reports:

`libcurl/8.15.0-IMPERSONATE BoringSSL zlib/1.3 brotli/1.1.0 zstd/1.5.6
WinIDN nghttp2/1.63.0 ngtcp2/1.20.0 nghttp3/1.15.0`

WinIDN is provided by Windows rather than copied into the distribution.
BoringSSL does not publish a release number in the wrapper's version string.

curl_cffi also acknowledges that its request header and cookie code was copied
and adapted from HTTPX under the BSD license. Full upstream notices and source
links for curl, curl-impersonate, BoringSSL, zlib, Brotli, Zstandard, nghttp2,
ngtcp2, nghttp3, and HTTPX are retained in
`LICENSES/curl_cffi-0.15.0-native.txt`.

## PyInstaller

The executable bootloader and bundled runtime hooks are produced with
PyInstaller 6.21.0. PyInstaller is distributed under GPL-2.0-or-later with a
special Bootloader Exception that permits distribution of programs built with
PyInstaller. Its runtime hooks include Apache-2.0-licensed material, and its
isolated helper is MIT-licensed.

The complete upstream terms are retained in
`LICENSES/PyInstaller-6.21.0.txt`. Source:
https://github.com/pyinstaller/pyinstaller.

## Game Icon Pack

The desktop interface includes selected SVG icons from Game Icon Pack by
Nieobie. The icon pack is released under the CC0 1.0 Universal Public Domain
Dedication.

* Source: https://github.com/Nieobie/game-icon-pack
* License: CC0 1.0 Universal
* Local notice: `LICENSES/Game-Icon-Pack-CC0-1.0.txt`

Only the SVG files used by the application are included in the repository and
portable distribution. The complete local source asset folder is development
material and is not packaged.

## Disclaimer

This project is an independent third-party tool and is not affiliated with,
endorsed by, sponsored by, or officially associated with JMComic or the
operators of any website accessed by the software.

Users are responsible for ensuring that their use of this software complies
with applicable laws, copyright requirements, website terms of service, and
local regulations.

This software does not grant users any rights to content downloaded through
third-party services.
