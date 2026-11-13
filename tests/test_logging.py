"""Offline tests for the LOG_FILE_LEVEL / LOG_CONSOLE_LEVEL switches. Run: python -m unittest"""

import logging
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from hiring_agents.logging_config import PACKAGE_LOGGER, setup_logging


class LoggingConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.log_dir = Path(self.tmp.name) / "logs"

    def tearDown(self):
        for h in list(logging.getLogger(PACKAGE_LOGGER).handlers):  # release file handles
            logging.getLogger(PACKAGE_LOGGER).removeHandler(h)
            h.close()
        self.tmp.cleanup()

    def test_file_off_by_default(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(setup_logging(self.log_dir))
        self.assertFalse(self.log_dir.exists())

    def test_file_level_filters_messages(self):
        with mock.patch.dict(os.environ, {"LOG_FILE_LEVEL": "info", "LOG_CONSOLE_LEVEL": "OFF"}):
            path = setup_logging(self.log_dir)
        log = logging.getLogger(PACKAGE_LOGGER + ".test")
        log.debug("hidden detail")
        log.info("workflow step")
        text = path.read_text(encoding="utf-8")
        self.assertIn("workflow step", text)
        self.assertNotIn("hidden detail", text)

    def test_invalid_level_raises(self):
        with mock.patch.dict(os.environ, {"LOG_FILE_LEVEL": "VERBOSE"}):
            with self.assertRaises(ValueError):
                setup_logging(self.log_dir)


if __name__ == "__main__":
    unittest.main()
