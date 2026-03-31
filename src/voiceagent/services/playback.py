from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QObject, QUrl, Signal
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer


class AudioPlayer(QObject):
    playback_finished = Signal()
    playback_failed = Signal(str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._audio_output: QAudioOutput | None = None
        self._player: QMediaPlayer | None = None
        self._current_file: Path | None = None

    def play_file(self, path: Path) -> None:
        self._ensure_backend()
        self._cleanup_current_file()
        self._current_file = path
        assert self._player is not None
        self._player.setSource(QUrl.fromLocalFile(str(path)))
        self._player.play()

    def _handle_media_status_changed(self, status: QMediaPlayer.MediaStatus) -> None:
        if status == QMediaPlayer.MediaStatus.EndOfMedia:
            self._cleanup_current_file()
            self.playback_finished.emit()

    def _handle_error(self, error: QMediaPlayer.Error, message: str) -> None:
        if error == QMediaPlayer.Error.NoError:
            return

        self._cleanup_current_file()
        self.playback_failed.emit(message or "Audio playback failed.")

    def _cleanup_current_file(self) -> None:
        if self._current_file is None:
            return

        self._current_file.unlink(missing_ok=True)
        self._current_file = None

    def _ensure_backend(self) -> None:
        if self._player is not None:
            return

        self._audio_output = QAudioOutput(self)
        self._player = QMediaPlayer(self)
        self._player.setAudioOutput(self._audio_output)
        self._player.mediaStatusChanged.connect(self._handle_media_status_changed)
        self._player.errorOccurred.connect(self._handle_error)
