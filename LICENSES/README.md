# Bundled License Texts

This directory is the license inventory for the JM-Downloader v2.4.0 Windows
x64 portable distribution. Versions match the packages used by the release
build.

| File | Component | License |
| --- | --- | --- |
| `GPL-3.0-only.txt` | Qt GPL reference text | GPL-3.0-only |
| `LGPL-3.0-only.txt` | Qt 6.11.1, PySide6 Essentials 6.11.1, Shiboken6 6.11.1 | LGPL-3.0-only |
| `Game-Icon-Pack-CC0-1.0.txt` | Selected Game Icon Pack SVG assets | CC0-1.0 |
| `Python-3.14.txt` | CPython 3.14 runtime | Python-2.0 and incorporated notices |
| `JMComic-Crawler-Python-2.7.1.txt` | JMComic-Crawler-Python 2.7.1 | MIT |
| `commonX-0.6.40.txt` | commonX 0.6.40 | MIT |
| `curl_cffi-0.15.0.txt` | curl_cffi 0.15.0 Python code | MIT |
| `curl_cffi-0.15.0-native.txt` | curl_cffi native wrapper and HTTPX-derived code | Upstream notices listed in the file |
| `certifi-2026.6.17.txt` | certifi 2026.6.17 CA bundle | MPL-2.0 |
| `cffi-2.0.0.txt` | cffi 2.0.0 | MIT metadata; MIT No Attribution text |
| `Pillow-12.2.0.txt` | Pillow 12.2.0 and its bundled image libraries | MIT-CMU and incorporated notices |
| `pycparser-3.0.txt` | pycparser 3.0 | BSD-3-Clause |
| `PyCryptodome-3.23.0.txt` | PyCryptodome 3.23.0 | BSD and public domain |
| `PyInstaller-6.21.0.txt` | PyInstaller bootloader and runtime 6.21.0 | GPL-2.0-or-later with Bootloader Exception; other terms listed in the file |
| `PyYAML-6.0.3.txt` | PyYAML 6.0.3 | MIT |
| `typing_extensions-4.16.0.txt` | typing_extensions 4.16.0 | PSF-2.0 |

The Pillow license file is copied in full from the installed wheel. It
includes the notices for native image and font libraries compiled into that
wheel.

The curl_cffi native notice records the exact version string reported by the
Windows wheel and reproduces upstream license texts for libcurl,
curl-impersonate, BoringSSL, zlib, Brotli, Zstandard, nghttp2, ngtcp2,
nghttp3, and the HTTPX code adapted by curl_cffi.

See `../THIRD_PARTY_NOTICES.md`, `../QT_SOURCE_AND_RELINKING.md`, and
`../QT_THIRD_PARTY_NOTICES.txt` for source links, component details,
relinking information, and additional Qt attributions.
