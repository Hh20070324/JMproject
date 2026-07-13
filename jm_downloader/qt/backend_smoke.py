from pathlib import Path
from tempfile import TemporaryDirectory

import certifi
import jmcomic
from common import CurlCffiPostman
from Crypto.Cipher import AES
from PIL import Image
from curl_cffi import Curl

from ..pdf import album_to_pdf
from ..settings import AppPaths, DEFAULT_PATHS


def run_backend_smoke(paths: AppPaths = DEFAULT_PATHS) -> None:
    option_file = paths.option_file
    if not option_file.is_file():
        raise FileNotFoundError(f"没有找到下载配置：{option_file}")

    option = jmcomic.create_option_by_file(str(option_file))
    if option is None:
        raise RuntimeError("JMComic 未能解析下载配置。")

    auto_update = jmcomic.JmModuleConfig.FLAG_API_CLIENT_AUTO_UPDATE_DOMAIN
    require_cookies = jmcomic.JmModuleConfig.FLAG_API_CLIENT_REQUIRE_COOKIES
    try:
        jmcomic.JmModuleConfig.FLAG_API_CLIENT_AUTO_UPDATE_DOMAIN = False
        jmcomic.JmModuleConfig.FLAG_API_CLIENT_REQUIRE_COOKIES = False
        client = option.new_jm_client(domain_list=["offline.invalid"])
    finally:
        jmcomic.JmModuleConfig.FLAG_API_CLIENT_AUTO_UPDATE_DOMAIN = auto_update
        jmcomic.JmModuleConfig.FLAG_API_CLIENT_REQUIRE_COOKIES = require_cookies
    if not isinstance(client, jmcomic.JmApiClient):
        raise RuntimeError("JMComic 未能创建 API 下载客户端。")
    if not isinstance(client.postman, CurlCffiPostman):
        raise RuntimeError("JMComic 未使用 Curl CFFI 网络后端。")

    curl = Curl()
    curl.close()

    key = bytes(range(16))
    plaintext = bytes(reversed(range(16)))
    encrypted = AES.new(key, AES.MODE_ECB).encrypt(plaintext)
    decrypted = AES.new(key, AES.MODE_ECB).decrypt(encrypted)
    if decrypted != plaintext:
        raise RuntimeError("AES 后端自检失败。")

    certificate = Path(certifi.where())
    if not certificate.is_file():
        raise FileNotFoundError(f"没有找到 CA 证书：{certificate}")

    with TemporaryDirectory(prefix="jm-downloader-backend-smoke-") as temp_dir:
        temp_root = Path(temp_dir)
        image_path = temp_root / "test.png"
        Image.new("RGB", (2, 2), (32, 128, 96)).save(image_path, format="PNG")
        with Image.open(image_path) as image:
            image.load()
            if image.size != (2, 2) or image.getpixel((0, 0)) != (32, 128, 96):
                raise RuntimeError("Pillow 图片读写自检失败。")

        chapter_dir = temp_root / "Pictures" / "1" / "chapter"
        chapter_dir.mkdir(parents=True)
        pdf_source = chapter_dir / "1.png"
        Image.new("RGB", (8, 8), (32, 128, 96)).save(pdf_source, format="PNG")
        pdf_path = album_to_pdf(
            str(chapter_dir.parent),
            str(temp_root / "PDFs"),
        )
        if pdf_path is None or not Path(pdf_path).is_file():
            raise RuntimeError("PDF 生成后端自检失败。")
