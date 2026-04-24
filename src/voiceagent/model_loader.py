from __future__ import annotations

from PySide6.QtCore import QObject

from voiceagent.backends import SpeechToTextBackend
from voiceagent.parallel_item_loader import ParallelItemLoader


class WhisperModelLoader(ParallelItemLoader):
    """Whisper-flavored loader. The state machine lives in `ParallelItemLoader`."""

    def __init__(
        self, transcriber: SpeechToTextBackend, parent: QObject | None = None
    ) -> None:
        super().__init__(
            transcriber,
            max_workers=3,
            thread_name_prefix="voiceagent-model-loader",
            parent=parent,
        )
        self.transcriber = transcriber
        self._emit_initial_state()

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
        self.download_item(model_name)

    def delete_model(self, model_name: str) -> None:
        self.delete_item(model_name)

    # -- internal ----------------------------------------------------------

    def _emit_initial_state(self) -> None:
        self.ready_changed.emit(self.is_ready)
        self.loading_changed.emit(self.is_loading)
        if self.is_ready:
            self.status_changed.emit(self._status_ready())
        else:
            self.status_changed.emit(self._status_select_to_enable())
        self.progress_changed.emit(self._aggregate_progress())

    # -- subclass status hooks --------------------------------------------

    def _status_checking(self) -> str:
        return (
            f"Checking {self.transcriber.backend_name} "
            f"{self.transcriber.selection_label.lower()} files"
        )

    def _status_downloading(self) -> str:
        return (
            f"Downloading {self.transcriber.backend_name} "
            f"{self.transcriber.selection_label.lower()} with aria2"
        )

    def _status_removing(self) -> str:
        return (
            f"Removing {self.transcriber.backend_name} "
            f"{self.transcriber.selection_label.lower()}"
        )

    def _status_ready(self) -> str:
        return (
            f"{self.transcriber.backend_name} "
            f"{self.transcriber.selection_label.lower()} ready"
        )

    def _status_load_failed(self) -> str:
        return f"{self.transcriber.backend_name} load failed"

    def _status_remove_failed(self) -> str:
        return f"{self.transcriber.backend_name} remove failed"

    def _status_idle_prompt(self) -> str:
        return (
            f"Select a {self.transcriber.backend_name} "
            f"{self.transcriber.selection_label.lower()}"
        )

    def _status_removed_ok(self) -> str:
        return f"{self.transcriber.backend_name} model removed"

    def _status_select_to_enable(self) -> str:
        return (
            f"Download {self.transcriber.backend_name} "
            f"{self.transcriber.selection_label.lower()} to enable audio"
        )
