from __future__ import annotations

from PySide6.QtCore import QSettings, Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QStatusBar,
    QVBoxLayout,
    QWidget,
)

from voiceagent.audio_check import AudioCheckController, AudioCheckDialog
from voiceagent.controller import VoiceController
from voiceagent.downloaders import format_bytes, format_transfer_rate
from voiceagent.model_loader import WhisperModelLoader
from voiceagent.replay_widgets import ConversationView
from voiceagent.services.playback import AudioPlayer
from voiceagent.services.chat import LmStudioClient
from voiceagent.services.stt import WhisperTranscriber
from voiceagent.services.tts import PiperTtsService
from voiceagent.tts_loader import TtsVoiceLoader


class MainWindow(QMainWindow):
    def __init__(
        self,
        controller: VoiceController,
        audio_check_controller: AudioCheckController,
        model_loader: WhisperModelLoader,
        tts_loader: TtsVoiceLoader,
    ) -> None:
        super().__init__()
        self.controller = controller
        self.audio_check_controller = audio_check_controller
        self.model_loader = model_loader
        self.tts_loader = tts_loader
        self.settings = QSettings("voiceagent", "voiceagent")
        self.settings.remove("current_llm_model")
        self.settings.remove("llm_model_history")
        self.replay_player = AudioPlayer(self)
        self.audio_check_dialog: QDialog | None = None
        self._default_llm_url = "silverthread:1234"
        self._stt_catalog = WhisperTranscriber.available_model_names()
        self._tts_catalog = PiperTtsService.available_voice_names(
            self.tts_loader.tts_service.model_root,
            self.tts_loader.selected_model,
        )
        self.setWindowTitle("Voice Agent")
        self.resize(760, 520)

        root = QWidget(self)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(12)

        self.stt_selector = QComboBox(root)
        self.stt_download_button = QPushButton("Download STT", root)
        self.stt_download_button.clicked.connect(self._download_selected_stt_model)
        self.stt_selector.currentTextChanged.connect(self._handle_stt_selection_changed)

        stt_row = QHBoxLayout()
        stt_row.setSpacing(8)
        stt_row.addWidget(QLabel("STT Model", root))
        stt_row.addWidget(self.stt_selector, 1)
        stt_row.addWidget(self.stt_download_button)

        self.model_status_label = QLabel("Load Whisper model to enable audio", root)
        self.model_status_label.setWordWrap(True)

        self.model_progress_bar = QProgressBar(root)
        self.model_progress_bar.setVisible(False)
        self.model_progress_bar.setMinimum(0)
        self.model_progress_bar.setMaximum(1)

        self.model_progress_detail_label = QLabel("", root)
        self.model_progress_detail_label.setVisible(False)
        self.model_progress_detail_label.setWordWrap(True)

        self.tts_status_label = QLabel("Load Piper voice to enable speech", root)
        self.tts_status_label.setWordWrap(True)
        self.tts_status_label.setVisible(self.tts_loader.is_enabled)

        self.tts_selector = QComboBox(root)
        self.tts_download_button = QPushButton("Download Voice", root)
        self.tts_download_button.clicked.connect(self._download_selected_tts_model)
        self.tts_selector.currentTextChanged.connect(self._handle_tts_selection_changed)

        tts_row = QHBoxLayout()
        tts_row.setSpacing(8)
        tts_row.addWidget(QLabel("TTS Voice", root))
        tts_row.addWidget(self.tts_selector, 1)
        tts_row.addWidget(self.tts_download_button)

        self.tts_progress_bar = QProgressBar(root)
        self.tts_progress_bar.setVisible(False)
        self.tts_progress_bar.setMinimum(0)
        self.tts_progress_bar.setMaximum(1)

        self.tts_progress_detail_label = QLabel("", root)
        self.tts_progress_detail_label.setVisible(False)
        self.tts_progress_detail_label.setWordWrap(True)

        self.llm_url_selector = QComboBox(root)
        self.llm_url_selector.setEditable(True)
        self.llm_url_selector.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        if self.llm_url_selector.lineEdit() is not None:
            self.llm_url_selector.lineEdit().editingFinished.connect(self._persist_current_llm_url)
        self.llm_url_selector.activated.connect(lambda _index: self._persist_current_llm_url())
        self.llm_url_selector.currentTextChanged.connect(self._handle_llm_url_changed)

        llm_row = QHBoxLayout()
        llm_row.setSpacing(8)
        llm_row.addWidget(QLabel("LLM URL", root))
        llm_row.addWidget(self.llm_url_selector, 1)

        self.llm_model_selector = QComboBox(root)
        self.llm_model_selector.setEditable(False)
        self.llm_model_selector.activated.connect(self._load_selected_llm_model)

        llm_model_row = QHBoxLayout()
        llm_model_row.setSpacing(8)
        llm_model_row.addWidget(QLabel("LLM Model", root))
        llm_model_row.addWidget(self.llm_model_selector, 1)

        self.push_to_talk_button = QPushButton("Click To Record", root)
        self.push_to_talk_button.setMinimumHeight(72)
        self.push_to_talk_button.clicked.connect(self._toggle_recording)
        self.push_to_talk_button.setVisible(False)

        self.audio_check_button = QPushButton("Audio Check", root)
        self.audio_check_button.clicked.connect(self._open_audio_check)

        self.conversation_view = ConversationView(self.tts_loader.tts_service, self.replay_player, root)

        self.error_label = QLabel("", root)
        self.error_label.setWordWrap(True)
        self.error_label.setStyleSheet("color: #b00020;")

        layout.addLayout(stt_row)
        layout.addWidget(self.model_status_label)
        layout.addWidget(self.model_progress_bar)
        layout.addWidget(self.model_progress_detail_label)
        layout.addLayout(tts_row)
        layout.addWidget(self.tts_status_label)
        layout.addWidget(self.tts_progress_bar)
        layout.addWidget(self.tts_progress_detail_label)
        layout.addLayout(llm_row)
        layout.addLayout(llm_model_row)
        layout.addWidget(self.push_to_talk_button)
        layout.addWidget(self.audio_check_button)
        layout.addWidget(self.conversation_view, 1)
        layout.addWidget(self.error_label)

        self.setCentralWidget(root)
        self.status_bar = QStatusBar(self)
        self.status_bar.showMessage("Ready")
        self.setStatusBar(self.status_bar)

        self.controller.status_changed.connect(self.status_bar.showMessage)
        self.controller.transcript_changed.connect(self._append_user_message)
        self.controller.response_changed.connect(self._append_assistant_message)
        self.controller.error_changed.connect(self.error_label.setText)
        self.controller.state_changed.connect(self._apply_state)
        self.model_loader.ready_changed.connect(self._apply_model_ready)
        self.model_loader.loading_changed.connect(self._apply_model_loading)
        self.model_loader.status_changed.connect(self._apply_model_status)
        self.model_loader.progress_changed.connect(self._apply_model_progress)
        self.model_loader.error_changed.connect(self.error_label.setText)
        self.model_loader.selection_changed.connect(self._sync_stt_selection)
        self.model_loader.load_completed.connect(self._populate_stt_selector)
        self.tts_loader.ready_changed.connect(self._apply_tts_ready)
        self.tts_loader.loading_changed.connect(self._apply_tts_loading)
        self.tts_loader.status_changed.connect(self._apply_tts_status)
        self.tts_loader.progress_changed.connect(self._apply_tts_progress)
        self.tts_loader.error_changed.connect(self.error_label.setText)
        self.tts_loader.selection_changed.connect(self._sync_tts_selection)
        self.tts_loader.load_completed.connect(self._populate_tts_selector)

        self._populate_stt_selector()
        self._populate_tts_selector()
        self._populate_llm_url_selector()
        self._populate_llm_model_selector([], "")
        self._restore_initial_selections()
        self._apply_state("idle")
        self._apply_model_ready(self.model_loader.is_ready)
        self._apply_model_loading(self.model_loader.is_loading)
        self._apply_tts_ready(self.tts_loader.is_ready)
        self._apply_tts_loading(self.tts_loader.is_loading)
        self._refresh_action_buttons()

    def closeEvent(self, event) -> None:  # noqa: N802
        self.controller.shutdown()
        self.audio_check_controller.shutdown()
        self.model_loader.shutdown()
        self.tts_loader.shutdown()
        self.replay_player.stop()
        if self.audio_check_dialog is not None:
            self.audio_check_dialog.close()
        super().closeEvent(event)

    def _apply_state(self, state: str) -> None:
        can_record = self._main_actions_ready() and (state == "idle" or state == "recording")
        self.push_to_talk_button.setEnabled(can_record)

        if state == "recording":
            self.push_to_talk_button.setText("Click To Send")
        elif state == "transcribing":
            self.push_to_talk_button.setText("Transcribing...")
        elif state == "thinking":
            self.push_to_talk_button.setText("Thinking...")
        elif state == "synthesizing":
            self.push_to_talk_button.setText("Synthesizing...")
        elif state == "speaking":
            self.push_to_talk_button.setText("Speaking...")
        else:
            self.push_to_talk_button.setText("Click To Record")

    def _open_audio_check(self) -> None:
        if not self._main_actions_ready():
            return

        if self.audio_check_dialog is None:
            self.audio_check_dialog = AudioCheckDialog(self.audio_check_controller, self)

        self.audio_check_dialog.show()
        self.audio_check_dialog.raise_()
        self.audio_check_dialog.activateWindow()

    def _toggle_recording(self) -> None:
        if self.controller.state == "recording":
            self.controller.stop_recording()
            return

        if self.controller.state == "idle":
            self._persist_current_llm_url()
            self.controller.start_recording()

    def _apply_model_ready(self, ready: bool) -> None:
        self.model_status_label.setVisible(self.model_loader.is_loading or not ready)
        self._refresh_stt_controls()
        self._refresh_action_buttons()

    def _apply_model_loading(self, loading: bool) -> None:
        self.model_progress_bar.setVisible(loading)
        self.model_progress_detail_label.setVisible(loading)
        self._refresh_stt_controls()
        self._refresh_action_buttons()

    def _apply_model_status(self, status: str) -> None:
        self.model_status_label.setText(status)

    def _apply_model_progress(self, progress) -> None:
        current = progress.completed_bytes
        total = progress.total_bytes
        speed = progress.download_speed_bytes_per_second
        if total > 0:
            percent = int((current / total) * 1000)
            self.model_progress_bar.setRange(0, 1000)
            self.model_progress_bar.setValue(percent)
            detail = f"{percent / 10:.1f}% ({format_bytes(current)} / {format_bytes(total)})"
            if speed > 0:
                detail += f" at {format_transfer_rate(speed)}"
            self.model_progress_detail_label.setText(detail)
        else:
            self.model_progress_bar.setRange(0, 0)
            self.model_progress_detail_label.setText("Waiting for aria2 download telemetry")

    def _apply_tts_ready(self, ready: bool) -> None:
        available = self.tts_loader.is_ready
        if not self.tts_loader.is_enabled:
            self.tts_status_label.setVisible(False)
        else:
            self.tts_status_label.setVisible(not available and self.tts_loader.is_loading)
            if available:
                self.tts_status_label.setText("Piper voice ready")
        self._refresh_tts_controls()
        self._refresh_action_buttons()

    def _apply_tts_loading(self, loading: bool) -> None:
        available = self.tts_loader.is_ready
        self.tts_progress_bar.setVisible(loading and not available)
        self.tts_progress_detail_label.setVisible(loading and not available)
        if available:
            self.tts_status_label.setVisible(False)
        self._refresh_tts_controls()
        self._refresh_action_buttons()

    def _apply_tts_status(self, status: str) -> None:
        self.tts_status_label.setText(status)

    def _apply_tts_progress(self, progress) -> None:
        current = progress.completed_bytes
        total = progress.total_bytes
        speed = progress.download_speed_bytes_per_second
        if self.tts_loader.is_ready and total > 0 and current >= total:
            self.tts_status_label.setText("Piper voice ready")
            self.tts_progress_bar.setVisible(False)
            self.tts_progress_detail_label.setVisible(False)
            self._refresh_tts_controls()
            return
        if total > 0:
            percent = int((current / total) * 1000)
            self.tts_progress_bar.setRange(0, 1000)
            self.tts_progress_bar.setValue(percent)
            detail = f"{percent / 10:.1f}% ({format_bytes(current)} / {format_bytes(total)})"
            if speed > 0:
                detail += f" at {format_transfer_rate(speed)}"
            self.tts_progress_detail_label.setText(detail)
        else:
            self.tts_progress_bar.setRange(0, 0)
            self.tts_progress_detail_label.setText("Waiting for aria2 download telemetry")

    def _populate_stt_selector(self) -> None:
        current_model = self.model_loader.selected_model
        self.stt_selector.blockSignals(True)
        current_selection = self.stt_selector.currentText() or current_model
        self.stt_selector.clear()
        for index, model_name in enumerate(self._stt_catalog):
            self.stt_selector.addItem(model_name)
            if self._is_stt_downloaded(model_name):
                self.stt_selector.setItemData(index, QColor("#1f8f4c"), Qt.ItemDataRole.ForegroundRole)
        match_index = self.stt_selector.findText(current_selection)
        if match_index >= 0:
            self.stt_selector.setCurrentIndex(match_index)
        self.stt_selector.blockSignals(False)
        self._refresh_stt_controls()
        self._refresh_action_buttons()

    def _populate_tts_selector(self) -> None:
        current_model = self.tts_loader.selected_model or ""
        self.tts_selector.blockSignals(True)
        current_selection = self.tts_selector.currentText() or current_model
        self.tts_selector.clear()
        for index, model_name in enumerate(self._tts_catalog):
            self.tts_selector.addItem(model_name)
            if self._is_tts_downloaded(model_name):
                self.tts_selector.setItemData(index, QColor("#1f8f4c"), Qt.ItemDataRole.ForegroundRole)
        match_index = self.tts_selector.findText(current_selection)
        if match_index >= 0:
            self.tts_selector.setCurrentIndex(match_index)
        self.tts_selector.blockSignals(False)
        self._refresh_tts_controls()
        self._refresh_action_buttons()

    def _refresh_stt_controls(self) -> None:
        selected_model = self.stt_selector.currentText()
        is_downloaded = self._is_stt_downloaded(selected_model) if selected_model else False
        show_download = bool(selected_model) and not is_downloaded
        self.stt_download_button.setVisible(show_download)
        self.stt_download_button.setEnabled(show_download and not self.model_loader.is_loading)
        self._refresh_action_buttons()

    def _refresh_tts_controls(self) -> None:
        selected_model = self.tts_selector.currentText()
        has_selection = bool(selected_model)
        if not has_selection:
            self.tts_download_button.setVisible(False)
            self.tts_download_button.setEnabled(False)
            self._refresh_action_buttons()
            return

        is_downloaded = self._is_tts_downloaded(selected_model) if selected_model else False
        show_download = bool(selected_model) and not is_downloaded
        self.tts_download_button.setVisible(show_download)
        self.tts_download_button.setEnabled(show_download and not self.tts_loader.is_loading)
        self._refresh_action_buttons()

    def _download_selected_stt_model(self) -> None:
        selected_model = self.stt_selector.currentText()
        if not selected_model:
            return
        self.model_loader.load_model()

    def _download_selected_tts_model(self) -> None:
        selected_model = self.tts_selector.currentText()
        if not selected_model:
            return
        self.tts_loader.load_voice()

    def _sync_stt_selection(self, model_name: str) -> None:
        if self.stt_selector.currentText() != model_name:
            index = self.stt_selector.findText(model_name)
            if index >= 0:
                self.stt_selector.setCurrentIndex(index)
        self._populate_stt_selector()

    def _sync_tts_selection(self, model_name: str) -> None:
        if model_name and model_name not in self._tts_catalog:
            self._tts_catalog.append(model_name)
            self._tts_catalog.sort()
        if self.tts_selector.currentText() != model_name:
            index = self.tts_selector.findText(model_name)
            if index >= 0:
                self.tts_selector.setCurrentIndex(index)
        self._populate_tts_selector()

    def _is_stt_downloaded(self, model_name: str) -> bool:
        return WhisperTranscriber.is_model_available(self.model_loader.transcriber.model_root, model_name)

    def _is_tts_downloaded(self, model_name: str) -> bool:
        return PiperTtsService.is_voice_available(self.tts_loader.tts_service.model_root, model_name)

    def _main_actions_ready(self) -> bool:
        return self._audio_check_ready() and self._llm_ready()

    def _audio_check_ready(self) -> bool:
        selected_stt = self.stt_selector.currentText()
        selected_tts = self.tts_selector.currentText()
        stt_ready = (
            bool(selected_stt)
            and self._is_stt_downloaded(selected_stt)
        )
        tts_ready = (
            bool(selected_tts)
            and self._is_tts_downloaded(selected_tts)
        )
        return stt_ready and tts_ready

    def _llm_ready(self) -> bool:
        return bool(self.controller.chat_client.base_url and self.controller.chat_client.model)

    def _refresh_action_buttons(self) -> None:
        talk_ready = self._main_actions_ready()
        audio_check_ready = self._audio_check_ready()
        self.push_to_talk_button.setVisible(talk_ready)
        self.audio_check_button.setEnabled(audio_check_ready)
        self.audio_check_button.setToolTip(
            "" if audio_check_ready else "Select downloaded STT and TTS models first."
        )
        if self.audio_check_dialog is not None and not audio_check_ready:
            self.audio_check_dialog.close()
            self.audio_check_dialog = None
        self._apply_state(self.controller.state.value)

    def _handle_stt_selection_changed(self, model_name: str) -> None:
        self.settings.setValue("selected_stt_model", model_name)
        if model_name and model_name != self.model_loader.selected_model:
            self.model_loader.select_model(model_name)
        self._refresh_stt_controls()

    def _handle_tts_selection_changed(self, model_name: str) -> None:
        self.settings.setValue("selected_tts_model", model_name)
        if model_name and model_name != (self.tts_loader.selected_model or ""):
            self.tts_loader.select_model(model_name)
        self._refresh_tts_controls()

    def _handle_llm_url_changed(self, value: str) -> None:
        self.controller.chat_client.set_base_url(value)
        self.controller.chat_client.set_model("")
        self._populate_llm_model_selector([], "")
        self._refresh_action_buttons()

    def _append_user_message(self, text: str) -> None:
        self.conversation_view.add_user_message(text)

    def _append_assistant_message(self, text: str) -> None:
        self.conversation_view.add_assistant_message(text)

    def _restore_initial_selections(self) -> None:
        stt_model = self._resolve_initial_selection(
            self._stt_catalog,
            self.settings.value("selected_stt_model", "", str) or "",
            self.model_loader.selected_model,
            self._is_stt_downloaded,
        )
        tts_model = self._resolve_initial_selection(
            self._tts_catalog,
            self.settings.value("selected_tts_model", "", str) or "",
            self.tts_loader.selected_model or "",
            self._is_tts_downloaded,
        )
        self._set_selector_value(self.stt_selector, stt_model, self._handle_stt_selection_changed)
        self._set_selector_value(self.tts_selector, tts_model, self._handle_tts_selection_changed)
        self._set_selector_value(self.llm_url_selector, self._initial_llm_url(), self._handle_llm_url_changed)
        self._refresh_llm_models(show_error=False)

    def _resolve_initial_selection(
        self,
        catalog: list[str],
        persisted_model: str,
        fallback_model: str,
        is_downloaded,
    ) -> str:
        for candidate in (persisted_model, fallback_model):
            if candidate and candidate in catalog and is_downloaded(candidate):
                return candidate
        for candidate in catalog:
            if is_downloaded(candidate):
                return candidate
        for candidate in (persisted_model, fallback_model):
            if candidate and candidate in catalog:
                return candidate
        return catalog[0] if catalog else ""

    def _set_selector_value(self, selector: QComboBox, value: str, on_change) -> None:
        if not value:
            return
        index = selector.findText(value)
        if index < 0:
            return
        if selector.currentIndex() != index:
            selector.setCurrentIndex(index)
            return
        on_change(value)

    def _populate_llm_url_selector(self) -> None:
        history = self.settings.value("llm_url_history", [], list) or []
        entries = [entry for entry in history if isinstance(entry, str) and entry.strip()]
        current_base_url = self.controller.chat_client.base_url
        if current_base_url:
            entries.insert(0, current_base_url)
        default_url = self._default_llm_url
        entries.insert(0, default_url)

        unique_entries: list[str] = []
        for entry in entries:
            normalized = entry.strip()
            if normalized and normalized not in unique_entries:
                unique_entries.append(normalized)

        self.llm_url_selector.blockSignals(True)
        self.llm_url_selector.clear()
        self.llm_url_selector.addItems(unique_entries)
        self.llm_url_selector.blockSignals(False)

    def _populate_llm_model_selector(self, models: list[str], loaded_model: str) -> None:
        unique_models: list[str] = []
        for model in models:
            normalized = model.strip()
            if normalized and normalized not in unique_models:
                unique_models.append(normalized)

        self.llm_model_selector.blockSignals(True)
        self.llm_model_selector.clear()
        self.llm_model_selector.addItem("")
        self.llm_model_selector.addItems(unique_models)
        selected_index = 0
        if loaded_model:
            match_index = self.llm_model_selector.findText(loaded_model)
            if match_index >= 0:
                selected_index = match_index
        self.llm_model_selector.setCurrentIndex(selected_index)
        self.llm_model_selector.blockSignals(False)
        self.controller.chat_client.set_model(loaded_model)

    def _initial_llm_url(self) -> str:
        stored = self.settings.value("current_llm_url", "", str) or ""
        if stored:
            return stored
        return self._default_llm_url

    def _persist_current_llm_url(self) -> None:
        value = self.llm_url_selector.currentText().strip()
        if not value:
            return

        self.settings.setValue("current_llm_url", value)
        history = self.settings.value("llm_url_history", [], list) or []
        entries = [entry for entry in history if isinstance(entry, str) and entry.strip()]
        updated_entries = [value, *[entry for entry in entries if entry != value]]
        self.settings.setValue("llm_url_history", updated_entries[:10])
        self._populate_llm_url_selector()
        self._set_selector_value(self.llm_url_selector, value, self._handle_llm_url_changed)
        self._refresh_llm_models(show_error=True)

    def _refresh_llm_models(self, show_error: bool) -> None:
        try:
            models = self.controller.chat_client.list_models()
        except RuntimeError as exc:
            if show_error:
                self._show_llm_error("Unable to load LLM models", str(exc))
            self._populate_llm_model_selector([], "")
            self._refresh_action_buttons()
            return

        try:
            loaded_models = self.controller.chat_client.list_loaded_models()
        except RuntimeError:
            loaded_models = []

        loaded_model = loaded_models[0] if loaded_models else ""
        self._populate_llm_model_selector(models, loaded_model)
        self._refresh_action_buttons()

    def _load_selected_llm_model(self, index: int) -> None:
        if index <= 0:
            try:
                self.controller.chat_client.unload_all_models()
            except RuntimeError as exc:
                self._show_llm_error("Unable to unload LLM models", str(exc))
                self._refresh_llm_models(show_error=False)
                return
            self._populate_llm_model_selector(self._current_llm_models(), "")
            self._refresh_action_buttons()
            return

        selected_model = self.llm_model_selector.itemText(index).strip()
        if not selected_model:
            return

        try:
            loaded_model = self.controller.chat_client.load_model(selected_model)
        except RuntimeError as exc:
            self._show_llm_error("Unable to load LLM model", str(exc))
            self._refresh_llm_models(show_error=False)
            return

        self._populate_llm_model_selector(self._current_llm_models(), loaded_model)
        self._refresh_action_buttons()

    def _current_llm_models(self) -> list[str]:
        models: list[str] = []
        for index in range(1, self.llm_model_selector.count()):
            value = self.llm_model_selector.itemText(index).strip()
            if value:
                models.append(value)
        return models

    def _show_llm_error(self, title: str, message: str) -> None:
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle(title)
        box.setText(title)
        box.setInformativeText(message)
        box.exec()
