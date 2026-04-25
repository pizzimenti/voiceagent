from __future__ import annotations

import logging
import tempfile
from pathlib import Path

from PySide6.QtCore import QLockFile, QObject, QStandardPaths, Signal
from PySide6.QtNetwork import QLocalServer, QLocalSocket


logger = logging.getLogger(__name__)

# Wire protocol: a peer must send this exact byte string to trigger activation.
ACTIVATE_PAYLOAD = b"activate"
# Cap the per-connection buffer so a misbehaving peer cannot stream unbounded
# bytes at us. The protocol is a single fixed token, so a small ceiling is fine.
_MAX_PAYLOAD_BYTES = 64


class SingleInstance(QObject):
    activated = Signal()

    def __init__(self, lock_file: QLockFile, server: QLocalServer, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._lock_file = lock_file
        self._server = server
        self._buffers: dict[int, bytearray] = {}
        self._server.newConnection.connect(self._on_new_connection)

    def _on_new_connection(self) -> None:
        connection = self._server.nextPendingConnection()
        if connection is None:
            return
        key = id(connection)
        self._buffers[key] = bytearray()

        def _on_ready_read() -> None:
            buffer = self._buffers.get(key)
            if buffer is None:
                # Connection already finalized; drain and ignore.
                connection.readAll()
                return
            chunk = bytes(connection.readAll())
            if not chunk:
                return
            buffer.extend(chunk)
            if len(buffer) > _MAX_PAYLOAD_BYTES:
                logger.warning(
                    "Activation peer sent oversized payload (%d bytes); dropping",
                    len(buffer),
                )
                self._buffers.pop(key, None)
                connection.disconnectFromServer()
                return
            if bytes(buffer).rstrip(b"\r\n") == ACTIVATE_PAYLOAD:
                self._buffers.pop(key, None)
                self.activated.emit()
                connection.disconnectFromServer()

        def _on_disconnected() -> None:
            self._buffers.pop(key, None)
            connection.deleteLater()

        connection.readyRead.connect(_on_ready_read)
        connection.disconnected.connect(_on_disconnected)

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
            socket.write(ACTIVATE_PAYLOAD)
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
