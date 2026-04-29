from __future__ import annotations

from logging.handlers import RotatingFileHandler
import logging
import os
from pathlib import Path
import time

from PySide6.QtCore import QtMsgType, qInstallMessageHandler

from voiceagent.paths import default_log_dir


CONVERSATION_LOGGER_NAME = "voiceagent.conversation"
CONVERSATION_LOG_FILENAME = "conversation.log"
CONVERSATION_BACKUP_COUNT = 5


_QT_LEVEL_MAP = {
    QtMsgType.QtDebugMsg: logging.INFO,  # QML console.log arrives as QtDebugMsg
    QtMsgType.QtInfoMsg: logging.INFO,
    QtMsgType.QtWarningMsg: logging.WARNING,
    QtMsgType.QtCriticalMsg: logging.ERROR,
    QtMsgType.QtFatalMsg: logging.CRITICAL,
}


def _qt_message_handler(mode, _context, message) -> None:  # noqa: ANN001 - Qt signature
    logging.getLogger("voiceagent.qml").log(_QT_LEVEL_MAP.get(mode, logging.INFO), message)


def rotate_conversation_log(
    log_path: Path, backup_count: int = CONVERSATION_BACKUP_COUNT
) -> None:
    """Shift `log_path` -> `log_path.1`, `.1` -> `.2`, ..., dropping the
    oldest beyond `backup_count`.

    Called once at startup BEFORE the conversation logger's handler
    opens the file, so each launch starts with a fresh
    `conversation.log` and the prior session is preserved as `.1`.
    No-op when the current log doesn't exist (fresh install or fresh
    log directory). Errors during rename / unlink are logged as
    warnings but do not propagate — failing to rotate must not block
    the app from starting.
    """
    if not log_path.exists():
        return
    shim_logger = logging.getLogger(__name__)
    backup_count = max(1, int(backup_count))
    # Drop the would-be-overflow backup so the rename chain has room.
    overflow = Path(f"{log_path}.{backup_count}")
    if overflow.exists():
        try:
            overflow.unlink()
        except OSError as exc:
            shim_logger.warning(
                "rotate_conversation_log: could not unlink %s: %s",
                overflow,
                exc,
            )
            return
    # Shift backups one slot older: .{N-1} -> .N for N down to 1.
    for n in range(backup_count - 1, 0, -1):
        src = Path(f"{log_path}.{n}")
        if not src.exists():
            continue
        dst = Path(f"{log_path}.{n + 1}")
        try:
            src.rename(dst)
        except OSError as exc:
            shim_logger.warning(
                "rotate_conversation_log: could not rename %s -> %s: %s",
                src,
                dst,
                exc,
            )
            return
    # Move current -> .1.
    try:
        log_path.rename(Path(f"{log_path}.1"))
    except OSError as exc:
        shim_logger.warning(
            "rotate_conversation_log: could not rename %s -> %s.1: %s",
            log_path,
            log_path,
            exc,
        )


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
    if not root_logger.handlers:
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

    # Conversation logger setup is independent of the root-logger gate.
    # Some embedding contexts (pytest fixtures, host apps that bring
    # their own logging) configure root logging before calling
    # `configure_logging()`; the early-return above used to silently
    # drop the conversation logger in those cases. Now this runs
    # ALWAYS, gated only on its own idempotency check inside
    # `_install_conversation_logger`.
    _install_conversation_logger(log_dir)
    return log_path


def _install_conversation_logger(log_dir: Path) -> None:
    """Set up the dedicated conversation logger.

    Captures the actual content shipped to the LLM (full `messages`
    list per turn, assistant response, token usage), plus per-turn
    lifecycle events (model swap, trim). Rotates by SESSION rather
    than by size: each launch shifts the prior `conversation.log` to
    `.1`, drops the oldest beyond `CONVERSATION_BACKUP_COUNT`.
    Default-on so a debug pass can always reach back to the previous
    N sessions; size growth is bounded by the rotation count, not the
    per-file size.

    Idempotent: if the conversation logger already has handlers (a
    prior `configure_logging()` call, or test setup), this is a
    no-op. Decoupled from root-logger state so a host process that
    pre-configured root logging still gets its conversation logger.
    """
    conversation_log_path = log_dir / CONVERSATION_LOG_FILENAME
    conversation_logger = logging.getLogger(CONVERSATION_LOGGER_NAME)
    if conversation_logger.handlers:
        return
    rotate_conversation_log(conversation_log_path)
    conversation_logger.setLevel(logging.INFO)
    conversation_logger.propagate = False
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    conversation_handler = logging.FileHandler(
        conversation_log_path, encoding="utf-8"
    )
    conversation_handler.setLevel(logging.INFO)
    conversation_handler.setFormatter(formatter)
    conversation_logger.addHandler(conversation_handler)
    conversation_logger.info(
        "Conversation log opened at %s", conversation_log_path
    )


def log_ui_timing(logger: logging.Logger, label: str, started_monotonic: float) -> None:
    """Emit a DEBUG entry of the form 'ui-timing label=... ms=...'.

    Cheap helper for instrumenting UI/event-handler latency. Use
    `time.monotonic()` to capture the start time, then call this on
    exit. No-op (still cheap) when DEBUG is disabled.
    """
    elapsed_ms = (time.monotonic() - started_monotonic) * 1000.0
    logger.debug("ui-timing label=%s ms=%.1f", label, elapsed_ms)
