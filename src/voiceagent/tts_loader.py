from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
import logging

from PySide6.QtCore import QObject, Signal

from voiceagent.backends import TextToSpeechBackend
from voiceagent.downloaders import DownloadProgress


class TtsVoiceLoader(QObject):
    selection_changed = Signal(str)
    ready_changed = Signal(bool)
    loading_changed = Signal(bool)
    status_changed = Signal(str)
    progress_changed = Signal(object)
    error_changed = Signal(str)
    load_completed = Signal()
    load_failed = Signal(str)

    def __init__(self, tts_service: TextToSpeechBackend, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.tts_service = tts_service
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="voiceagent-tts-loader")
        self._loading = False
        self._logger = logging.getLogger(__name__)
        self._last_progress = DownloadProgress(completed_bytes=0, total_bytes=0, download_speed_bytes_per_second=0)

        self.load_completed.connect(self._finish_success)
        self.load_failed.connect(self._finish_failure)
        self._emit_initial_state()

    @property
    def is_enabled(self) -> bool:
        return self.tts_service.enabled

    @property
    def is_ready(self) -> bool:
        return self.tts_service.is_available

    @property
    def is_loading(self) -> bool:
        return self._loading

    @property
    def selected_model(self) -> str | None:
        return self.tts_service.selected_item

    def select_model(self, model_name: str | None) -> None:
        self.tts_service.set_selected_item(model_name)
        self._last_progress = DownloadProgress(completed_bytes=0, total_bytes=0, download_speed_bytes_per_second=0)
        self.selection_changed.emit(model_name or "")
        self._emit_initial_state()

    def load_voice(self) -> None:
        if not self.is_enabled or self._loading or self.is_ready:
            return

        self._loading = True
        self._last_progress = DownloadProgress(completed_bytes=0, total_bytes=0, download_speed_bytes_per_second=0)
        self.loading_changed.emit(True)
        self.error_changed.emit("")
        self.status_changed.emit(
            f"Preparing {self.tts_service.backend_name} {self.tts_service.selection_label.lower()} download"
        )
        self.progress_changed.emit(self._last_progress)

        future = self.executor.submit(self._load_voice)
        future.add_done_callback(self._handle_done)

    def select_and_load(self, model_name: str) -> None:
        """Select a model and immediately start downloading it."""
        self.tts_service.set_selected_item(model_name)
        self._last_progress = DownloadProgress(completed_bytes=0, total_bytes=0, download_speed_bytes_per_second=0)
        self.selection_changed.emit(model_name)
        self._emit_initial_state()
        self.load_voice()

    def shutdown(self) -> None:
        self.executor.shutdown(wait=False, cancel_futures=True)

    def _emit_initial_state(self) -> None:
        self.ready_changed.emit(self.is_ready)
        self.loading_changed.emit(self._loading)
        if not self.selected_model:
            self.status_changed.emit(
                f"Select a {self.tts_service.backend_name} {self.tts_service.selection_label.lower()}"
            )
        elif self.is_ready:
            self.status_changed.emit(f"{self.tts_service.backend_name} {self.tts_service.selection_label.lower()} ready")
        else:
            self.status_changed.emit(
                f"Load {self.tts_service.backend_name} {self.tts_service.selection_label.lower()} to enable speech"
            )
        self.progress_changed.emit(self._last_progress)

    def _load_voice(self) -> None:
        try:
            self.status_changed.emit(
                f"Downloading {self.tts_service.backend_name} {self.tts_service.selection_label.lower()} with aria2"
            )
            self.tts_service.download_selected_item(progress_callback=self._emit_progress)
        except Exception as exc:
            self._logger.exception("Piper voice load failed")
            self.load_failed.emit(str(exc))
            return

        self.load_completed.emit()

    def _handle_done(self, future: Future[None]) -> None:
        try:
            future.result()
        except Exception:
            self._logger.exception("TTS load future raised unexpectedly")
            if self._loading:
                self.load_failed.emit("Voice download failed unexpectedly")

    def _finish_success(self) -> None:
        self._loading = False
        self.loading_changed.emit(False)
        self.ready_changed.emit(True)
        self.status_changed.emit(f"{self.tts_service.backend_name} {self.tts_service.selection_label.lower()} ready")
        self.progress_changed.emit(
            DownloadProgress(
                completed_bytes=self._last_progress.total_bytes or 1,
                total_bytes=self._last_progress.total_bytes or 1,
                download_speed_bytes_per_second=0,
            )
        )

    def _finish_failure(self, message: str) -> None:
        self._loading = False
        self.loading_changed.emit(False)
        self.ready_changed.emit(False)
        self.status_changed.emit(f"{self.tts_service.backend_name} load failed")
        self.error_changed.emit(message)
        self.progress_changed.emit(DownloadProgress(completed_bytes=0, total_bytes=0, download_speed_bytes_per_second=0))

    def _emit_progress(self, progress: DownloadProgress) -> None:
        self._last_progress = progress
        self.progress_changed.emit(progress)
