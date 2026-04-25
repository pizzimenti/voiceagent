from __future__ import annotations

from PySide6.QtCore import QObject

from voiceagent.backends import TextToSpeechBackend
from voiceagent.parallel_item_loader import ParallelItemLoader


class TtsVoiceLoader(ParallelItemLoader):
    """Piper-flavored loader. The state machine lives in `ParallelItemLoader`."""

    def __init__(
        self, tts_service: TextToSpeechBackend, parent: QObject | None = None
    ) -> None:
        super().__init__(
            tts_service,
            max_workers=3,
            thread_name_prefix="voiceagent-tts-loader",
            parent=parent,
        )
        self.tts_service = tts_service
        self._emit_initial_state()

    @property
    def is_enabled(self) -> bool:
        return self.tts_service.enabled

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
        self.download_item(model_name)

    def select_and_load(self, model_name: str) -> None:
        """Select a model and immediately start downloading it."""
        self.tts_service.set_selected_item(model_name)
        self._log_state("select_and_load")
        self.selection_changed.emit(model_name)
        self._emit_initial_state()
        self.download_voice(model_name)

    def delete_voice(self, model_name: str) -> None:
        self.delete_item(model_name)

    # -- subclass overrides ------------------------------------------------

    def _can_download(self, name: str) -> bool:
        # Piper requires its CLI command to be present before downloading.
        return bool(getattr(self.tts_service, "command", None))

    def _emit_initial_state(self) -> None:
        self._log_state("emit_initial_state")
        self.ready_changed.emit(self.is_ready)
        self.loading_changed.emit(self.is_loading)
        if not self.selected_model:
            self.status_changed.emit(self._status_idle_prompt())
        elif self.is_ready:
            self.status_changed.emit(self._status_ready())
        else:
            self.status_changed.emit(self._status_select_to_enable())
        self.progress_changed.emit(self._aggregate_progress())

    def _log_state(self, context: str) -> None:
        details_getter = getattr(self.tts_service, "describe_selection_state", None)
        if callable(details_getter):
            details = details_getter()
            self._logger.info(
                "TTS state context=%s enabled=%s ready=%s loading=%s active_items=%s "
                "selected_model=%s available=%s can_download=%s resolved_model_path=%s "
                "direct_candidate=%s local_candidate=%s onnx_candidate=%s json_candidate=%s",
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

    # -- subclass status hooks --------------------------------------------

    def _status_checking(self) -> str:
        return (
            f"Preparing {self.tts_service.backend_name} "
            f"{self.tts_service.selection_label.lower()} download"
        )

    def _status_downloading(self) -> str:
        return (
            f"Downloading {self.tts_service.backend_name} "
            f"{self.tts_service.selection_label.lower()} with aria2"
        )

    def _status_removing(self) -> str:
        return (
            f"Removing {self.tts_service.backend_name} "
            f"{self.tts_service.selection_label.lower()}"
        )

    def _status_ready(self) -> str:
        return (
            f"{self.tts_service.backend_name} "
            f"{self.tts_service.selection_label.lower()} ready"
        )

    def _status_load_failed(self) -> str:
        return f"{self.tts_service.backend_name} load failed"

    def _status_remove_failed(self) -> str:
        return f"{self.tts_service.backend_name} remove failed"

    def _status_idle_prompt(self) -> str:
        return (
            f"Select a {self.tts_service.backend_name} "
            f"{self.tts_service.selection_label.lower()}"
        )

    def _status_removed_ok(self) -> str:
        return f"{self.tts_service.backend_name} voice removed"

    def _status_select_to_enable(self) -> str:
        return (
            f"Load {self.tts_service.backend_name} "
            f"{self.tts_service.selection_label.lower()} to enable speech"
        )
