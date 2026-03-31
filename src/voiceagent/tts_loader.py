from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
import logging

from PySide6.QtCore import QObject, Signal

from voiceagent.downloaders import DownloadProgress
from voiceagent.services.tts import PiperTtsService


class TtsVoiceLoader(QObject):
    selection_changed = Signal(str)
    ready_changed = Signal(bool)
    loading_changed = Signal(bool)
    status_changed = Signal(str)
    progress_changed = Signal(object)
    error_changed = Signal(str)
    load_completed = Signal()
    load_failed = Signal(str)

    def __init__(self, tts_service: PiperTtsService, parent: QObject | None = None) -> None:
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
        return self.tts_service.model_path

    def select_model(self, model_name: str | None) -> None:
        if self._loading:
            return

        self.tts_service.set_model_path(model_name)
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
        self.status_changed.emit("Preparing Piper voice download")
        self.progress_changed.emit(self._last_progress)

        future = self.executor.submit(self._load_voice)
        future.add_done_callback(self._handle_done)

    def shutdown(self) -> None:
        self.executor.shutdown(wait=False, cancel_futures=True)

    def _emit_initial_state(self) -> None:
        self.ready_changed.emit(self.is_ready)
        self.loading_changed.emit(self._loading)
        if not self.selected_model:
            self.status_changed.emit("Select a Piper voice")
        elif self.is_ready:
            self.status_changed.emit("Piper voice ready")
        else:
            self.status_changed.emit("Load Piper voice to enable speech")
        self.progress_changed.emit(self._last_progress)

    def _load_voice(self) -> None:
        try:
            self.status_changed.emit("Downloading Piper voice with aria2")
            self.tts_service.download_voice(progress_callback=self._emit_progress)
        except Exception as exc:
            self._logger.exception("Piper voice load failed")
            self.load_failed.emit(str(exc))
            return

        self.load_completed.emit()

    def _handle_done(self, future: Future[None]) -> None:
        future.result()

    def _finish_success(self) -> None:
        self._loading = False
        self.loading_changed.emit(False)
        self.ready_changed.emit(True)
        self.status_changed.emit("Piper voice ready")
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
        self.status_changed.emit("Piper voice load failed")
        self.error_changed.emit(message)
        self.progress_changed.emit(DownloadProgress(completed_bytes=0, total_bytes=0, download_speed_bytes_per_second=0))

    def _emit_progress(self, progress: DownloadProgress) -> None:
        self._last_progress = progress
        self.progress_changed.emit(progress)
