from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
import logging

from PySide6.QtCore import QObject, Signal

from voiceagent.backends import SpeechToTextBackend
from voiceagent.downloaders import DownloadProgress


_EMPTY_PROGRESS = DownloadProgress(completed_bytes=0, total_bytes=0, download_speed_bytes_per_second=0)


class WhisperModelLoader(QObject):
    selection_changed = Signal(str)
    ready_changed = Signal(bool)
    loading_changed = Signal(bool)
    status_changed = Signal(str)
    # Aggregate progress across all active downloads.
    progress_changed = Signal(object)
    # Per-model signals for parallel-install UI.
    item_loading_changed = Signal(str, bool)  # (model_name, is_loading)
    item_progress_changed = Signal(str, object)  # (model_name, DownloadProgress)
    error_changed = Signal(str)
    load_completed = Signal(str)  # model_name
    load_failed = Signal(str, str)  # (model_name, error_message)
    delete_completed = Signal(str)  # model_name
    delete_failed = Signal(str, str)  # (model_name, error_message)

    def __init__(self, transcriber: SpeechToTextBackend, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.transcriber = transcriber
        self.executor = ThreadPoolExecutor(max_workers=3, thread_name_prefix="voiceagent-model-loader")
        self._active_items: set[str] = set()
        self._progress_by_item: dict[str, DownloadProgress] = {}
        self._logger = logging.getLogger(__name__)

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
        return bool(self._active_items)

    def is_item_loading(self, model_name: str) -> bool:
        return model_name in self._active_items

    def progress_for(self, model_name: str) -> DownloadProgress:
        return self._progress_by_item.get(model_name, _EMPTY_PROGRESS)

    @property
    def active_items(self) -> frozenset[str]:
        return frozenset(self._active_items)

    @property
    def selected_model(self) -> str:
        return self.transcriber.selected_item

    def select_model(self, model_name: str) -> None:
        self.transcriber.set_selected_item(model_name)
        self.selection_changed.emit(model_name)
        self._emit_initial_state()

    def load_model(self) -> None:
        if (
            not self.selected_model
            or self.selected_model in self._active_items
            or self.transcriber.is_item_available(self.selected_model)
        ):
            return
        self.download_model(self.selected_model)

    def download_model(self, model_name: str) -> None:
        if (
            not model_name
            or model_name in self._active_items
            or self.transcriber.is_item_available(model_name)
        ):
            return

        was_idle = not self._active_items
        self._active_items.add(model_name)
        self._progress_by_item[model_name] = _EMPTY_PROGRESS
        self.item_loading_changed.emit(model_name, True)
        self.item_progress_changed.emit(model_name, _EMPTY_PROGRESS)
        if was_idle:
            self.loading_changed.emit(True)
        self.error_changed.emit("")
        self.status_changed.emit(f"Checking {self.transcriber.backend_name} {self.transcriber.selection_label.lower()} files")
        self.progress_changed.emit(self._aggregate_progress())

        future = self.executor.submit(self._download_model, model_name)
        future.add_done_callback(lambda f, n=model_name: self._handle_done(f, n))

    def delete_model(self, model_name: str) -> None:
        if (
            not model_name
            or model_name in self._active_items
            or not self.transcriber.is_item_available(model_name)
        ):
            return

        was_idle = not self._active_items
        self._active_items.add(model_name)
        self._progress_by_item[model_name] = _EMPTY_PROGRESS
        self.item_loading_changed.emit(model_name, True)
        if was_idle:
            self.loading_changed.emit(True)
        self.error_changed.emit("")
        self.status_changed.emit(f"Removing {self.transcriber.backend_name} {self.transcriber.selection_label.lower()}")
        self.progress_changed.emit(self._aggregate_progress())

        future = self.executor.submit(self._delete_model, model_name)
        future.add_done_callback(lambda f, n=model_name: self._handle_done(f, n))

    def shutdown(self) -> None:
        self.executor.shutdown(wait=False, cancel_futures=True)

    def _emit_initial_state(self) -> None:
        self.ready_changed.emit(self.is_ready)
        self.loading_changed.emit(self.is_loading)
        if self.is_ready:
            self.status_changed.emit(f"{self.transcriber.backend_name} {self.transcriber.selection_label.lower()} ready")
        else:
            self.status_changed.emit(
                f"Download {self.transcriber.backend_name} {self.transcriber.selection_label.lower()} to enable audio"
            )
        self.progress_changed.emit(self._aggregate_progress())

    def _download_model(self, model_name: str) -> None:
        try:
            self.status_changed.emit(
                f"Downloading {self.transcriber.backend_name} {self.transcriber.selection_label.lower()} with aria2"
            )
            self.transcriber.download_item(
                model_name,
                progress_callback=lambda p, n=model_name: self._emit_progress_for(n, p),
            )
        except Exception as exc:
            self._logger.exception("Whisper model load failed")
            self.load_failed.emit(model_name, str(exc))
            return

        self.load_completed.emit(model_name)

    def _delete_model(self, model_name: str) -> None:
        try:
            self.transcriber.remove_item(model_name)
        except Exception as exc:
            self._logger.exception("Whisper model delete failed")
            self.delete_failed.emit(model_name, str(exc))
            return

        self.delete_completed.emit(model_name)

    def _handle_done(self, future: Future[None], model_name: str) -> None:
        try:
            future.result()
        except Exception:
            self._logger.exception("Whisper future raised unexpectedly for model=%s", model_name)
            if model_name in self._active_items:
                self.load_failed.emit(model_name, "Model download failed unexpectedly")

    def _finish_success(self, model_name: str) -> None:
        self._active_items.discard(model_name)
        total = self._progress_by_item.pop(model_name, _EMPTY_PROGRESS).total_bytes or 1
        self.item_loading_changed.emit(model_name, False)
        self.item_progress_changed.emit(
            model_name,
            DownloadProgress(completed_bytes=total, total_bytes=total, download_speed_bytes_per_second=0),
        )
        if not self._active_items:
            self.loading_changed.emit(False)
        self.ready_changed.emit(self.is_ready)
        self.status_changed.emit(f"{self.transcriber.backend_name} {self.transcriber.selection_label.lower()} ready")
        self.progress_changed.emit(self._aggregate_progress())

    def _finish_failure(self, model_name: str, message: str) -> None:
        self._active_items.discard(model_name)
        self._progress_by_item.pop(model_name, None)
        self.item_loading_changed.emit(model_name, False)
        self.item_progress_changed.emit(model_name, _EMPTY_PROGRESS)
        if not self._active_items:
            self.loading_changed.emit(False)
        self.ready_changed.emit(self.is_ready)
        self.status_changed.emit(f"{self.transcriber.backend_name} load failed")
        self.error_changed.emit(message)
        self.progress_changed.emit(self._aggregate_progress())

    def _finish_delete_success(self, model_name: str) -> None:
        self._active_items.discard(model_name)
        self._progress_by_item.pop(model_name, None)
        self.item_loading_changed.emit(model_name, False)
        self.item_progress_changed.emit(model_name, _EMPTY_PROGRESS)
        if not self._active_items:
            self.loading_changed.emit(False)
        self.ready_changed.emit(self.is_ready)
        self.status_changed.emit(f"{self.transcriber.backend_name} model removed")
        self.progress_changed.emit(self._aggregate_progress())

    def _finish_delete_failure(self, model_name: str, message: str) -> None:
        self._active_items.discard(model_name)
        self._progress_by_item.pop(model_name, None)
        self.item_loading_changed.emit(model_name, False)
        self.item_progress_changed.emit(model_name, _EMPTY_PROGRESS)
        if not self._active_items:
            self.loading_changed.emit(False)
        self.ready_changed.emit(self.is_ready)
        self.status_changed.emit(f"{self.transcriber.backend_name} remove failed")
        self.error_changed.emit(message)
        self.progress_changed.emit(self._aggregate_progress())

    def _emit_progress_for(self, model_name: str, progress: DownloadProgress) -> None:
        self._progress_by_item[model_name] = progress
        self.item_progress_changed.emit(model_name, progress)
        self.progress_changed.emit(self._aggregate_progress())

    def _aggregate_progress(self) -> DownloadProgress:
        if not self._progress_by_item:
            return _EMPTY_PROGRESS
        completed = sum(p.completed_bytes for p in self._progress_by_item.values())
        total = sum(p.total_bytes for p in self._progress_by_item.values())
        speed = sum(p.download_speed_bytes_per_second for p in self._progress_by_item.values())
        return DownloadProgress(completed_bytes=completed, total_bytes=total, download_speed_bytes_per_second=speed)
