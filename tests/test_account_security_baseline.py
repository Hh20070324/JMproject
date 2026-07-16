import logging
import tempfile
import unittest
from pathlib import Path

import jmcomic

from jm_downloader.desktop_runtime import configure_logging
from jm_downloader.jmcomic_logging import install_safe_jmcomic_logging
from jm_downloader.settings import AppPaths


class AccountSecurityBaselineTests(unittest.TestCase):
    def test_jmcomic_log_bridge_never_writes_account_secrets(self):
        secrets = (
            "account-sentinel",
            "password-sentinel",
            "cookie-sentinel",
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            paths = AppPaths(Path(temp_dir))
            logger = configure_logging(paths, level=logging.DEBUG)
            try:
                install_safe_jmcomic_logging()
                joined = " ".join(secrets)
                jmcomic.JmModuleConfig.jm_log(
                    "req.error",
                    joined,
                    RuntimeError(joined),
                )
                jmcomic.JmModuleConfig.jm_log("account.login", joined)
                for handler in logger.handlers:
                    handler.flush()
                output = (paths.logs / "app.log").read_text(encoding="utf-8")
            finally:
                for handler in tuple(logger.handlers):
                    logger.removeHandler(handler)
                    handler.close()

        self.assertIn("JM request attempt failed (RuntimeError)", output)
        for secret in secrets:
            self.assertNotIn(secret, output)


if __name__ == "__main__":
    unittest.main()
