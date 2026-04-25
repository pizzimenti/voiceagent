from __future__ import annotations

from logging.handlers import RotatingFileHandler
import logging
from pathlib import Path

from PySide6.QtCore import QtMsgType, qInstallMessageHandler

from voiceagent.paths import default_log_dir


_QT_LEVEL_MAP = {
    QtMsgType.QtDebugMsg: logging.INFO,  # QML console.log arrives as QtDebugMsg
    QtMsgType.QtInfoMsg: logging.INFO,
    QtMsgType.QtWarningMsg: logging.WARNING,
    QtMsgType.QtCriticalMsg: logging.ERROR,
    QtMsgType.QtFatalMsg: logging.CRITICAL,
}


def _qt_message_handler(mode, _context, message) -> None:  # noqa: ANN001 - Qt signature
    logging.getLogger("voiceagent.qml").log(_QT_LEVEL_MAP.get(mode, logging.INFO), message)


def configure_logging() -> Path:
    log_dir = default_log_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "voiceagent.log"

    root_logger = logging.getLogger()
    if root_logger.handlers:
        return log_path

    root_logger.setLevel(logging.INFO)

    qInstallMessageHandler(_qt_message_handler)

    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = RotatingFileHandler(log_path, maxBytes=1_000_000, backupCount=3)
    file_handler.setFormatter(formatter)
    root_logger.addHandler(file_handler)

    stream_handler = logging.StreamHandler()
    stream_handler.setLevel(logging.WARNING)
    stream_handler.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
    root_logger.addHandler(stream_handler)

    logging.getLogger(__name__).info("Logging initialized at %s", log_path)
    console_logger = logging.getLogger("voiceagent.console")
    console_logger.setLevel(logging.INFO)
    console_logger.propagate = False
    if not console_logger.handlers:
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(logging.Formatter("%(message)s"))
        console_logger.addHandler(console_handler)
    return log_path
