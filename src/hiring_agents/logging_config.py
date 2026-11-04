"""Logging setup for the hiring_agents pipeline.

Two independent outputs, each controlled by an environment variable (set it in
environment_vars.env or in the shell):

    LOG_FILE_LEVEL      File in logs/run-<timestamp>.log.    Default: OFF
    LOG_CONSOLE_LEVEL   Terminal (stderr).                    Default: WARNING

Values: OFF, ERROR, WARNING, INFO, DEBUG. Each level includes everything above it.
    WARNING  problems only: failed rankers, retries, validation fixes
    INFO     the workflow: active/sleeping criteria, each ranker's start/finish and timing,
             final ranking summary
    DEBUG    full detail: each agent's instructions, the exact JSON sent to each ranker,
             raw and validated ranker replies, and the aggregation math

Privacy: DEBUG logs contain the payloads sent to the LLMs. These are already pseudonymized
(no names or UCIDs), but they still hold applicant details. The raw candidate records and the
ref -> real-ID map are never logged. logs/ is gitignored, so don't share these files.

Only loggers under "hiring_agents" are captured. Third-party libraries (openai, httpx) stay quiet.
Call `setup_logging()` once, at program start-up. Library modules just use
`logging.getLogger(__name__)`.
"""

import logging
import os
import sys
from datetime import datetime
from pathlib import Path

PACKAGE_LOGGER = "hiring_agents"
_VALID = {"OFF", "ERROR", "WARNING", "INFO", "DEBUG"}


def _read_level(env_var: str, default: str) -> int | None:
    """Parse a level from an env var. Returns None for OFF; raises on an invalid value."""
    value = os.getenv(env_var, default).strip().upper()
    if value not in _VALID:
        raise ValueError(f"{env_var}={value!r} is invalid; use one of {sorted(_VALID)}")
    return None if value == "OFF" else getattr(logging, value)


def setup_logging(log_dir: Path) -> Path | None:
    """Configure the console and file handlers from LOG_CONSOLE_LEVEL and LOG_FILE_LEVEL.

    Safe to call more than once: existing handlers are replaced, not duplicated.

    Args:
        log_dir: Folder for log files (created if missing). Usually <repo root>/logs.

    Returns:
        Path of the new log file, or None if file logging is OFF.
    """
    logger = logging.getLogger(PACKAGE_LOGGER)
    for h in list(logger.handlers):
        logger.removeHandler(h)
        h.close()
    logger.propagate = False  # don't also send our logs to the root logger

    console_level = _read_level("LOG_CONSOLE_LEVEL", "WARNING")
    file_level = _read_level("LOG_FILE_LEVEL", "OFF")
    active_levels = []

    if console_level is not None:
        console = logging.StreamHandler(sys.stderr)
        console.setLevel(console_level)
        console.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
        logger.addHandler(console)
        active_levels.append(console_level)

    log_path = None
    if file_level is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / f"run-{datetime.now():%Y%m%d-%H%M%S}.log"
        file_handler = logging.FileHandler(log_path, encoding="utf-8")
        file_handler.setLevel(file_level)
        file_handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
        )
        logger.addHandler(file_handler)
        active_levels.append(file_level)

    # The logger passes on the most detailed level any handler wants; each handler filters itself.
    logger.setLevel(min(active_levels) if active_levels else logging.CRITICAL + 1)
    return log_path
