"""Logging configuration shared by every module of the pipeline."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

ROOT_LOGGER_NAME = "pipeline"
_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"


def configure_logging(level: str = "INFO", log_dir: Path | None = None) -> logging.Logger:
    """Configure the ``pipeline`` logger (console and optional log file).

    The function is idempotent: calling it again replaces the existing handlers
    instead of stacking them.

    Args:
        level: Logging level name, e.g. ``"INFO"``.
        log_dir: Directory for ``pipeline.log``. No file is written if ``None``.
    """
    logger = logging.getLogger(ROOT_LOGGER_NAME)
    logger.setLevel(level.upper())
    logger.propagate = False

    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()

    formatter = logging.Formatter(_FORMAT)
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    logger.addHandler(console)

    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_dir / "pipeline.log", encoding="utf-8")
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

    return logger


def get_logger(name: str) -> logging.Logger:
    """Return a child logger such as ``pipeline.transform``."""
    return logging.getLogger(f"{ROOT_LOGGER_NAME}.{name}")
