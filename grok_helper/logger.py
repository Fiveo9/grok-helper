from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

from grok_helper.paths import log_dir


logger = logging.getLogger("grok_helper")


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def setup_logging(
    *,
    level: str = "INFO",
    file_logging: bool | None = None,
    max_files: int = 7,
) -> None:
    del max_files
    resolved_level = getattr(logging, level.upper(), logging.INFO)
    logger.handlers.clear()
    logger.setLevel(resolved_level)
    logger.propagate = False

    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)s:%(funcName)s:%(lineno)d - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(resolved_level)
    console.setFormatter(formatter)
    logger.addHandler(console)

    if file_logging is None:
        file_logging = _env_bool("LOG_FILE_ENABLED", True)
    if not file_logging:
        return

    directory: Path = log_dir()
    directory.mkdir(parents=True, exist_ok=True)
    file_handler = logging.FileHandler(directory / "register.log", encoding="utf-8")
    file_handler.setLevel(resolved_level)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)


__all__ = ["logger", "setup_logging"]
