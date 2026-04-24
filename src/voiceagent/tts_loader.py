from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
import logging

from PySide6.QtCore import QObject, Signal

from voiceagent.backends import TextToSpeechBackend
from voiceagent.downloaders import DownloadProgress


_EMPTY_PROGRESS = DownloadProgress(completed_bytes=0, total_bytes=0, download_speed_bytes_per_second=0)


class TtsVoiceLoader(QObject):
    selection_changed = Signal(str)
    ready_changed = Signal(bool)
    loading_changed = Signal(bool)
    status_changed = Signal(str)
    # Aggregate progress across all active downloads (weighted by byte totals).
    progress_changed = Signal(object)
    # Per-model signals for parallel-install UI.
    item_loading_changed = Signal(str, bool)  # (model_name, is_loading)
    item_progress_changed = Signal(str, object)  # (model_name, DownloadProgress)
    error_changed = Signal(str)
    load_completed = Signal(str)  # model_name
    load_failed = Signal(str, str)  # (model_name, error_message)
    delete_completed = Signal(str)  # model_name
    delete_failed = Signal(str, str)  # (model_name, error_message)

    def __init__(self, tts_service: TextToSpeechBackend, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.tts_service = tts_service
        self.executor = ThreadPoolExecutor(max_workers=3, thread_name_prefix="voiceagent-tts-loader")
        self._active_items: set[str] = set()
        self._progress_by_item: dict[str, DownloadProgress] = {}
        self._logger = logging.getLogger(__name__)

        self.load_completed.connect(self._finish_success)
        self.load_failed.connect(self._finish_failure)
        self.delete_completed.connect(self._finish_delete_success)
        self.delete_failed.connect(self._finish_delete_failure)
        self._emit_initial_state()

    @property
    def is_enabled(self) -> bool:
        return self.tts_service.enabled

    @property
    def is_ready(self) -> bool:
        return self.tts_service.is_available

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
    def selected_model(self) -> str | None:
        return self.tts_service.selected_item

    def select_model(self, model_name: str | None) -> None:
        self.tts_service.set_selected_item(model_name)
        self._log_state("select_model")
        self.selection_changed.emit(model_name or "")
        self._emit_initial_state()

    def load_voice(self) -> None:
        if (
            not self.is_enabled
            or not self.selected_model
            or self.selected_model in self._active_items
            or self.is_ready
        ):
            return
        self.download_voice(self.selected_model)

    def download_voice(self, model_name: str) -> None:
        if (
            not self.tts_service.command
            or not model_name
            or model_name in self._active_items
            or self.tts_service.is_item_available(model_name)
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
        self.status_changed.emit(
            f"Preparing {self.tts_service.backend_name} {self.tts_service.selection_label.lower()} download"
        )
        self.progress_changed.emit(self._aggregate_progress())

        future = self.executor.submit(self._load_voice, model_name)
        future.add_done_callback(lambda f, n=model_name: self._handle_done(f, n))

    def select_and_load(self, model_name: str) -> None:
        """Select a model and immediately start downloading it."""
        self.tts_service.set_selected_item(model_name)
        self._log_state("select_and_load")
        self.selection_changed.emit(model_name)
        self._emit_initial_state()
        self.download_voice(model_name)

    def delete_voice(self, model_name: str) -> None:
        if (
            not model_name
            or model_name in self._active_items
            or not self.tts_service.is_item_available(model_name)
        ):
            return

        was_idle = not self._active_items
        self._active_items.add(model_name)
        self._progress_by_item[model_name] = _EMPTY_PROGRESS
        self.item_loading_changed.emit(model_name, True)
        if was_idle:
            self.loading_changed.emit(True)
        self.error_changed.emit("")
        self.status_changed.emit(
            f"Removing {self.tts_service.backend_name} {self.tts_service.selection_label.lower()}"
        )
        self.progress_changed.emit(self._aggregate_progress())

        future = self.executor.submit(self._delete_voice, model_name)
        future.add_done_callback(lambda f, n=model_name: self._handle_done(f, n))

    def shutdown(self) -> None:
        self.executor.shutdown(wait=False, cancel_futures=True)

    def _emit_initial_state(self) -> None:
        self._log_state("emit_initial_state")
        self.ready_changed.emit(self.is_ready)
        self.loading_changed.emit(self.is_loading)
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
        self.progress_changed.emit(self._aggregate_progress())

    def _load_voice(self, model_name: str) -> None:
        try:
            self.status_changed.emit(
                f"Downloading {self.tts_service.backend_name} {self.tts_service.selection_label.lower()} with aria2"
            )
            self.tts_service.download_item(
                model_name,
                progress_callback=lambda p, n=model_name: self._emit_progress_for(n, p),
            )
        except Exception as exc:
            self._logger.exception("Piper voice load failed")
            self.load_failed.emit(model_name, str(exc))
            return

        self.load_completed.emit(model_name)

    def _delete_voice(self, model_name: str) -> None:
        try:
            self.tts_service.remove_item(model_name)
        except Exception as exc:
            self._logger.exception("Piper voice delete failed")
            self.delete_failed.emit(model_name, str(exc))
            return

        self.delete_completed.emit(model_name)

    def _handle_done(self, future: Future[None], model_name: str) -> None:
        try:
            future.result()
        except Exception:
            self._logger.exception("TTS future raised unexpectedly for model=%s", model_name)
            if model_name in self._active_items:
                self.load_failed.emit(model_name, "Voice download failed unexpectedly")

    def _finish_success(self, model_name: str) -> None:
        self._active_items.discard(model_name)
        total = self._progress_by_item.pop(model_name, _EMPTY_PROGRESS).total_bytes or 1
        self._log_state("finish_success")
        self.item_loading_changed.emit(model_name, False)
        self.item_progress_changed.emit(
            model_name,
            DownloadProgress(completed_bytes=total, total_bytes=total, download_speed_bytes_per_second=0),
        )
        if not self._active_items:
            self.loading_changed.emit(False)
        self.ready_changed.emit(self.is_ready)
        self.status_changed.emit(f"{self.tts_service.backend_name} {self.tts_service.selection_label.lower()} ready")
        self.progress_changed.emit(self._aggregate_progress())

    def _finish_failure(self, model_name: str, message: str) -> None:
        self._logger.error("TTS load failed model=%s message=%s", model_name, message)
        self._active_items.discard(model_name)
        self._progress_by_item.pop(model_name, None)
        self._log_state("finish_failure")
        self.item_loading_changed.emit(model_name, False)
        self.item_progress_changed.emit(model_name, _EMPTY_PROGRESS)
        if not self._active_items:
            self.loading_changed.emit(False)
        self.ready_changed.emit(self.is_ready)
        self.status_changed.emit(f"{self.tts_service.backend_name} load failed")
        self.error_changed.emit(message)
        self.progress_changed.emit(self._aggregate_progress())

    def _finish_delete_success(self, model_name: str) -> None:
        self._active_items.discard(model_name)
        self._progress_by_item.pop(model_name, None)
        self._log_state("finish_delete_success")
        self.item_loading_changed.emit(model_name, False)
        self.item_progress_changed.emit(model_name, _EMPTY_PROGRESS)
        if not self._active_items:
            self.loading_changed.emit(False)
        self.ready_changed.emit(self.is_ready)
        self.status_changed.emit(f"{self.tts_service.backend_name} voice removed")
        self.progress_changed.emit(self._aggregate_progress())

    def _finish_delete_failure(self, model_name: str, message: str) -> None:
        self._logger.error("TTS delete failed model=%s message=%s", model_name, message)
        self._active_items.discard(model_name)
        self._progress_by_item.pop(model_name, None)
        self.item_loading_changed.emit(model_name, False)
        self.item_progress_changed.emit(model_name, _EMPTY_PROGRESS)
        if not self._active_items:
            self.loading_changed.emit(False)
        self.ready_changed.emit(self.is_ready)
        self.status_changed.emit(f"{self.tts_service.backend_name} remove failed")
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

    def _log_state(self, context: str) -> None:
        details_getter = getattr(self.tts_service, "describe_selection_state", None)
        if callable(details_getter):
            details = details_getter()
            self._logger.info(
                "TTS state context=%s enabled=%s ready=%s loading=%s active_items=%s selected_model=%s available=%s can_download=%s resolved_model_path=%s direct_candidate=%s local_candidate=%s onnx_candidate=%s json_candidate=%s",
                context,
                self.is_enabled,
                self.is_ready,
                self.is_loading,
                sorted(self._active_items),
                details.get("selected_model", ""),
                details.get("available", False),
                details.get("can_download", False),
                details.get("resolved_model_path", ""),
                details.get("direct_candidate", ""),
                details.get("local_candidate", ""),
                details.get("onnx_candidate", ""),
                details.get("json_candidate", ""),
            )
            return

        self._logger.info(
            "TTS state context=%s enabled=%s ready=%s loading=%s active_items=%s selected_model=%s",
            context,
            self.is_enabled,
            self.is_ready,
            self.is_loading,
            sorted(self._active_items),
            self.selected_model,
        )
