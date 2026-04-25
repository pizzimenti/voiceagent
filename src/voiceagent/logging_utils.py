from __future__ import annotations

from logging.handlers import RotatingFileHandler
import logging
import os
from pathlib import Path
import time

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


def is_verbose_ui_enabled() -> bool:
    """True when VOICEAGENT_VERBOSE_UI is set to a truthy value.

    When enabled, the file handler logs at DEBUG so timing entries from
    the click chain (window slots, controller.start_recording,
    audio.start) land in the rotating log file. The terminal stream
    handler stays at WARNING regardless so launching the app does not
    spam the console.
    """
    raw = os.environ.get("VOICEAGENT_VERBOSE_UI", "")
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def configure_logging() -> Path:
    log_dir = default_log_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "voiceagent.log"

    root_logger = logging.getLogger()
    if root_logger.handlers:
        return log_path

    verbose_ui = is_verbose_ui_enabled()
    root_logger.setLevel(logging.DEBUG if verbose_ui else logging.INFO)

    qInstallMessageHandler(_qt_message_handler)

    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = RotatingFileHandler(log_path, maxBytes=1_000_000, backupCount=3)
    file_handler.setLevel(logging.DEBUG if verbose_ui else logging.INFO)
    file_handler.setFormatter(formatter)
    root_logger.addHandler(file_handler)

    stream_handler = logging.StreamHandler()
    stream_handler.setLevel(logging.WARNING)
    stream_handler.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
    root_logger.addHandler(stream_handler)

    logging.getLogger(__name__).info("Logging initialized at %s", log_path)
    if verbose_ui:
        logging.getLogger(__name__).info(
            "VOICEAGENT_VERBOSE_UI active; file handler at DEBUG"
        )
    console_logger = logging.getLogger("voiceagent.console")
    console_logger.setLevel(logging.INFO)
    console_logger.propagate = False
    if not console_logger.handlers:
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(logging.Formatter("%(message)s"))
        console_logger.addHandler(console_handler)
    return log_path


def log_ui_timing(logger: logging.Logger, label: str, started_monotonic: float) -> None:
    """Emit a DEBUG entry of the form 'ui-timing label=... ms=...'.

    Cheap helper for instrumenting UI/event-handler latency. Use
    `time.monotonic()` to capture the start time, then call this on
    exit. No-op (still cheap) when DEBUG is disabled.
    """
    elapsed_ms = (time.monotonic() - started_monotonic) * 1000.0
    logger.debug("ui-timing label=%s ms=%.1f", label, elapsed_ms)
