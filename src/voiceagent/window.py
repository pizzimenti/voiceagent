from __future__ import annotations

from datetime import datetime
import logging
from pathlib import Path
from typing import Callable

from PySide6.QtCore import (
    Property,
    QSettings,
    Qt,
    QTimer,
    QUrl,
    QObject,
    Signal,
    Slot,
)
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtWidgets import QApplication

from voiceagent.catalog_model import CatalogModel
from voiceagent.controller import VoiceController
from voiceagent.conversation_model import ConversationModel
from voiceagent.downloaders import format_bytes, format_transfer_rate
from voiceagent.model_loader import WhisperModelLoader
from voiceagent.models import AppState
from voiceagent.services.llm_controller import LlmController
from voiceagent.services.playback import AudioPlayer
from voiceagent.tts_loader import TtsVoiceLoader


class MainWindow(QObject):
    ui_changed = Signal()
    progress_changed = Signal()
    conversation_changed = Signal()
    # Per-row download visibility (active set membership). Fires only on start/end.
    downloads_changed = Signal()
    # Per-row download progress (fraction 0..1). Fires on every progress tick.
    downloads_progress_changed = Signal()

    def __init__(
        self,
        controller: VoiceController,
        model_loader: WhisperModelLoader,
        tts_loader: TtsVoiceLoader,
    ) -> None:
        super().__init__()
        self.controller = controller
        self.model_loader = model_loader
        self.tts_loader = tts_loader
        self.settings = QSettings("voiceagent", "voiceagent")
        self.replay_player = AudioPlayer(self)
        self._logger = logging.getLogger(__name__)
        self._stt_catalog = self.model_loader.transcriber.available_items()
        self._tts_catalog = self.tts_loader.tts_service.available_items()
        self._conversation_model = ConversationModel(self)
        self._error_message = ""
        self._status_message = "Ready"
        self._llm = LlmController(self.controller.chat_client, self.settings, parent=self)
        self._shutting_down = False
        self._state = "idle"
        self._model_progress_value = 0.0
        self._model_progress_indeterminate = False
        self._model_progress_text = ""
        self._tts_progress_value = 0.0
        self._tts_progress_indeterminate = False
        self._tts_progress_text = ""
        # Per-model in-flight tracking for parallel installs and per-row progress bars.
        self._stt_active_items: set[str] = set()
        self._tts_active_items: set[str] = set()
        self._stt_progress_by_item: dict[str, float] = {}
        self._tts_progress_by_item: dict[str, float] = {}
        # Incremental list models so per-row "installed" flips don't rebuild the whole
        # ListView (which would reset contentY and jump to top).
        self._stt_catalog_model = CatalogModel(
            self._stt_catalog, self._is_stt_downloaded, self
        )
        self._tts_catalog_model = CatalogModel(
            self._tts_catalog, self._is_tts_downloaded, self
        )

        self.controller.status_changed.connect(self._set_status_message)
        self.controller.connection_changed.connect(self._handle_connection_changed)
        self.controller.live_transcript_changed.connect(self._sync_live_user_message)
        self.controller.transcript_changed.connect(self._append_user_message)
        self.controller.response_changed.connect(self._append_assistant_message)
        self.controller.error_changed.connect(self._set_error_message)
        self.controller.state_changed.connect(self._apply_state)
        self.replay_player.playback_started.connect(self.controller.handle_aux_playback_started)
        self.replay_player.playback_finished.connect(self.controller.handle_aux_playback_finished)
        self.replay_player.playback_failed.connect(self.controller.handle_aux_playback_failed)
        self.model_loader.ready_changed.connect(self._emit_ui_changed)
        self.model_loader.loading_changed.connect(self._emit_ui_changed)
        self.model_loader.status_changed.connect(self._apply_model_status)
        self.model_loader.progress_changed.connect(self._apply_model_progress)
        self.model_loader.item_loading_changed.connect(self._on_stt_item_loading_changed)
        self.model_loader.item_progress_changed.connect(self._on_stt_item_progress_changed)
        self.model_loader.error_changed.connect(self._set_error_message)
        self.model_loader.selection_changed.connect(self._emit_ui_changed)
        self.model_loader.load_completed.connect(self._handle_inventory_change)
        self.model_loader.delete_completed.connect(self._handle_inventory_change)
        self.tts_loader.ready_changed.connect(self._emit_ui_changed)
        self.tts_loader.loading_changed.connect(self._emit_ui_changed)
        self.tts_loader.status_changed.connect(self._apply_tts_status)
        self.tts_loader.progress_changed.connect(self._apply_tts_progress)
        self.tts_loader.item_loading_changed.connect(self._on_tts_item_loading_changed)
        self.tts_loader.item_progress_changed.connect(self._on_tts_item_progress_changed)
        self.tts_loader.error_changed.connect(self._set_error_message)
        self.tts_loader.selection_changed.connect(self._emit_ui_changed)
        self.tts_loader.load_completed.connect(self._handle_inventory_change)
        self.tts_loader.delete_completed.connect(self._handle_inventory_change)
        self._llm.urls_changed.connect(self._on_llm_urls_changed)
        self._llm.current_url_changed.connect(self._on_llm_current_url_changed)
        self._llm.connection_state_changed.connect(self._on_llm_connection_state_changed)
        self._llm.connection_busy_changed.connect(self._on_llm_busy_changed)
        self._llm.model_busy_changed.connect(self._on_llm_busy_changed)
        self._llm.models_changed.connect(self._on_llm_models_changed)
        self._llm.selected_model_changed.connect(self._on_llm_selected_model_changed)
        self._llm.status_message.connect(self._on_llm_status_message)
        self._llm.error.connect(self._on_llm_error)

        self._restore_initial_selections()
        self._apply_audio_mute_state(self.settings.value("audio_output_muted", False, bool))
        self._apply_state(self.controller.state.value)
        self._apply_theme_mode(self.settings.value("theme_mode", "auto", str) or "auto")

        self.engine = QQmlApplicationEngine()
        self.engine.setInitialProperties({"voiceAgent": self})
        qml_path = Path(__file__).with_name("qml") / "MainWindow.qml"
        self.engine.load(QUrl.fromLocalFile(str(qml_path)))
        root_objects = self.engine.rootObjects()
        if not root_objects:
            raise RuntimeError(f"Failed to load QML interface from {qml_path}")
        self._window = root_objects[0]

        app = QApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self.shutdown)

    def show(self) -> None:
        if hasattr(self._window, "setVisible"):
            self._window.setVisible(True)
        if hasattr(self._window, "show"):
            self._window.show()
        if hasattr(self._window, "raise_"):
            self._window.raise_()
        elif hasattr(self._window, "raise"):
            getattr(self._window, "raise")()
        if hasattr(self._window, "requestActivate"):
            self._window.requestActivate()
        if not self._llm.startup_connect_scheduled and self.currentLlmUrl:
            self._llm.mark_startup_connect_scheduled()
            QTimer.singleShot(0, self.autoconnectLlmServer)

    def shutdown(self) -> None:
        if self._shutting_down:
            return
        self._shutting_down = True
        if hasattr(self, "_window") and self._window is not None:
            if hasattr(self._window, "setVisible"):
                self._window.setVisible(False)
            if hasattr(self._window, "close"):
                self._window.close()
            if hasattr(self._window, "deleteLater"):
                self._window.deleteLater()
            self._window = None
        if hasattr(self, "engine") and self.engine is not None:
            self.engine.collectGarbage()
            if hasattr(self.engine, "clearComponentCache"):
                self.engine.clearComponentCache()
            self.engine.deleteLater()
        self.controller.shutdown()
        self.model_loader.shutdown()
        self.tts_loader.shutdown()
        self.replay_player.stop()
        self._llm.shutdown()
        app = QApplication.instance()
        if app is not None:
            app.sendPostedEvents()
            app.processEvents()

    @Property("QVariantList", notify=ui_changed)
    def sttOptions(self) -> list[str]:  # noqa: N802
        return [name for name in self._stt_catalog if self._is_stt_downloaded(name)]

    @Property("QVariantList", notify=ui_changed)
    def ttsOptions(self) -> list[str]:  # noqa: N802
        return [name for name in self._tts_catalog if self._is_tts_downloaded(name)]

    @Property("QVariantList", notify=ui_changed)
    def sttCatalog(self) -> list[dict[str, object]]:  # noqa: N802
        return [{"name": name, "installed": self._is_stt_downloaded(name)} for name in self._stt_catalog]

    @Property("QVariantList", notify=ui_changed)
    def ttsCatalog(self) -> list[dict[str, object]]:  # noqa: N802
        return [{"name": name, "installed": self._is_tts_downloaded(name)} for name in self._tts_catalog]

    @Property(QObject, constant=True)
    def sttCatalogModel(self) -> CatalogModel:  # noqa: N802
        return self._stt_catalog_model

    @Property(QObject, constant=True)
    def ttsCatalogModel(self) -> CatalogModel:  # noqa: N802
        return self._tts_catalog_model

    @Property("QVariantList", notify=downloads_changed)
    def sttDownloadingList(self) -> list[str]:  # noqa: N802
        return sorted(self._stt_active_items)

    @Property("QVariantList", notify=downloads_changed)
    def ttsDownloadingList(self) -> list[str]:  # noqa: N802
        return sorted(self._tts_active_items)

    @Property("QVariantMap", notify=downloads_progress_changed)
    def sttProgressMap(self) -> dict[str, float]:  # noqa: N802
        return dict(self._stt_progress_by_item)

    @Property("QVariantMap", notify=downloads_progress_changed)
    def ttsProgressMap(self) -> dict[str, float]:  # noqa: N802
        return dict(self._tts_progress_by_item)

    @Property(str, notify=ui_changed)
    def selectedSttModel(self) -> str:  # noqa: N802
        current = self.model_loader.selected_model
        return current if current in self.sttOptions else ""

    @Property(str, notify=ui_changed)
    def selectedTtsModel(self) -> str:  # noqa: N802
        current = self.tts_loader.selected_model or ""
        return current if current in self.ttsOptions else ""

    @Property(str, notify=ui_changed)
    def modelStatus(self) -> str:  # noqa: N802
        if self.model_loader.is_loading:
            return f"Downloading {self.model_loader.transcriber.backend_name} model"
        if self.sttOptions:
            return f"{len(self.sttOptions)} installed STT model(s)"
        return "No STT models installed"

    @Property(bool, notify=ui_changed)
    def modelLoading(self) -> bool:  # noqa: N802
        return self.model_loader.is_loading

    @Property(float, notify=progress_changed)
    def modelProgressValue(self) -> float:  # noqa: N802
        return self._model_progress_value

    @Property(bool, notify=progress_changed)
    def modelProgressIndeterminate(self) -> bool:  # noqa: N802
        return self._model_progress_indeterminate

    @Property(str, notify=progress_changed)
    def modelProgressText(self) -> str:  # noqa: N802
        return self._model_progress_text

    @Property(str, notify=ui_changed)
    def ttsStatus(self) -> str:  # noqa: N802
        if self.tts_loader.is_loading:
            return f"Downloading {self.tts_loader.tts_service.backend_name} voice"
        if self.ttsOptions:
            return f"{len(self.ttsOptions)} installed TTS voice(s)"
        return "No TTS voices installed"

    @Property(bool, notify=ui_changed)
    def ttsLoading(self) -> bool:  # noqa: N802
        return self.tts_loader.is_loading

    @Property(float, notify=progress_changed)
    def ttsProgressValue(self) -> float:  # noqa: N802
        return self._tts_progress_value

    @Property(bool, notify=progress_changed)
    def ttsProgressIndeterminate(self) -> bool:  # noqa: N802
        return self._tts_progress_indeterminate

    @Property(str, notify=progress_changed)
    def ttsProgressText(self) -> str:  # noqa: N802
        return self._tts_progress_text

    @Property("QVariantList", notify=ui_changed)
    def llmUrls(self) -> list[str]:  # noqa: N802
        return self._llm.url_options()

    @Property(str, notify=ui_changed)
    def currentLlmUrl(self) -> str:  # noqa: N802
        return self._llm.current_url()

    @Property("QVariantList", notify=ui_changed)
    def llmModelOptions(self) -> list[str]:  # noqa: N802
        return ["", *self._llm.models]

    @Property(str, notify=ui_changed)
    def selectedLlmModel(self) -> str:  # noqa: N802
        return self.controller.chat_client.model

    @Property(bool, notify=ui_changed)
    def llmServerConnected(self) -> bool:  # noqa: N802
        return self._llm.server_connected

    @Property(bool, notify=ui_changed)
    def llmConnectionBusy(self) -> bool:  # noqa: N802
        return self._llm.connection_busy

    @Property(str, notify=ui_changed)
    def llmConnectionButtonText(self) -> str:  # noqa: N802
        if self._llm.connection_busy:
            return "Disconnecting..." if self._llm.server_connected else "Connecting..."
        return "Disconnect" if self._llm.server_connected else "Connect"

    @Property(bool, notify=ui_changed)
    def llmModelBusy(self) -> bool:  # noqa: N802
        return self._llm.model_busy

    @Property(bool, notify=ui_changed)
    def talkReady(self) -> bool:  # noqa: N802
        return bool(self.selectedSttModel and self.selectedTtsModel and self._llm.is_ready)

    @Property(str, notify=ui_changed)
    def micStatusLabel(self) -> str:  # noqa: N802
        # Priority table: first matching predicate wins. Blocking conditions
        # come first (ordered most-likely-to-resolve-first), then pipeline
        # state. To add a new state, insert a (predicate, label) tuple at the
        # appropriate priority — no need to walk an if/elif ladder.
        rules: list[tuple[Callable[[], bool], str]] = [
            (lambda: self.model_loader.is_loading, "Loading speech model…"),
            (lambda: self.tts_loader.is_loading, "Loading voice…"),
            (lambda: not self.selectedSttModel, "No speech model"),
            (lambda: not self.selectedTtsModel, "No voice model"),
            (lambda: not self.controller.chat_client.base_url, "No LLM URL"),
            (
                lambda: self._llm.connection_busy and not self._llm.server_connected,
                "Connecting…",
            ),
            (lambda: not self._llm.server_connected, "LLM disconnected"),
            (lambda: not self.controller.chat_client.model, "No model loaded"),
            # Ready to talk — reflect what the pipeline is doing.
            (lambda: not self.voiceConnectionEnabled, "Ready — tap to talk"),
            (lambda: self._state == AppState.RECORDING.value, "Listening…"),
            (lambda: self._state == AppState.TRANSCRIBING.value, "Transcribing…"),
            (lambda: self._state == AppState.THINKING.value, "Thinking…"),
            (lambda: self._state == AppState.SYNTHESIZING.value, "Generating voice…"),
            (lambda: self._state == AppState.SPEAKING.value, "Speaking…"),
        ]
        for predicate, label in rules:
            if predicate():
                return label
        return "Connected and listening"

    @Property(bool, notify=ui_changed)
    def voiceConnectionEnabled(self) -> bool:  # noqa: N802
        return self.controller.voice_connection_enabled

    @Property(str, notify=ui_changed)
    def voiceConnectionLabel(self) -> str:  # noqa: N802
        return "Voice Connection On" if self.controller.voice_connection_enabled else "Voice Connection Off"

    @Property(bool, notify=ui_changed)
    def audioMuted(self) -> bool:  # noqa: N802
        return self.settings.value("audio_output_muted", False, bool)

    @Property(str, notify=ui_changed)
    def themeMode(self) -> str:  # noqa: N802
        stored = self.settings.value("theme_mode", "auto", str) or "auto"
        normalized = stored.strip().lower()
        return normalized if normalized in {"auto", "light", "dark"} else "auto"

    @Property(str, notify=ui_changed)
    def themeModeLabel(self) -> str:  # noqa: N802
        return {"auto": "Auto", "light": "Light", "dark": "Dark"}.get(self.themeMode, "Auto")

    @Property(QObject, constant=True)
    def conversationModel(self) -> ConversationModel:  # noqa: N802
        return self._conversation_model

    @Property(int, notify=conversation_changed)
    def conversationMessageCount(self) -> int:  # noqa: N802
        return self._conversation_model.rowCount()

    @Property(str, notify=ui_changed)
    def errorMessage(self) -> str:  # noqa: N802
        return self._error_message

    @Property(str, notify=ui_changed)
    def statusMessage(self) -> str:  # noqa: N802
        return self._status_message

    @Property(str, notify=ui_changed)
    def state(self) -> str:
        return self._state

    @Slot(str)
    def selectSttModel(self, model_name: str) -> None:  # noqa: N802
        if model_name not in self.sttOptions:
            return
        self.settings.setValue("selected_stt_model", model_name)
        self.model_loader.select_model(model_name)
        self.ui_changed.emit()

    @Slot(str)
    def selectTtsModel(self, model_name: str) -> None:  # noqa: N802
        if model_name not in self.ttsOptions:
            return
        self.settings.setValue("selected_tts_model", model_name)
        self.tts_loader.select_model(model_name)
        self.ui_changed.emit()

    @Slot(str)
    def installSttModel(self, model_name: str) -> None:  # noqa: N802
        self.model_loader.download_model(model_name)

    @Slot(str)
    def deleteSttModel(self, model_name: str) -> None:  # noqa: N802
        self.model_loader.delete_model(model_name)

    @Slot(str)
    def installTtsModel(self, model_name: str) -> None:  # noqa: N802
        self.tts_loader.download_voice(model_name)

    @Slot(str)
    def deleteTtsModel(self, model_name: str) -> None:  # noqa: N802
        self.tts_loader.delete_voice(model_name)

    @Slot(str)
    def setCurrentLlmUrl(self, value: str) -> None:  # noqa: N802
        self._llm.set_current_url(value)

    @Slot()
    def persistCurrentLlmUrl(self) -> None:  # noqa: N802
        self._llm.persist_current_url()

    @Slot(bool)
    def refreshLlmModels(self, show_error: bool) -> None:  # noqa: N802
        self._llm.refresh_models(show_error)

    @Slot(str)
    def selectLlmModel(self, model_name: str) -> None:  # noqa: N802
        self._llm.select_model(model_name)

    @Slot(str)
    def toggleLlmServerConnection(self, value: str) -> None:  # noqa: N802
        if self._llm.connection_busy and self._llm.server_connected:
            return
        if self._llm.server_connected:
            self.disconnectLlmServer()
            return
        self.connectLlmServer(value, True)

    @Slot(str, bool)
    def connectLlmServer(self, value: str, show_error: bool = True) -> None:  # noqa: N802
        self._llm.connect_server(value, show_error)

    @Slot()
    def disconnectLlmServer(self) -> None:  # noqa: N802
        if self._llm.connection_busy or self._llm.model_busy:
            return
        if self.voiceConnectionEnabled:
            self.controller.stop_recording()
        self._llm.disconnect_server()

    @Slot()
    def autoconnectLlmServer(self) -> None:
        self._llm.autoconnect_server()

    @Slot(bool)
    def setVoiceConnectionEnabled(self, enabled: bool) -> None:  # noqa: N802
        if enabled:
            self.persistCurrentLlmUrl()
            self.controller.start_recording()
            return
        self.controller.stop_recording()

    @Slot(bool)
    def setAudioMuted(self, enabled: bool) -> None:  # noqa: N802
        self.settings.setValue("audio_output_muted", enabled)
        self._apply_audio_mute_state(enabled)

    @Slot(str)
    def setThemeMode(self, mode: str) -> None:  # noqa: N802
        normalized = mode.strip().lower()
        if normalized not in {"auto", "light", "dark"}:
            normalized = "auto"
        if normalized == self.themeMode:
            return
        self.settings.setValue("theme_mode", normalized)
        self._apply_theme_mode(normalized)
        self.ui_changed.emit()

    @Slot(int)
    def replayMessage(self, index: int) -> None:  # noqa: N802
        message = self._conversation_model.message(index)
        if message is None:
            return
        if message.get("role") != "assistant":
            return
        text = str(message.get("text", "")).strip()
        if not text or not self.tts_loader.tts_service.enabled:
            return
        audio_path = self.tts_loader.tts_service.synthesize(text)
        if audio_path is not None:
            self.replay_player.play_file(audio_path)

    def _handle_inventory_change(self) -> None:
        self._sync_installed_selections()
        self.ui_changed.emit()

    def _sync_installed_selections(self) -> None:
        selected_stt = self.model_loader.selected_model
        installed_stt = self.sttOptions
        if selected_stt not in installed_stt:
            fallback_stt = self._preferred_selection(installed_stt, self.settings.value("selected_stt_model", "", str) or "")
            if fallback_stt:
                self.model_loader.select_model(fallback_stt)

        selected_tts = self.tts_loader.selected_model or ""
        installed_tts = self.ttsOptions
        if selected_tts not in installed_tts:
            fallback_tts = self._preferred_selection(installed_tts, self.settings.value("selected_tts_model", "", str) or "")
            self.tts_loader.select_model(fallback_tts or None)

    def _preferred_selection(self, installed_items: list[str], persisted_item: str) -> str:
        if persisted_item and persisted_item in installed_items:
            return persisted_item
        return installed_items[0] if installed_items else ""

    def _restore_initial_selections(self) -> None:
        self._sync_installed_selections()
        if self.selectedSttModel:
            self.settings.setValue("selected_stt_model", self.selectedSttModel)
        if self.selectedTtsModel:
            self.settings.setValue("selected_tts_model", self.selectedTtsModel)

    def _append_user_message(self, text: str) -> None:
        cleaned = text.strip()
        if not cleaned:
            return
        pending_index = self._find_message_index(role="user", turn_pending=True)
        if pending_index >= 0:
            self._conversation_model.update_message(
                pending_index,
                text=cleaned,
                bubbleState="sent",
                turnPending=False,
                timestampLabel=f"Sent {self._clock_time()}",
            )
        else:
            self._conversation_model.append_message(
                {
                    "role": "user",
                    "text": cleaned,
                    "replayable": False,
                    "bubbleState": "sent",
                    "turnPending": False,
                    "timestampLabel": f"Sent {self._clock_time()}",
                }
            )
        self.conversation_changed.emit()

    def _append_assistant_message(self, text: str) -> None:
        cleaned = text.strip()
        if not cleaned:
            return
        self._conversation_model.append_message(
            {
                "role": "assistant",
                "text": cleaned,
                "replayable": True,
                "bubbleState": "sent",
                "turnPending": False,
                "timestampLabel": f"Received {self._clock_time()}",
            }
        )
        self.conversation_changed.emit()

    def _append_log_message(self, text: str, level: str) -> None:
        cleaned = text.strip()
        if not cleaned:
            return
        self._conversation_model.append_message(
            {
                "role": "system",
                "level": level,
                "text": cleaned,
                "replayable": False,
                "bubbleState": "plain",
                "turnPending": False,
                "timestampLabel": self._clock_time(),
            }
        )
        self.conversation_changed.emit()

    def _apply_audio_mute_state(self, enabled: bool) -> None:
        self.controller.player.set_muted(enabled)
        self.replay_player.set_muted(enabled)
        self.ui_changed.emit()

    def _apply_theme_mode(self, mode: str) -> None:
        app = QApplication.instance()
        if app is None:
            return
        style_hints = app.styleHints()
        scheme = {
            "auto": Qt.ColorScheme.Unknown,
            "light": Qt.ColorScheme.Light,
            "dark": Qt.ColorScheme.Dark,
        }.get(mode, Qt.ColorScheme.Unknown)
        if hasattr(style_hints, "setColorScheme"):
            style_hints.setColorScheme(scheme)

    def _apply_model_status(self, status: str) -> None:
        if self.model_loader.is_loading:
            self._status_message = status
            self._append_log_message(status, "status")
        self.ui_changed.emit()

    def _apply_model_progress(self, progress) -> None:
        self._model_progress_value, self._model_progress_indeterminate, self._model_progress_text = self._format_progress(
            progress
        )
        self.progress_changed.emit()

    def _apply_tts_status(self, status: str) -> None:
        if self.tts_loader.is_loading:
            self._status_message = status
            self._append_log_message(status, "status")
        self.ui_changed.emit()

    def _apply_tts_progress(self, progress) -> None:
        self._tts_progress_value, self._tts_progress_indeterminate, self._tts_progress_text = self._format_progress(
            progress
        )
        self.progress_changed.emit()

    def _on_stt_item_loading_changed(self, model_name: str, is_loading: bool) -> None:
        if is_loading:
            self._stt_active_items.add(model_name)
        else:
            self._stt_active_items.discard(model_name)
            self._stt_progress_by_item.pop(model_name, None)
            # File may have just appeared/disappeared on disk; update only this row
            # instead of invalidating the whole catalog.
            self._stt_catalog_model.refresh_installed(model_name)
        self.downloads_changed.emit()
        self.downloads_progress_changed.emit()

    def _on_stt_item_progress_changed(self, model_name: str, progress) -> None:
        total = getattr(progress, "total_bytes", 0) or 0
        completed = getattr(progress, "completed_bytes", 0) or 0
        fraction = (completed / total) if total > 0 else 0.0
        self._stt_progress_by_item[model_name] = max(0.0, min(1.0, fraction))
        self.downloads_progress_changed.emit()

    def _on_tts_item_loading_changed(self, model_name: str, is_loading: bool) -> None:
        if is_loading:
            self._tts_active_items.add(model_name)
        else:
            self._tts_active_items.discard(model_name)
            self._tts_progress_by_item.pop(model_name, None)
            self._tts_catalog_model.refresh_installed(model_name)
        self.downloads_changed.emit()
        self.downloads_progress_changed.emit()

    def _on_tts_item_progress_changed(self, model_name: str, progress) -> None:
        total = getattr(progress, "total_bytes", 0) or 0
        completed = getattr(progress, "completed_bytes", 0) or 0
        fraction = (completed / total) if total > 0 else 0.0
        self._tts_progress_by_item[model_name] = max(0.0, min(1.0, fraction))
        self.downloads_progress_changed.emit()

    def _apply_state(self, state: str) -> None:
        self._state = state
        if state in {
            AppState.TRANSCRIBING.value,
            AppState.THINKING.value,
            AppState.SYNTHESIZING.value,
            AppState.SPEAKING.value,
        }:
            self._promote_live_user_message()
        self.ui_changed.emit()

    def _set_status_message(self, message: str) -> None:
        self._status_message = message
        self._append_log_message(message, "status")
        self.ui_changed.emit()

    def _set_error_message(self, message: str) -> None:
        self._error_message = message
        if message:
            self._discard_draft_user_message()
            self._append_log_message(message, "error")
        self.ui_changed.emit()

    def _handle_connection_changed(self, enabled: bool) -> None:
        if not enabled:
            self._discard_draft_user_message()
        self.ui_changed.emit()

    def _emit_ui_changed(self, *_args) -> None:
        self.ui_changed.emit()

    def _is_stt_downloaded(self, model_name: str) -> bool:
        return self.model_loader.transcriber.is_item_available(model_name)

    def _is_tts_downloaded(self, model_name: str) -> bool:
        return self.tts_loader.tts_service.is_item_available(model_name)

    def _format_progress(self, progress) -> tuple[float, bool, str]:
        current = progress.completed_bytes
        total = progress.total_bytes
        speed = progress.download_speed_bytes_per_second
        if total > 0:
            detail = f"{(current / total) * 100:.1f}% ({format_bytes(current)} / {format_bytes(total)})"
            if speed > 0:
                detail += f" at {format_transfer_rate(speed)}"
            return min(current / total, 1.0), False, detail
        return 0.0, True, "Waiting for aria2 download telemetry"

    def _on_llm_urls_changed(self, _urls: list[str]) -> None:
        self.ui_changed.emit()

    def _on_llm_current_url_changed(self, _url: str) -> None:
        self.ui_changed.emit()

    def _on_llm_connection_state_changed(self, _connected: bool) -> None:
        self.ui_changed.emit()

    def _on_llm_busy_changed(self, _busy: bool) -> None:
        self.ui_changed.emit()

    def _on_llm_models_changed(self, _models: list[str], _loaded_model: str) -> None:
        self.ui_changed.emit()

    def _on_llm_selected_model_changed(self, _model: str) -> None:
        self.ui_changed.emit()

    def _on_llm_status_message(self, message: str) -> None:
        if not message:
            return
        self._status_message = message
        self._append_log_message(message, "status")
        self.ui_changed.emit()

    def _on_llm_error(self, title: str, message: str) -> None:
        if message:
            self._set_error_message(f"{title}: {message}")
            return
        if title:
            self._append_log_message(title, "error")
        self.ui_changed.emit()

    def _sync_live_user_message(self, text: str) -> None:
        cleaned = text.strip()
        draft_index = self._find_message_index(role="user", bubble_state="draft")
        if not cleaned:
            draft_message = self._conversation_model.message(draft_index)
            if draft_message is not None and not bool(draft_message.get("turnPending")):
                self._conversation_model.remove_message(draft_index)
                self.conversation_changed.emit()
            return
        if draft_index >= 0:
            self._conversation_model.update_message(draft_index, text=cleaned)
        else:
            self._conversation_model.append_message(
                {
                    "role": "user",
                    "text": cleaned,
                    "replayable": False,
                    "bubbleState": "draft",
                    "turnPending": True,
                    "timestampLabel": "",
                }
            )
        self.conversation_changed.emit()

    def _promote_live_user_message(self) -> None:
        draft_index = self._find_message_index(role="user", bubble_state="draft")
        if draft_index < 0:
            return
        self._conversation_model.update_message(draft_index, bubbleState="sent", turnPending=True)
        self.conversation_changed.emit()

    def _find_message_index(self, role: str, bubble_state: str | None = None, turn_pending: bool | None = None) -> int:
        return self._conversation_model.find_message_index(role, bubble_state, turn_pending)

    def _discard_draft_user_message(self) -> None:
        draft_index = self._find_message_index(role="user", bubble_state="draft")
        if draft_index < 0:
            return
        self._conversation_model.remove_message(draft_index)
        self.conversation_changed.emit()

    def _clock_time(self) -> str:
        return datetime.now().strftime("%H:%M:%S")
