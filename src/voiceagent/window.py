from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime
import logging
from pathlib import Path

from PySide6.QtCore import Property, QSettings, Qt, QTimer, QUrl, QObject, Signal, Slot
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtWidgets import QApplication

from voiceagent.controller import VoiceController
from voiceagent.downloaders import format_bytes, format_transfer_rate
from voiceagent.model_loader import WhisperModelLoader
from voiceagent.models import AppState
from voiceagent.services.chat import LmStudioClient
from voiceagent.services.playback import AudioPlayer
from voiceagent.tts_loader import TtsVoiceLoader


class MainWindow(QObject):
    ui_changed = Signal()
    conversation_changed = Signal()
    _llm_operation_finished = Signal(str, object)

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
        self.settings.remove("current_llm_model")
        self.settings.remove("llm_model_history")
        self.replay_player = AudioPlayer(self)
        self._logger = logging.getLogger(__name__)
        self._default_llm_url = "silverthread:1234"
        self._stt_catalog = self.model_loader.transcriber.available_items()
        self._tts_catalog = self.tts_loader.tts_service.available_items()
        self._llm_models: list[str] = []
        self._conversation_messages: list[dict[str, object]] = []
        self._error_message = ""
        self._status_message = "Ready"
        self._llm_server_connected = False
        self._llm_connection_busy = False
        self._llm_model_busy = False
        self._llm_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="voiceagent-llm")
        self._llm_refresh_request_id = 0
        self._llm_active_refresh_request_id = 0
        self._startup_llm_connect_scheduled = False
        self._shutting_down = False
        self._state = "idle"
        self._model_progress_value = 0.0
        self._model_progress_indeterminate = False
        self._model_progress_text = ""
        self._tts_progress_value = 0.0
        self._tts_progress_indeterminate = False
        self._tts_progress_text = ""

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
        self.model_loader.error_changed.connect(self._set_error_message)
        self.model_loader.selection_changed.connect(self._emit_ui_changed)
        self.model_loader.load_completed.connect(self._handle_inventory_change)
        self.model_loader.delete_completed.connect(self._handle_inventory_change)
        self.tts_loader.ready_changed.connect(self._emit_ui_changed)
        self.tts_loader.loading_changed.connect(self._emit_ui_changed)
        self.tts_loader.status_changed.connect(self._apply_tts_status)
        self.tts_loader.progress_changed.connect(self._apply_tts_progress)
        self.tts_loader.error_changed.connect(self._set_error_message)
        self.tts_loader.selection_changed.connect(self._emit_ui_changed)
        self.tts_loader.load_completed.connect(self._handle_inventory_change)
        self.tts_loader.delete_completed.connect(self._handle_inventory_change)
        self._llm_operation_finished.connect(self._handle_llm_operation_finished)

        self._populate_llm_urls()
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
        if not self._startup_llm_connect_scheduled and self.currentLlmUrl:
            self._startup_llm_connect_scheduled = True
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
        self._llm_executor.shutdown(wait=False, cancel_futures=True)
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

    @Property(float, notify=ui_changed)
    def modelProgressValue(self) -> float:  # noqa: N802
        return self._model_progress_value

    @Property(bool, notify=ui_changed)
    def modelProgressIndeterminate(self) -> bool:  # noqa: N802
        return self._model_progress_indeterminate

    @Property(str, notify=ui_changed)
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

    @Property(float, notify=ui_changed)
    def ttsProgressValue(self) -> float:  # noqa: N802
        return self._tts_progress_value

    @Property(bool, notify=ui_changed)
    def ttsProgressIndeterminate(self) -> bool:  # noqa: N802
        return self._tts_progress_indeterminate

    @Property(str, notify=ui_changed)
    def ttsProgressText(self) -> str:  # noqa: N802
        return self._tts_progress_text

    @Property("QVariantList", notify=ui_changed)
    def llmUrls(self) -> list[str]:  # noqa: N802
        history = self.settings.value("llm_url_history", [], list) or []
        entries = [entry for entry in history if isinstance(entry, str) and entry.strip()]
        current_base_url = self.controller.chat_client.base_url
        if current_base_url:
            entries.insert(0, current_base_url)
        entries.insert(0, self._default_llm_url)
        unique_entries: list[str] = []
        for entry in entries:
            normalized = entry.strip()
            if normalized and normalized not in unique_entries:
                unique_entries.append(normalized)
        return unique_entries

    @Property(str, notify=ui_changed)
    def currentLlmUrl(self) -> str:  # noqa: N802
        return self._initial_llm_url() if not self.controller.chat_client.base_url else self.controller.chat_client.base_url

    @Property("QVariantList", notify=ui_changed)
    def llmModelOptions(self) -> list[str]:  # noqa: N802
        return ["", *self._llm_models]

    @Property(str, notify=ui_changed)
    def selectedLlmModel(self) -> str:  # noqa: N802
        return self.controller.chat_client.model

    @Property(bool, notify=ui_changed)
    def llmServerConnected(self) -> bool:  # noqa: N802
        return self._llm_server_connected

    @Property(bool, notify=ui_changed)
    def llmConnectionBusy(self) -> bool:  # noqa: N802
        return self._llm_connection_busy

    @Property(str, notify=ui_changed)
    def llmConnectionButtonText(self) -> str:  # noqa: N802
        if self._llm_connection_busy:
            return "Disconnecting..." if self._llm_server_connected else "Connecting..."
        return "Disconnect" if self._llm_server_connected else "Connect"

    @Property(bool, notify=ui_changed)
    def llmModelBusy(self) -> bool:  # noqa: N802
        return self._llm_model_busy

    @Property(bool, notify=ui_changed)
    def talkReady(self) -> bool:  # noqa: N802
        return bool(self.selectedSttModel and self.selectedTtsModel and self._llm_ready())

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

    @Property("QVariantList", notify=conversation_changed)
    def conversationMessages(self) -> list[dict[str, object]]:  # noqa: N802
        return list(self._conversation_messages)

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
        normalized_value = self.controller.chat_client.normalize_base_url(value)
        if normalized_value == self.controller.chat_client.base_url:
            return
        self.controller.chat_client.set_base_url(value)
        self.controller.chat_client.set_model("")
        self._llm_models = []
        self._llm_server_connected = False
        self.ui_changed.emit()

    @Slot()
    def persistCurrentLlmUrl(self) -> None:  # noqa: N802
        value = self.controller.chat_client.base_url.strip()
        if not value:
            return
        self.settings.setValue("current_llm_url", value)
        history = self.settings.value("llm_url_history", [], list) or []
        entries = [entry for entry in history if isinstance(entry, str) and entry.strip()]
        updated_entries = [value, *[entry for entry in entries if entry != value]]
        self.settings.setValue("llm_url_history", updated_entries[:10])
        self.ui_changed.emit()

    @Slot(bool)
    def refreshLlmModels(self, show_error: bool) -> None:  # noqa: N802
        self._start_llm_refresh(show_error=show_error)

    @Slot(str)
    def selectLlmModel(self, model_name: str) -> None:  # noqa: N802
        if not self._llm_server_connected:
            self._append_log_message("Connect to the LLM server before selecting a model.", "error")
            return
        if self._llm_connection_busy or self._llm_model_busy:
            return
        selected_model = model_name.strip()
        self._llm_model_busy = True
        self.ui_changed.emit()
        if not selected_model:
            self._append_log_message("Unloading the active LLM model...", "status")
            self._submit_llm_operation("select_model", self._unload_llm_model_task)
            return
        self._append_log_message(f"Loading LLM model {selected_model}...", "status")
        self._submit_llm_operation("select_model", lambda: self._load_llm_model_task(selected_model))

    @Slot(str)
    def toggleLlmServerConnection(self, value: str) -> None:  # noqa: N802
        if self._llm_connection_busy and self._llm_server_connected:
            return
        if self._llm_server_connected:
            self.disconnectLlmServer()
            return
        self.connectLlmServer(value, True)

    @Slot(str, bool)
    def connectLlmServer(self, value: str, show_error: bool = True) -> None:  # noqa: N802
        if self._llm_connection_busy and self._llm_server_connected:
            return
        if value.strip():
            self.setCurrentLlmUrl(value)
        self._start_llm_refresh(show_error=show_error)

    @Slot()
    def disconnectLlmServer(self) -> None:  # noqa: N802
        if self._llm_connection_busy or self._llm_model_busy:
            return
        if self.voiceConnectionEnabled:
            self.controller.stop_recording()
        self._llm_connection_busy = True
        self.ui_changed.emit()
        self._submit_llm_operation("disconnect", self._disconnect_llm_task)

    @Slot()
    def autoconnectLlmServer(self) -> None:
        self._start_llm_refresh(show_error=False)

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
        if index < 0 or index >= len(self._conversation_messages):
            return
        message = self._conversation_messages[index]
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
            self._conversation_messages[pending_index]["text"] = cleaned
            self._conversation_messages[pending_index]["bubbleState"] = "sent"
            self._conversation_messages[pending_index]["turnPending"] = False
            self._conversation_messages[pending_index]["timestampLabel"] = f"Sent {self._clock_time()}"
        else:
            self._conversation_messages.append(
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
        thinking_index = self._find_message_index(role="assistant", bubble_state="thinking")
        if thinking_index >= 0:
            self._conversation_messages[thinking_index]["text"] = cleaned
            self._conversation_messages[thinking_index]["bubbleState"] = "sent"
            self._conversation_messages[thinking_index]["replayable"] = True
            self._conversation_messages[thinking_index]["turnPending"] = False
            self._conversation_messages[thinking_index]["timestampLabel"] = f"Received {self._clock_time()}"
        else:
            self._conversation_messages.append(
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
        self._conversation_messages.append(
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
        self.ui_changed.emit()

    def _apply_tts_status(self, status: str) -> None:
        if self.tts_loader.is_loading:
            self._status_message = status
            self._append_log_message(status, "status")
        self.ui_changed.emit()

    def _apply_tts_progress(self, progress) -> None:
        self._tts_progress_value, self._tts_progress_indeterminate, self._tts_progress_text = self._format_progress(
            progress
        )
        self.ui_changed.emit()

    def _apply_state(self, state: str) -> None:
        self._state = state
        if state in {
            AppState.TRANSCRIBING.value,
            AppState.THINKING.value,
            AppState.SYNTHESIZING.value,
            AppState.SPEAKING.value,
        }:
            self._promote_live_user_message()
        if state == AppState.THINKING.value:
            self._ensure_assistant_thinking_message()
        elif state in {AppState.RECORDING.value, AppState.IDLE.value}:
            self._discard_assistant_thinking_message()
        self.ui_changed.emit()

    def _set_status_message(self, message: str) -> None:
        self._status_message = message
        self._append_log_message(message, "status")
        self.ui_changed.emit()

    def _set_error_message(self, message: str) -> None:
        self._error_message = message
        if message:
            self._discard_assistant_thinking_message()
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

    def _populate_llm_urls(self) -> None:
        self.controller.chat_client.set_base_url(self._initial_llm_url())
        self.ui_changed.emit()

    def _populate_llm_model_selector(self, models: list[str], loaded_model: str) -> None:
        unique_models: list[str] = []
        for model in models:
            normalized = model.strip()
            if normalized and normalized not in unique_models:
                unique_models.append(normalized)
        if loaded_model and loaded_model not in unique_models:
            unique_models.insert(0, loaded_model)
        self._llm_models = unique_models
        self.controller.chat_client.set_model(loaded_model)
        self.ui_changed.emit()

    def _initial_llm_url(self) -> str:
        stored = self.settings.value("current_llm_url", "", str) or ""
        if stored:
            return stored
        return self._default_llm_url

    def _llm_ready(self) -> bool:
        return bool(self.controller.chat_client.base_url and self.controller.chat_client.model)

    def _show_llm_error(self, title: str, message: str) -> None:
        self._set_error_message(f"{title}: {message}")

    def _start_llm_refresh(self, show_error: bool) -> None:
        if self._llm_connection_busy and self._llm_server_connected:
            return
        self._llm_refresh_request_id += 1
        request_id = self._llm_refresh_request_id
        self._llm_active_refresh_request_id = request_id
        self.persistCurrentLlmUrl()
        self._llm_connection_busy = True
        self.ui_changed.emit()
        self._submit_llm_operation(
            "refresh",
            lambda: self._refresh_llm_models_task(request_id=request_id, show_error=show_error),
        )

    def _submit_llm_operation(self, operation: str, task) -> None:
        future = self._llm_executor.submit(task)
        future.add_done_callback(lambda completed: self._emit_llm_operation_result(operation, completed))

    def _emit_llm_operation_result(self, operation: str, future: Future[object]) -> None:
        try:
            payload = future.result()
        except Exception as exc:  # pragma: no cover - defensive bridge
            payload = {"ok": False, "error": str(exc)}
        self._llm_operation_finished.emit(operation, payload)

    @Slot(str, object)
    def _handle_llm_operation_finished(self, operation: str, payload: object) -> None:
        result = payload if isinstance(payload, dict) else {"ok": False, "error": "Unexpected LLM operation result."}
        ok = bool(result.get("ok"))
        if operation == "select_model":
            self._llm_model_busy = False
            loaded_model = str(result.get("loaded_model", "")).strip() if "loaded_model" in result else None
            if not ok:
                if loaded_model is not None:
                    self._populate_llm_model_selector(self._llm_models, loaded_model)
                self._show_llm_error("Unable to update LLM model", str(result.get("error", "")))
                self.ui_changed.emit()
                return
            loaded_model = loaded_model or ""
            self._populate_llm_model_selector(self._llm_models, loaded_model)
            if loaded_model:
                self._status_message = f"Loaded LLM model {loaded_model}."
            else:
                self._status_message = "No LLM model loaded."
            self._append_log_message(self._status_message, "status")
            self.ui_changed.emit()
            return

        self._llm_connection_busy = False
        if operation == "refresh":
            request_id = int(result.get("request_id", 0) or 0)
            if request_id and request_id != self._llm_active_refresh_request_id:
                return
        if operation == "disconnect":
            if ok:
                self._llm_server_connected = False
                self._llm_models = []
                self.controller.chat_client.set_model("")
                self._status_message = "Disconnected from LLM server."
                self._append_log_message(self._status_message, "status")
            else:
                self._show_llm_error("Unable to disconnect from LLM server", str(result.get("error", "")))
            self.ui_changed.emit()
            return

        if not ok:
            self._llm_server_connected = False
            self._llm_models = []
            self.controller.chat_client.set_model("")
            failure_message = f"Unable to connect to LLM server: {str(result.get('error', ''))}".strip()
            if bool(result.get("show_error", True)):
                self._show_llm_error("Unable to connect to LLM server", str(result.get("error", "")))
            elif failure_message:
                self._append_log_message(failure_message, "error")
            self.ui_changed.emit()
            return

        models = result.get("models", [])
        loaded_model = str(result.get("loaded_model", "")).strip()
        previous_models = list(self._llm_models)
        previous_loaded_model = self.controller.chat_client.model
        self._llm_server_connected = True
        self._populate_llm_model_selector(list(models) if isinstance(models, list) else [], loaded_model)
        self._append_log_message(f"Connected to LLM server at {self.currentLlmUrl}.", "status")

        added_models = [model for model in self._llm_models if model not in previous_models]
        removed_models = [model for model in previous_models if model not in self._llm_models]
        if added_models or removed_models:
            parts: list[str] = []
            if added_models:
                parts.append(f"added {len(added_models)}")
            if removed_models:
                parts.append(f"removed {len(removed_models)}")
            self._status_message = f"LLM models refreshed: {', '.join(parts)}."
        elif loaded_model and loaded_model != previous_loaded_model:
            self._status_message = f"LLM models refreshed. Loaded model is now {loaded_model}."
        elif self._llm_models:
            self._status_message = f"LLM models refreshed. {len(self._llm_models)} model(s) available."
        else:
            self._status_message = "LLM models refreshed. No models loaded."
        self._append_log_message(self._status_message, "status")
        self.ui_changed.emit()

    def _refresh_llm_models_task(self, request_id: int, show_error: bool) -> dict[str, object]:
        base_url = self.controller.chat_client.base_url
        snapshot_client = LmStudioClient(
            base_url=base_url,
            model=self.controller.chat_client.model,
            system_prompt=self.controller.chat_client.system_prompt,
            timeout_seconds=self.controller.chat_client.timeout_seconds,
        )
        try:
            models = snapshot_client.list_models()
            try:
                loaded_models = snapshot_client.list_loaded_models()
            except RuntimeError:
                loaded_models = []
            return {
                "ok": True,
                "models": models,
                "loaded_model": loaded_models[0] if loaded_models else "",
                "request_id": request_id,
                "show_error": show_error,
            }
        except RuntimeError as exc:
            return {"ok": False, "error": str(exc), "request_id": request_id, "show_error": show_error}

    def _disconnect_llm_task(self) -> dict[str, object]:
        return {"ok": True}

    def _load_llm_model_task(self, model_name: str) -> dict[str, object]:
        try:
            loaded_model = self.controller.chat_client.load_model(model_name)
        except RuntimeError as exc:
            try:
                loaded_models = self.controller.chat_client.list_loaded_models()
            except RuntimeError:
                loaded_models = []
            return {"ok": False, "error": str(exc), "loaded_model": loaded_models[0] if loaded_models else ""}
        return {"ok": True, "loaded_model": loaded_model}

    def _unload_llm_model_task(self) -> dict[str, object]:
        try:
            self.controller.chat_client.unload_all_models()
        except RuntimeError as exc:
            try:
                loaded_models = self.controller.chat_client.list_loaded_models()
            except RuntimeError:
                loaded_models = []
            result: dict[str, object] = {"ok": False, "error": str(exc)}
            if loaded_models:
                result["loaded_model"] = loaded_models[0]
            return result
        self.controller.chat_client.set_model("")
        return {"ok": True, "loaded_model": ""}

    def _sync_live_user_message(self, text: str) -> None:
        cleaned = text.strip()
        draft_index = self._find_message_index(role="user", bubble_state="draft")
        if not cleaned:
            if draft_index >= 0 and not bool(self._conversation_messages[draft_index].get("turnPending")):
                self._conversation_messages.pop(draft_index)
                self.conversation_changed.emit()
            return
        if draft_index >= 0:
            self._conversation_messages[draft_index]["text"] = cleaned
        else:
            self._conversation_messages.append(
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
        self._conversation_messages[draft_index]["bubbleState"] = "sent"
        self._conversation_messages[draft_index]["turnPending"] = True
        self.conversation_changed.emit()

    def _ensure_assistant_thinking_message(self) -> None:
        if self._find_message_index(role="assistant", bubble_state="thinking") >= 0:
            return
        self._conversation_messages.append(
            {
                "role": "assistant",
                "text": "Thinking...",
                "replayable": False,
                "bubbleState": "thinking",
                "turnPending": True,
                "timestampLabel": "",
            }
        )
        self.conversation_changed.emit()

    def _discard_assistant_thinking_message(self) -> None:
        thinking_index = self._find_message_index(role="assistant", bubble_state="thinking")
        if thinking_index < 0:
            return
        self._conversation_messages.pop(thinking_index)
        self.conversation_changed.emit()

    def _find_message_index(self, role: str, bubble_state: str | None = None, turn_pending: bool | None = None) -> int:
        for index in range(len(self._conversation_messages) - 1, -1, -1):
            message = self._conversation_messages[index]
            if message.get("role") != role:
                continue
            if bubble_state is not None and message.get("bubbleState") != bubble_state:
                continue
            if turn_pending is not None and bool(message.get("turnPending")) != turn_pending:
                continue
            return index
        return -1

    def _discard_draft_user_message(self) -> None:
        draft_index = self._find_message_index(role="user", bubble_state="draft")
        if draft_index < 0:
            return
        self._conversation_messages.pop(draft_index)
        self.conversation_changed.emit()

    def _clock_time(self) -> str:
        return datetime.now().strftime("%H:%M:%S")
