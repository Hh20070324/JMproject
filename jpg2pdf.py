"""Compatibility imports and command-line entry point for PDF generation."""

import sys

from jm_downloader.pdf import album_to_pdf, jpg_to_pdf, natural_key
from jm_downloader.settings import DEFAULT_PATHS


__all__ = ["album_to_pdf", "jpg_to_pdf", "natural_key"]


if __name__ == "__main__":
    if len(sys.argv) >= 2:
        jpg_to_pdf(sys.argv[1])
    else:
        jpg_to_pdf(
            str(DEFAULT_PATHS.root / "打包专用"),
            str(DEFAULT_PATHS.root),
        )
