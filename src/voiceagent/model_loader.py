from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
import logging

from PySide6.QtCore import QObject, Signal

from voiceagent.backends import SpeechToTextBackend
from voiceagent.downloaders import DownloadProgress


class WhisperModelLoader(QObject):
    selection_changed = Signal(str)
    ready_changed = Signal(bool)
    loading_changed = Signal(bool)
    status_changed = Signal(str)
    progress_changed = Signal(object)
    error_changed = Signal(str)
    load_completed = Signal()
    load_failed = Signal(str)
    delete_completed = Signal()
    delete_failed = Signal(str)

    def __init__(self, transcriber: SpeechToTextBackend, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.transcriber = transcriber
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="voiceagent-model-loader")
        self._loading = False
        self._logger = logging.getLogger(__name__)
        self._last_progress = DownloadProgress(completed_bytes=0, total_bytes=0, download_speed_bytes_per_second=0)

        self.load_completed.connect(self._finish_success)
        self.load_failed.connect(self._finish_failure)
        self.delete_completed.connect(self._finish_delete_success)
        self.delete_failed.connect(self._finish_delete_failure)
        self._emit_initial_state()

    @property
    def is_ready(self) -> bool:
        return self.transcriber.is_available

    @property
    def is_loading(self) -> bool:
        return self._loading

    @property
    def selected_model(self) -> str:
        return self.transcriber.selected_item

    def select_model(self, model_name: str) -> None:
        if self._loading:
            return

        self.transcriber.set_selected_item(model_name)
        self._last_progress = DownloadProgress(completed_bytes=0, total_bytes=0, download_speed_bytes_per_second=0)
        self.selection_changed.emit(model_name)
        self._emit_initial_state()

    def load_model(self) -> None:
        if self._loading or not self.selected_model or self.transcriber.is_item_available(self.selected_model):
            return

        self.download_model(self.selected_model)

    def download_model(self, model_name: str) -> None:
        if self._loading or not model_name or self.transcriber.is_item_available(model_name):
            return

        self._loading = True
        self._last_progress = DownloadProgress(completed_bytes=0, total_bytes=0, download_speed_bytes_per_second=0)
        self.loading_changed.emit(True)
        self.error_changed.emit("")
        self.status_changed.emit(f"Checking {self.transcriber.backend_name} {self.transcriber.selection_label.lower()} files")
        self.progress_changed.emit(self._last_progress)

        future = self.executor.submit(self._download_model, model_name)
        future.add_done_callback(self._handle_done)

    def delete_model(self, model_name: str) -> None:
        if self._loading or not model_name or not self.transcriber.is_item_available(model_name):
            return

        self._loading = True
        self._last_progress = DownloadProgress(completed_bytes=0, total_bytes=0, download_speed_bytes_per_second=0)
        self.loading_changed.emit(True)
        self.error_changed.emit("")
        self.status_changed.emit(f"Removing {self.transcriber.backend_name} {self.transcriber.selection_label.lower()}")
        self.progress_changed.emit(self._last_progress)

        future = self.executor.submit(self._delete_model, model_name)
        future.add_done_callback(self._handle_done)

    def shutdown(self) -> None:
        self.executor.shutdown(wait=False, cancel_futures=True)

    def _emit_initial_state(self) -> None:
        self.ready_changed.emit(self.is_ready)
        self.loading_changed.emit(self._loading)
        if self.is_ready:
            self.status_changed.emit(f"{self.transcriber.backend_name} {self.transcriber.selection_label.lower()} ready")
        else:
            self.status_changed.emit(
                f"Download {self.transcriber.backend_name} {self.transcriber.selection_label.lower()} to enable audio"
            )
        self.progress_changed.emit(self._last_progress)

    def _download_model(self, model_name: str) -> None:
        try:
            self.status_changed.emit(
                f"Downloading {self.transcriber.backend_name} {self.transcriber.selection_label.lower()} with aria2"
            )
            self.transcriber.download_item(model_name, progress_callback=self._emit_progress)
        except Exception as exc:
            self._logger.exception("Whisper model load failed")
            self.load_failed.emit(str(exc))
            return

        self.load_completed.emit()

    def _delete_model(self, model_name: str) -> None:
        try:
            self.transcriber.remove_item(model_name)
        except Exception as exc:
            self._logger.exception("Whisper model delete failed")
            self.delete_failed.emit(str(exc))
            return

        self.delete_completed.emit()

    def _handle_done(self, future: Future[None]) -> None:
        future.result()

    def _finish_success(self) -> None:
        self._loading = False
        self.loading_changed.emit(False)
        self.ready_changed.emit(self.is_ready)
        self.status_changed.emit(f"{self.transcriber.backend_name} {self.transcriber.selection_label.lower()} ready")
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
        self.ready_changed.emit(self.is_ready)
        self.status_changed.emit(f"{self.transcriber.backend_name} load failed")
        self.error_changed.emit(message)
        self.progress_changed.emit(DownloadProgress(completed_bytes=0, total_bytes=0, download_speed_bytes_per_second=0))

    def _finish_delete_success(self) -> None:
        self._loading = False
        self.loading_changed.emit(False)
        self.ready_changed.emit(self.is_ready)
        self.status_changed.emit(f"{self.transcriber.backend_name} model removed")
        self.progress_changed.emit(DownloadProgress(completed_bytes=0, total_bytes=0, download_speed_bytes_per_second=0))

    def _finish_delete_failure(self, message: str) -> None:
        self._loading = False
        self.loading_changed.emit(False)
        self.ready_changed.emit(self.is_ready)
        self.status_changed.emit(f"{self.transcriber.backend_name} remove failed")
        self.error_changed.emit(message)
        self.progress_changed.emit(DownloadProgress(completed_bytes=0, total_bytes=0, download_speed_bytes_per_second=0))

    def _emit_progress(self, progress: DownloadProgress) -> None:
        self._last_progress = progress
        self.progress_changed.emit(progress)
