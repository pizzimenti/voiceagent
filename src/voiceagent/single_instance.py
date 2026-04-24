from __future__ import annotations

import logging
import tempfile
from pathlib import Path

from PySide6.QtCore import QLockFile, QObject, QStandardPaths, Signal
from PySide6.QtNetwork import QLocalServer, QLocalSocket


logger = logging.getLogger(__name__)


class SingleInstance(QObject):
    activated = Signal()

    def __init__(self, lock_file: QLockFile, server: QLocalServer, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._lock_file = lock_file
        self._server = server
        self._server.newConnection.connect(self._on_new_connection)

    def _on_new_connection(self) -> None:
        connection = self._server.nextPendingConnection()
        if connection is None:
            return
        connection.readyRead.connect(connection.readAll)
        connection.disconnected.connect(connection.deleteLater)
        self.activated.emit()

    def release(self) -> None:
        if self._server.isListening():
            self._server.close()
        if self._lock_file.isLocked():
            self._lock_file.unlock()


def _lock_path(server_name: str) -> Path:
    runtime_dir = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.RuntimeLocation)
    base = Path(runtime_dir) if runtime_dir else Path(tempfile.gettempdir())
    base.mkdir(parents=True, exist_ok=True)
    return base / f"{server_name}.lock"


def acquire_or_activate(server_name: str = "voiceagent") -> SingleInstance | None:
    lock_path = _lock_path(server_name)
    lock_file = QLockFile(str(lock_path))
    lock_file.setStaleLockTime(0)

    if not lock_file.tryLock(0):
        socket = QLocalSocket()
        socket.connectToServer(server_name)
        if socket.waitForConnected(1000):
            socket.write(b"activate\n")
            socket.flush()
            socket.waitForBytesWritten(1000)
            socket.disconnectFromServer()
            logger.info("Signaled existing voiceagent instance to activate")
        else:
            logger.warning(
                "Lock held but cannot reach existing instance on %s: %s",
                server_name,
                socket.errorString(),
            )
        return None

    QLocalServer.removeServer(server_name)
    server = QLocalServer()
    if not server.listen(server_name):
        logger.error(
            "Unable to start activation server on %s: %s",
            server_name,
            server.errorString(),
        )
        lock_file.unlock()
        return None

    logger.info(
        "Single-instance lock acquired at %s; activation server on %s",
        lock_path,
        server_name,
    )
    return SingleInstance(lock_file=lock_file, server=server)
