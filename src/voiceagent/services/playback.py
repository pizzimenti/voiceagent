from __future__ import annotations

import logging
from pathlib import Path
import threading
import wave

from PySide6.QtCore import QObject, Signal
import sounddevice as sd


class AudioPlayer(QObject):
    playback_started = Signal(str)
    playback_finished = Signal(str)
    playback_failed = Signal(str, str)
    playback_state_changed = Signal(str, str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._current_file: Path | None = None
        self._logger = logging.getLogger(__name__)
        self._stream: sd.RawOutputStream | None = None
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._pause_condition = threading.Condition()
        self._paused = False

    def play_file(self, path: Path) -> None:
        self._logger.info("Starting audio playback path=%s exists=%s bytes=%s", path, path.exists(), path.stat().st_size if path.exists() else 0)
        self.stop()
        self._current_file = path
        self._stop_event.clear()
        self._paused = False
        self._thread = threading.Thread(target=self._playback_worker, args=(path,), daemon=True)
        self._thread.start()

    def pause(self) -> None:
        if self._current_file is None or self._paused:
            return
        self._logger.info("Pausing audio playback path=%s", self._current_file)
        with self._pause_condition:
            self._paused = True
        self.playback_state_changed.emit(str(self._current_file), "PausedState")

    def resume(self) -> None:
        if self._current_file is None or not self._paused:
            return
        self._logger.info("Resuming audio playback path=%s", self._current_file)
        with self._pause_condition:
            self._paused = False
            self._pause_condition.notify_all()
        self.playback_state_changed.emit(str(self._current_file), "PlayingState")

    def stop(self) -> None:
        if self._current_file is None and self._thread is None:
            return

        self._logger.info("Stopping audio playback path=%s", self._current_file)
        self._stop_event.set()
        with self._pause_condition:
            self._paused = False
            self._pause_condition.notify_all()

        if self._stream is not None:
            try:
                self._stream.abort()
            except Exception:
                pass

        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=1.0)
        self._thread = None
        self._cleanup_current_file()

    @property
    def is_playing(self) -> bool:
        return self._current_file is not None and self._thread is not None and self._thread.is_alive() and not self._paused

    @property
    def is_paused(self) -> bool:
        return self._current_file is not None and self._paused

    def _playback_worker(self, path: Path) -> None:
        try:
            with wave.open(str(path), "rb") as wav_file:
                channels = wav_file.getnchannels()
                sample_rate = wav_file.getframerate()
                sample_width = wav_file.getsampwidth()
                dtype = self._dtype_for_sample_width(sample_width)
                self._logger.info(
                    "Audio playback stream open path=%s channels=%s sample_rate=%s sample_width=%s",
                    path,
                    channels,
                    sample_rate,
                    sample_width,
                )
                with sd.RawOutputStream(
                    samplerate=sample_rate,
                    channels=channels,
                    dtype=dtype,
                ) as stream:
                    self._stream = stream
                    self.playback_state_changed.emit(str(path), "PlayingState")
                    self.playback_started.emit(str(path))
                    while not self._stop_event.is_set():
                        with self._pause_condition:
                            while self._paused and not self._stop_event.is_set():
                                self._pause_condition.wait(timeout=0.1)

                        if self._stop_event.is_set():
                            break

                        data = wav_file.readframes(4096)
                        if not data:
                            break
                        stream.write(data)
        except Exception as exc:
            self._logger.exception("Audio playback failed path=%s", path)
            self.playback_failed.emit(str(path), str(exc) or "Audio playback failed.")
        else:
            if not self._stop_event.is_set():
                self._logger.info("Audio playback finished path=%s", path)
                self.playback_finished.emit(str(path))
        finally:
            self._stream = None
            if self._current_file == path:
                self._cleanup_current_file()
            self._stop_event.clear()
            self._paused = False

    def _cleanup_current_file(self) -> None:
        if self._current_file is None:
            return

        self._logger.info("Cleaning up playback file path=%s", self._current_file)
        self._current_file.unlink(missing_ok=True)
        self._current_file = None

    def _dtype_for_sample_width(self, sample_width: int) -> str:
        if sample_width == 1:
            return "uint8"
        if sample_width == 2:
            return "int16"
        if sample_width == 3:
            return "int24"
        if sample_width == 4:
            return "int32"
        raise RuntimeError(f"Unsupported WAV sample width: {sample_width}")
