from __future__ import annotations

from logging.handlers import RotatingFileHandler
import logging
from pathlib import Path

from voiceagent.paths import default_log_dir


def configure_logging() -> Path:
    log_dir = default_log_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "voiceagent.log"

    root_logger = logging.getLogger()
    if root_logger.handlers:
        return log_path

    root_logger.setLevel(logging.INFO)

    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = RotatingFileHandler(log_path, maxBytes=1_000_000, backupCount=3)
    file_handler.setFormatter(formatter)
    root_logger.addHandler(file_handler)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    root_logger.addHandler(stream_handler)

    logging.getLogger(__name__).info("Logging initialized at %s", log_path)
    return log_path
