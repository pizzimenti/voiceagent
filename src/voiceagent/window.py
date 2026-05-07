from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime
import logging
from pathlib import Path
import threading
import time
from collections.abc import Callable

from PySide6.QtCore import (
    Property,
    QSettings,
    Qt,
    QUrl,
    QObject,
    Signal,
    Slot,
)
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtWidgets import QApplication

from voiceagent.catalog_model import CatalogModel
from voiceagent.config import AppConfig
from voiceagent.controller import VoiceController
from voiceagent.conversation_model import ConversationModel
from voiceagent.conversation_turn_coordinator import ConversationTurnCoordinator
from voiceagent.downloaders import format_bytes, format_transfer_rate
from voiceagent.i18n import TranslatorContext
from voiceagent.logging_utils import CONVERSATION_LOGGER_NAME, log_ui_timing
from voiceagent.model_loader import WhisperModelLoader
from voiceagent.models import AppState
from voiceagent.services.llm_controller import LlmController
from voiceagent.services.playback import AudioPlayer
from voiceagent.startup_deferral import schedule_after_first_frame
from voiceagent.tts_loader import TtsVoiceLoader

_TTS_ENGINE_OPTIONS: tuple[str, ...] = ("piper", "chatterbox")


class _CatalogStateAdapter:
    """Pulls per-row state for CatalogModel from the loader + backend.

    Plain Python class (not a QObject) — lifetime is owned by MainWindow,
    not the CatalogModel.
    """

    def __init__(self, *, loader, backend) -> None:
        self._loader = loader
        self._backend = backend

    def is_installed(self, name: str) -> bool:
        return bool(self._backend.is_item_available(name))

    def is_loading(self, name: str) -> bool:
        return bool(self._loader.is_item_loading(name))

    def progress(self, name: str) -> float:
        snapshot = self._loader.progress_for(name)
        total = getattr(snapshot, "total_bytes", 0) or 0
        if total <= 0:
            return 0.0
        completed = getattr(snapshot, "completed_bytes", 0) or 0
        return max(0.0, min(1.0, completed / total))

    def is_downloadable(self, name: str) -> bool:
        return bool(self._backend.is_item_downloadable(name))

    def is_managed(self, name: str) -> bool:
        return bool(self._backend.is_item_managed(name))


class MainWindow(QObject):
    """QObject bridge between the QML root window and the Python backend.

    Owns the Q_PROPERTYs QML binds to (theme mode, model selectors,
    catalogs, conversation model, context-token counters, etc.) and
    the Slots QML invokes (model select / install
    / remove, theme cycle, replay, set-thinking-expanded). State
    mutations route through the conversation coordinator, the LLM
    controller, or the model loaders — this class is mostly a property
    surface and signal-relay. Cross-thread state writes (e.g. the
    context-length probe completing on a worker) marshal through
    private signals so the GUI thread stays the sole writer.
    """

    ui_changed = Signal()
    progress_changed = Signal()
    conversation_changed = Signal()
    # Internal worker-thread → main-thread bridge for the LM Studio
    # context-length fetch. Layer 5 / 6 design: when the selected LLM
    # model changes, hop a `fetch_loaded_context_length()` HTTP call off
    # to a background executor and post the integer result back here so
    # `_context_tokens_ceiling` is mutated only from the GUI thread. The
    # second arg carries the model name the fetch was issued for, so
    # late-arriving results for a stale selection can be ignored.
    _context_length_fetched = Signal(int, str)

    # Internal worker-thread → main-thread bridge for replay TTS
    # synthesis. Piper synth is a 5-7 s blocking call; running it on
    # the GUI thread froze the click handler, leaving the ▶/🤫
    # toggle unresponsive and queueing additional click events that
    # cascaded into stacked PortAudio workers and an eventual crash.
    # The first arg is the row index the synth was issued for; the
    # second is `(audio_path, error_message)` packed in a tuple so
    # both success and failure paths fit one signal shape.
    _replay_synth_completed = Signal(int, object)

    # Internal worker-thread → main-thread bridge for the Chatterbox
    # reference-voice recorder. `_chatterbox_record_progress` ticks
    # ~10 Hz with `(seconds_elapsed, seconds_total)`. `_chatterbox_record_finished`
    # fires once with `(saved_name, success, error_message)` — empty
    # `saved_name` + non-empty error means the record failed; non-empty
    # `saved_name` + empty error means success.
    _chatterbox_record_progress = Signal(float, float)
    _chatterbox_record_finished = Signal(str, bool, str)

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
        # Per-turn ordering policy lives in the coordinator. See
        # `conversation_turn_coordinator.py` for the rationale on why
        # the coordinator writes through the model directly instead of
        # emitting per-mutation signals.
        self._turn_coordinator = ConversationTurnCoordinator(
            self._conversation_model,
            # Live-callable so a direct `QSettings.setValue(...)` (used
            # by integration tests AND by the QML-bound
            # `setLogVerboseMode` slot via `self.settings`) takes
            # effect immediately, matching the pre-refactor behavior
            # where `_apply_state` and `_flush_pending_status_log_entries`
            # both read `self.logVerboseMode` fresh.
            verbose_mode=lambda: self.logVerboseMode,
            clock_time=self._clock_time,
            parent=self,
        )
        self._turn_coordinator.conversation_changed.connect(
            self.conversation_changed
        )
        # When the coordinator's history-cap trim drops rows from the
        # FRONT of the model, every row index above the dropped range
        # shifts by `count`. Our `_speaking_row` is one such index; if
        # we don't shift it in lockstep, the inline ▶/🤫 toggle
        # renders on the wrong row after the trim fires. The
        # coordinator already adjusts its own
        # `_streaming_assistant_index` the same way; this connection
        # extends that pattern to MainWindow's row tracker.
        # CodeRabbit round-3 P1.
        self._turn_coordinator.rows_dropped_from_front.connect(
            self._on_rows_dropped_from_front
        )
        self._llm = LlmController(self.controller.chat_client, self.settings, parent=self)
        self._shutting_down = False
        self._state = "idle"
        # Track the model we last reacted to so we can short-circuit
        # no-op `selected_model_changed` re-fires (LM Studio refresh,
        # reconnect, URL change). Without this guard the
        # context-tokens counters reset and a fresh
        # `fetch_loaded_context_length` HTTP probe fires on every
        # repeat, even when the loaded model genuinely hasn't changed.
        # Seeded from the chat client's current model so an
        # autoconnect that re-resolves the same name is silent.
        self._last_selected_llm_model: str = self.controller.chat_client.model
        # v0.11 multi-turn history. The coordinator owns the trim
        # invariant ("visible transcript == what the LLM sees on the
        # next call"); seed its cap from `AppConfig` via the
        # controller. Conversation deliberately persists across LLM
        # model swaps — modern instruction-tuned local models handle
        # foreign transcripts well, and the user gets continuity
        # rather than a surprise wipe when comparing models. The
        # context-token bar visually warns when the prompt approaches
        # the new model's loaded_context_length.
        self._turn_coordinator.set_max_history_turns(
            self.controller.max_history_turns
        )
        # Dedicated single-worker executor for replay TTS synthesis.
        # Sized 1 — a second concurrent synth would race the same
        # Piper invocation and produce stacked WAV temp files. The
        # `replayMessage` slot submits a job here, and the future's
        # done-callback bridges back to the GUI thread via
        # `_replay_synth_completed`.
        self._replay_executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="voiceagent-replay-synth"
        )
        self._replay_executor_shutdown = False
        # Tracks the in-flight replay synth future so a fresh
        # `replayMessage` click can cancel a still-pending prior synth
        # before queueing the new one. Without this, rapid spam-clicks
        # accumulate jobs in the single-worker executor's queue and
        # each one runs to completion just to be discarded by the
        # cancellation check inside `_on_replay_synth_completed`.
        # CodeRabbit round-3 P2.
        self._replay_synth_future: Future[object] | None = None
        # Monotonically increasing token bumped on each `replayMessage`
        # click and each `stopSpeaking`. Replay synth jobs carry their
        # spawning value through to `_on_replay_synth_completed`, which
        # discards any payload whose token != current. Decouples
        # cancellation from `_speaking_row` — the row index can shift
        # under the synth via `_on_rows_dropped_from_front` without
        # the completion check spuriously discarding because
        # "speaking_row != original index". CodeRabbit round-4 P2.
        self._replay_request_id: int = 0
        self._replay_synth_completed.connect(self._on_replay_synth_completed)
        # Row index of the assistant bubble whose TTS audio is currently
        # being read aloud. -1 when nothing is playing. Drives the
        # per-bubble ▶/🤫 toggle in the conversation pane: only the
        # actively-speaking row shows 🤫 (which calls `stopSpeaking()`
        # to cut the speech without touching the voice connection).
        # Set when playback_started fires on either player; reset on
        # finished / failed / explicit stop.
        self._speaking_row: int = -1
        # Which player currently OWNS the speaking row. Values:
        # `"controller"` (in-pipeline auto-play), `"replay"`
        # (user-triggered replay), or `""` (nothing playing).
        # Without this, a late `playback_finished` from the
        # `controller.player` (e.g. an in-pipeline auto-play that
        # was abandoned mid-stream) could wipe a `_speaking_row`
        # that belongs to a separate, still-running replay.
        # CodeRabbit round-3 P2.
        self._speaking_owner: str = ""
        # Wire the controller's history snapshot provider so each
        # voice turn captures the visible transcript on the GUI thread
        # before the executor takes over. The closure reads
        # `system_prompt` off the chat client (the v0.10 source-of-
        # truth) and the cap off the controller (set from
        # `AppConfig.max_history_turns`). With the coordinator's trim
        # active, the model is already capped — `max_turns` here is a
        # defensive backup.
        self.controller.chat_history_provider = self._build_chat_history_messages
        # Token-usage state for the QML context-window readout. `used`
        # is the running prompt + completion tokens reported on the
        # latest LM Studio chunk-with-usage; `ceiling` is the loaded
        # model's context length (0 = unknown / no model loaded).
        self._context_tokens_used: int = 0
        self._context_tokens_ceiling: int = 0
        # Dedicated single-worker executor for `fetch_loaded_context_length()`
        # so the call cannot block the GUI thread and cannot wedge the
        # `LlmController` executor that drives connect/select. Sized 1 —
        # multiple in-flight fetches would race the same HTTP endpoint
        # for no benefit.
        self._context_length_executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="voiceagent-ctxlen"
        )
        self._model_progress_value = 0.0
        self._model_progress_indeterminate = False
        self._model_progress_text = ""
        self._tts_progress_value = 0.0
        self._tts_progress_indeterminate = False
        self._tts_progress_text = ""
        # Incremental list models so per-row state flips don't rebuild the whole
        # ListView (which would reset contentY and jump to top). The adapter
        # pulls live state from the loader + backend so the model never owns
        # duplicate copies of `_active_items` / progress.
        self._stt_state_adapter = _CatalogStateAdapter(
            loader=self.model_loader, backend=self.model_loader.transcriber
        )
        self._tts_state_adapter = _CatalogStateAdapter(
            loader=self.tts_loader, backend=self.tts_loader.tts_service
        )
        self._stt_catalog_model = CatalogModel(
            self._stt_catalog, self._stt_state_adapter, self
        )
        self._tts_catalog_model = CatalogModel(
            self._tts_catalog, self._tts_state_adapter, self
        )
        # Set when `selectTtsEngine` is invoked while a pipeline is
        # active. The one-shot listener on `pipeline_state_changed`
        # consumes it on the next IDLE transition. None when no swap
        # is queued — the listener disconnects itself once it fires.
        self._pending_tts_engine: str | None = None

        # Chatterbox reference-voice recorder state. The worker thread
        # writes `_chatterbox_record_progress` / `_chatterbox_record_finished`
        # signals; the GUI-thread slots translate them into Property
        # updates the QML recording dialog reads. `_cancel_flag` is the
        # one-way kill switch the QML "Stop" button toggles.
        self._chatterbox_record_thread: threading.Thread | None = None
        self._chatterbox_record_cancel_flag = False
        self._chatterbox_record_active = False
        self._chatterbox_record_progress_value = 0.0
        self._chatterbox_record_active_name = ""
        self._chatterbox_record_progress.connect(
            self._on_chatterbox_record_progress
        )
        self._chatterbox_record_finished.connect(
            self._on_chatterbox_record_finished
        )

        self.controller.status_changed.connect(self._emit_ui_changed)
        self.controller.connection_changed.connect(self._handle_connection_changed)
        self.controller.live_transcript_changed.connect(self._sync_live_user_message)
        self.controller.transcript_changed.connect(self._append_user_message)
        self.controller.response_changed.connect(self._append_assistant_message)
        self.controller.error_changed.connect(self._set_error_message)
        self.controller.state_changed.connect(self._apply_state)
        # v0.10 streaming: chunk-level chat signals from VoiceController
        # forward to the turn coordinator (which threads them into the
        # active assistant draft bubble) and the per-turn token count
        # surfaces via `contextTokensUsed`.
        self.controller.chat_thinking_chunk.connect(self._on_chat_thinking_chunk)
        self.controller.chat_content_chunk.connect(self._on_chat_content_chunk)
        self.controller.chat_usage_changed.connect(self._on_chat_usage_changed)
        self.replay_player.playback_started.connect(self.controller.handle_aux_playback_started)
        self.replay_player.playback_finished.connect(self.controller.handle_aux_playback_finished)
        self.replay_player.playback_failed.connect(self.controller.handle_aux_playback_failed)
        # speakingRow tracking. The in-pipeline player belongs to the
        # controller; the replay player is ours. Both feed the same
        # `_speaking_row` field — only one path can be active at a
        # time, and either player ending resets to -1.
        self.controller.player.playback_started.connect(
            self._on_main_playback_started
        )
        self.controller.player.playback_finished.connect(
            self._on_main_playback_ended
        )
        self.controller.player.playback_failed.connect(
            self._on_main_playback_failed
        )
        # Replay-side speakingRow: replayMessage sets _speaking_row +
        # _speaking_owner synchronously the moment the user clicks ▶
        # (so the toggle responds immediately), then dispatches synth
        # on the _replay_executor. Playback_started carries no extra
        # info we need — the row + owner are already correct. Only
        # the end-of-playback resets matter on this signal, and only
        # if "replay" still owns the row at that point.
        self.replay_player.playback_finished.connect(
            self._on_replay_playback_ended
        )
        self.replay_player.playback_failed.connect(
            self._on_replay_playback_failed
        )
        self.model_loader.ready_changed.connect(self._emit_ui_changed)
        self.model_loader.loading_changed.connect(self._emit_ui_changed)
        self.model_loader.status_changed.connect(self._apply_model_status)
        self.model_loader.progress_changed.connect(self._apply_model_progress)
        self.model_loader.item_loading_changed.connect(self._on_stt_item_loading_changed)
        self.model_loader.item_progress_changed.connect(self._on_stt_item_progress_changed)
        self.model_loader.error_changed.connect(self._set_error_message)
        # Route selection changes through the inventory handler so the
        # catalog model picks up custom-path enter/leave (set_model_name
        # toggles `_custom_path` which shifts `available_items()`). A
        # plain `_emit_ui_changed` would skip the catalog rebuild and
        # leave a stale custom row when the user selects a managed model.
        self.model_loader.selection_changed.connect(self._handle_inventory_change)
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
        self.tts_loader.catalog_changed.connect(self._on_tts_catalog_changed)
        self._llm.urls_changed.connect(self._on_llm_urls_changed)
        self._llm.current_url_changed.connect(self._on_llm_current_url_changed)
        self._llm.connection_state_changed.connect(self._on_llm_connection_state_changed)
        self._llm.connection_busy_changed.connect(self._on_llm_busy_changed)
        self._llm.model_busy_changed.connect(self._on_llm_busy_changed)
        self._llm.models_changed.connect(self._on_llm_models_changed)
        self._llm.selected_model_changed.connect(self._on_llm_selected_model_changed)
        self._llm.status_message.connect(self._on_llm_status_message)
        self._llm.error.connect(self._on_llm_error)
        # Worker → GUI bridge for the context-length fetch. The slot
        # validates the result is for the *current* selection before
        # writing through to `_context_tokens_ceiling`, which guards
        # against late results from a previous model after a fast
        # load-then-switch.
        self._context_length_fetched.connect(self._on_context_length_fetched)

        self._restore_initial_selections()
        self._apply_state(self.controller.state.value)
        self._apply_theme_mode(self.settings.value("theme_mode", "auto", str) or "auto")

        self.engine = QQmlApplicationEngine()
        # i18n context: PyKF6.KI18n.KLocalizedContext is not available
        # in this venv (no PyKF6 module at all on the host), so wire a
        # tiny identity-pass translator under the `i18nCtx` context
        # property name. QML call sites use `i18nCtx.i18n("...")` and
        # the format-string shape `i18nCtx.i18n("Sent %1").arg(value)`,
        # which is swappable for a real KLocalizedContext later.
        self._translator = TranslatorContext(self)
        self.engine.rootContext().setContextProperty("i18nCtx", self._translator)
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
            schedule_after_first_frame(self._window, self._autoconnect_llm_server)
        # Defer the Piper `voices.json` network fetch until after QML
        # paints — see AGENTS.md's "keep network/model refreshes off the
        # first paint path" rule. The catalog starts populated with
        # whatever's already on disk; the refresh adds remote-only entries.
        # `schedule_after_first_frame` parallels the sounddevice pre-warm
        # in `app.py:142-158`: a 0 ms QTimer can fire on the next
        # event-loop tick before the first frame swap completes;
        # `QQuickWindow.frameSwapped` is the Qt-blessed primitive that
        # waits for the actual swap.
        if not self.tts_loader.catalog_refresh_scheduled:
            schedule_after_first_frame(
                self._window, self.tts_loader.refresh_catalog_async
            )

        # Sync the live TTS engine with the user's last QSettings choice.
        # `build_shared_services` (in app.py) reads `config.tts_engine`
        # which only consults the env var; it does not see QSettings.
        # If the user previously picked a different engine in the UI we
        # would otherwise show a banner saying "engine X selected" while
        # the actual service is still engine Y. Swap on first frame so
        # the divergence resolves before the user opens any pane.
        stored_engine = self.selectedTtsEngine
        active_engine = getattr(
            self.tts_loader.tts_service, "backend_name", ""
        ).lower()
        if stored_engine and stored_engine != active_engine:
            self._logger.info(
                "TTS engine startup desync: QSettings=%s service=%s — swapping",
                stored_engine,
                active_engine,
            )
            if stored_engine == "chatterbox" and not self._chatterbox_extras_available():
                # Chatterbox previously selected but extras now missing.
                # Reset QSettings to piper to keep the UI honest and
                # prevent looping back into this same branch on every
                # subsequent launch.
                self._logger.warning(
                    "QSettings asks for Chatterbox engine but extras "
                    "are not installed; reverting to piper"
                )
                self.settings.setValue("selected_tts_engine", "piper")
                self.ui_changed.emit()
            else:
                # Call _perform_tts_engine_swap directly rather than
                # selectTtsEngine: the latter has a same-engine guard
                # that would short-circuit here (QSettings already says
                # `stored_engine`, so `selectedTtsEngine == stored_engine`).
                # The desync is precisely what we are trying to resolve.
                schedule_after_first_frame(
                    self._window,
                    lambda eng=stored_engine: self._perform_tts_engine_swap(eng),
                )

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
        # Tear down the context-length probe executor. Mirrors the
        # llm_controller shutdown pattern: drop pending fetches rather
        # than block app exit on a slow HTTP probe.
        self._context_length_executor.shutdown(
            wait=False, cancel_futures=True
        )
        # Same pattern for replay synthesis. A pending Piper synth
        # would otherwise block exit for several seconds; cancel
        # rather than wait. Set the flag BEFORE shutdown so a
        # late-arriving replay click hits the early-return guard.
        self._replay_executor_shutdown = True
        self._replay_executor.shutdown(
            wait=False, cancel_futures=True
        )
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

    @Property(QObject, constant=True)
    def sttCatalogModel(self) -> CatalogModel:  # noqa: N802
        return self._stt_catalog_model

    @Property(QObject, constant=True)
    def ttsCatalogModel(self) -> CatalogModel:  # noqa: N802
        return self._tts_catalog_model

    @Property(int, notify=ui_changed)
    def sttInstalledCount(self) -> int:  # noqa: N802
        return sum(1 for name in self._stt_catalog if self._is_stt_downloaded(name))

    @Property(int, notify=ui_changed)
    def ttsInstalledCount(self) -> int:  # noqa: N802
        return sum(1 for name in self._tts_catalog if self._is_tts_downloaded(name))

    @Property(str, notify=ui_changed)
    def selectedSttModel(self) -> str:  # noqa: N802
        current = self.model_loader.selected_model
        return current if current in self.sttOptions else ""

    @Property(str, notify=ui_changed)
    def selectedTtsModel(self) -> str:  # noqa: N802
        current = self.tts_loader.selected_model or ""
        return current if current in self.ttsOptions else ""

    @Property("QVariantList", constant=True)
    def ttsEngineOptions(self) -> list[str]:  # noqa: N802
        """Engines the user can pick in the Session Setup pane.

        v0.12 introduced the engine selector for the Piper ↔ Chatterbox
        swap. The list is constant for the process lifetime — adding a
        new engine is a code change, not a runtime mutation.
        """
        return list(_TTS_ENGINE_OPTIONS)

    @Property(str, notify=ui_changed)
    def selectedTtsEngine(self) -> str:  # noqa: N802
        stored = self.settings.value("selected_tts_engine", "piper", str) or "piper"
        normalized = str(stored).strip().lower()
        return normalized if normalized in _TTS_ENGINE_OPTIONS else "piper"

    @Property(str, notify=ui_changed)
    def ttsConfigPaneFile(self) -> str:  # noqa: N802
        """QML filename of the per-engine TTS config pane.

        Resolved by the Voice Models dialog's Loader, which prepends
        ``"engines/"`` and instantiates the pane. Adding a future TTS
        engine is one new ``qml/engines/<Name>TtsConfigPane.qml`` file
        plus one branch here — `MainWindow.qml` does not change.
        """
        engine = self.selectedTtsEngine
        if engine == "chatterbox":
            return "ChatterboxTtsConfigPane.qml"
        return "PiperTtsConfigPane.qml"

    @Property(str, notify=ui_changed)
    def sttConfigPaneFile(self) -> str:  # noqa: N802
        """QML filename of the per-engine STT config pane.

        Only Whisper today; the indirection is in place so adding a
        future STT engine is a one-line change here + one new QML pane.
        """
        return "WhisperSttConfigPane.qml"

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

    @Property(int, notify=ui_changed)
    def contextTokensUsed(self) -> int:  # noqa: N802
        return self._context_tokens_used

    @Property(int, notify=ui_changed)
    def contextTokensCeiling(self) -> int:  # noqa: N802
        return self._context_tokens_ceiling

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

    @Property(int, notify=ui_changed)
    def speakingRow(self) -> int:  # noqa: N802
        """Row index of the assistant bubble whose audio is currently
        being read aloud (in-pipeline auto-play OR user-triggered
        replay). -1 when nothing is playing. The conversation pane's
        per-bubble button uses this to toggle between ▶ (replay) and
        🤫 (quiet — stop the active speech)."""
        return self._speaking_row

    @Property(str, notify=ui_changed)
    def themeMode(self) -> str:  # noqa: N802
        stored = self.settings.value("theme_mode", "auto", str) or "auto"
        normalized = stored.strip().lower()
        return normalized if normalized in {"auto", "light", "dark"} else "auto"

    @Property(bool, notify=ui_changed)
    def logVerboseMode(self) -> bool:  # noqa: N802
        return self.settings.value("log_verbose_mode", False, bool)

    @Property(QObject, constant=True)
    def conversationModel(self) -> ConversationModel:  # noqa: N802
        return self._conversation_model

    @Slot(str)
    def selectSttModel(self, model_name: str) -> None:  # noqa: N802
        if model_name not in self.sttOptions:
            return
        # Only persist managed selections. Custom paths come from the
        # `WHISPER_MODEL` env var; persisting one would leave a ghost
        # entry in QSettings that resolves to the fallback on next launch
        # if the env var is unset (the path no longer appears in the
        # catalog), making selection state invisibly drift.
        if self.model_loader.transcriber.is_item_managed(model_name):
            self.settings.setValue("selected_stt_model", model_name)
        self.model_loader.select_model(model_name)
        self.ui_changed.emit()

    @Slot(str)
    def selectTtsModel(self, model_name: str) -> None:  # noqa: N802
        if model_name not in self.ttsOptions:
            return
        self.settings.setValue("selected_tts_model", model_name)
        # Per-engine memory: a user who flips Piper → Chatterbox → Piper
        # gets their last-used voice on each side restored on the next
        # swap, rather than a "first installed voice" fallback. The
        # generic `selected_tts_model` key remains the active selection
        # for whichever engine is live; the `_<engine>` keys are per-
        # engine snapshots restored by `_perform_tts_engine_swap`.
        engine = self.selectedTtsEngine
        self.settings.setValue(f"selected_tts_model_{engine}", model_name)
        self.tts_loader.select_model(model_name)
        self.ui_changed.emit()

    @Slot(str, str, result=str)
    def importChatterboxReference(self, source_path: str, name: str) -> str:  # noqa: N802
        """Import a user-supplied audio file into the Chatterbox
        references directory. Returns the saved name on success or
        an empty string on failure (logged + appended to the
        conversation log). Only valid when the live engine is
        Chatterbox; callers should hide the UI affordance otherwise.
        """
        if self.selectedTtsEngine != "chatterbox":
            self._logger.info("importChatterboxReference ignored: engine=%s", self.selectedTtsEngine)
            return ""
        service = getattr(self.tts_loader, "tts_service", None)
        importer = getattr(service, "import_reference_clip", None)
        if importer is None:
            # Defensive — should not happen if the QML button is gated
            # on `selectedTtsEngine === "chatterbox"`. Log only; user
            # never reaches this path under normal flow.
            self._logger.warning(
                "importChatterboxReference invoked but service has no "
                "import_reference_clip (engine=%s)", self.selectedTtsEngine
            )
            return ""
        try:
            cleaned = source_path
            if cleaned.startswith("file://"):
                cleaned = cleaned[len("file://"):]
            saved = importer(Path(cleaned), name or Path(cleaned).stem)
        except Exception as exc:  # noqa: BLE001
            self._logger.exception("Chatterbox reference import failed: %s", exc)
            self._append_log_message(
                f"Could not import reference clip: {exc}", "error"
            )
            return ""
        # Refresh catalog so QML rebinds to the new entry, and select it.
        new_catalog = list(service.available_items())
        self._tts_catalog = new_catalog
        self._tts_catalog_model.replace_names(new_catalog)
        new_name = saved.stem
        service.set_selected_item(new_name)
        self.tts_loader.select_model(new_name)
        self.settings.setValue("selected_tts_model_chatterbox", new_name)
        self.ui_changed.emit()
        # No success notification — the catalog list updating + voice
        # ComboBox switching to the new entry is the visible feedback.
        return new_name

    # ------------------------------------------------------------------
    # Chatterbox mic-capture (option A) — fixed-duration recorder.
    #
    # Distinct from `MicrophoneRecorder` (which is wired into the STT
    # pipeline, uses VAD, and streams continuously). The reference-voice
    # recorder needs the opposite: record exactly N seconds, no VAD,
    # save to disk as a 24 kHz mono WAV. Lives entirely in window.py
    # rather than as a service so the QML recording dialog has a single
    # owner for the worker thread + cancel flag + progress state.
    # ------------------------------------------------------------------

    @Property(bool, notify=ui_changed)
    def chatterboxRecordingActive(self) -> bool:  # noqa: N802
        return self._chatterbox_record_active

    @Property(float, notify=ui_changed)
    def chatterboxRecordingProgress(self) -> float:  # noqa: N802
        """0.0 → 1.0 fraction of the requested recording duration that
        has elapsed. ProgressBar in QML binds directly to this.
        """
        return self._chatterbox_record_progress_value

    @Property(str, notify=ui_changed)
    def chatterboxRecordingName(self) -> str:  # noqa: N802
        return self._chatterbox_record_active_name

    @Slot(str, float)
    def startChatterboxRecording(  # noqa: N802
        self, name: str, seconds: float = 15.0,
    ) -> None:
        """Start a fixed-duration mic capture in a worker thread. The
        worker emits `_chatterbox_record_progress` ticks ~10 Hz and a
        single `_chatterbox_record_finished` at the end. QML calls
        `cancelChatterboxRecording()` to stop early; the worker saves
        whatever it has captured up to that point.
        """
        if self.selectedTtsEngine != "chatterbox":
            return
        if self._chatterbox_record_active:
            return
        cleaned = "".join(
            c if c.isalnum() or c in "-_" else "_" for c in (name or "")
        ).strip("_") or "user-voice"
        self._chatterbox_record_cancel_flag = False
        self._chatterbox_record_active = True
        self._chatterbox_record_progress_value = 0.0
        self._chatterbox_record_active_name = cleaned
        self.ui_changed.emit()
        self._chatterbox_record_thread = threading.Thread(
            target=self._record_chatterbox_worker,
            args=(cleaned, max(1.0, float(seconds))),
            name="chatterbox-recorder",
            daemon=True,
        )
        self._chatterbox_record_thread.start()

    @Slot()
    def cancelChatterboxRecording(self) -> None:  # noqa: N802
        """Set the cancel flag. The worker polls it on each progress
        tick and stops the sounddevice stream when set; whatever was
        captured up to that point is saved as the reference clip.
        Closing the QML dialog without explicit cancel does NOT stop
        the worker — the user's recorded audio is preserved.
        """
        self._chatterbox_record_cancel_flag = True

    def _record_chatterbox_worker(self, name: str, seconds: float) -> None:
        # Background thread. `sd.rec()` is non-blocking — it allocates
        # a buffer and starts a stream that the host audio system fills.
        # We poll `sd.get_stream().active` and the cancel flag in a
        # short sleep loop, emitting progress ticks. On cancel we stop
        # the stream early and save the captured prefix; on natural
        # completion we save the full buffer.
        try:
            import sounddevice as sd
            import soundfile
            import numpy as np
        except ImportError as exc:
            self._chatterbox_record_finished.emit(
                "", False, f"audio extras missing: {exc}"
            )
            return

        sample_rate = 24_000  # Chatterbox native; matches the service
        total_frames = int(sample_rate * seconds)
        try:
            audio = sd.rec(
                total_frames,
                samplerate=sample_rate,
                channels=1,
                dtype="float32",
                blocking=False,
            )
        except Exception as exc:  # noqa: BLE001
            self._chatterbox_record_finished.emit(
                "", False, f"could not start recording: {exc}"
            )
            return

        start = time.monotonic()
        frames_captured = total_frames
        while True:
            stream = sd.get_stream() if hasattr(sd, "get_stream") else None
            if stream is None or not stream.active:
                break
            elapsed = time.monotonic() - start
            self._chatterbox_record_progress.emit(
                min(elapsed, seconds), seconds
            )
            if self._chatterbox_record_cancel_flag:
                try:
                    sd.stop()
                except Exception:  # noqa: BLE001
                    pass
                frames_captured = max(1, int(elapsed * sample_rate))
                break
            time.sleep(0.1)

        try:
            sd.wait()
        except Exception:  # noqa: BLE001
            pass
        self._chatterbox_record_progress.emit(seconds, seconds)

        service = getattr(self.tts_loader, "tts_service", None)
        refs_root = getattr(service, "references_root", None)
        if refs_root is None:
            self._chatterbox_record_finished.emit(
                "", False, "Chatterbox engine is no longer active"
            )
            return

        target = Path(refs_root) / f"{name}.wav"
        try:
            Path(refs_root).mkdir(parents=True, exist_ok=True)
            # Keep only the captured prefix when the user stopped early.
            buf = np.asarray(audio[:frames_captured])
            soundfile.write(str(target), buf, sample_rate)
        except Exception as exc:  # noqa: BLE001
            self._chatterbox_record_finished.emit(
                "", False, f"could not save recording: {exc}"
            )
            return

        self._chatterbox_record_finished.emit(name, True, "")

    def _on_chatterbox_record_progress(
        self, elapsed: float, total: float,
    ) -> None:
        # GUI-thread slot, queued from the worker thread.
        if total <= 0:
            self._chatterbox_record_progress_value = 0.0
        else:
            self._chatterbox_record_progress_value = min(
                1.0, max(0.0, elapsed / total)
            )
        self.ui_changed.emit()

    def _on_chatterbox_record_finished(
        self, name: str, success: bool, error: str,
    ) -> None:
        self._chatterbox_record_active = False
        self._chatterbox_record_progress_value = 0.0
        self._chatterbox_record_active_name = ""
        self._chatterbox_record_cancel_flag = False

        if not success:
            if error:
                self._append_log_message(
                    f"Recording failed: {error}", "error"
                )
            self.ui_changed.emit()
            return

        # Refresh catalog with the new reference clip and select it as
        # the active voice. Same path the import-flow uses.
        service = getattr(self.tts_loader, "tts_service", None)
        if service is None:
            self.ui_changed.emit()
            return
        new_catalog = list(service.available_items())
        self._tts_catalog = new_catalog
        self._tts_catalog_model.replace_names(new_catalog)
        if name in new_catalog:
            service.set_selected_item(name)
            self.tts_loader.select_model(name)
            self.settings.setValue("selected_tts_model_chatterbox", name)
        self.ui_changed.emit()

    @Slot(str)
    def selectTtsEngine(self, engine_name: str) -> None:  # noqa: N802
        """Swap the live TTS engine. Same-engine and unknown engine
        no-op. Chatterbox without extras logs the rejection to the
        conversation pane and aborts. Piper-side is always available.
        Live swaps wait for `controller.state == AppState.IDLE` so we
        don't pull the rug out from under an in-flight pipeline.
        """
        normalized = (engine_name or "").strip().lower()
        if normalized not in _TTS_ENGINE_OPTIONS:
            return
        if normalized == self.selectedTtsEngine:
            return
        if normalized == "chatterbox" and not self._chatterbox_extras_available():
            self._logger.info("Engine swap rejected: chatterbox extras missing")
            self._append_log_message(
                "Chatterbox engine is not installed. Install with "
                "`pip install voiceagent[chatterbox]` to enable it.",
                "error",
            )
            return
        if self.controller.state == AppState.IDLE:
            self._perform_tts_engine_swap(normalized)
            return
        # Pipeline mid-flight: stash and wait for the next IDLE
        # transition. A one-shot listener decouples the slot return
        # from the actual swap so the QML ComboBox can re-bind cleanly.
        self._logger.info(
            "Deferring TTS engine swap until pipeline is idle pending=%s state=%s",
            normalized,
            self.controller.state.value,
        )
        self._pending_tts_engine = normalized
        self.controller.pipeline_state_changed.connect(
            self._on_pipeline_state_for_engine_swap
        )

    def _on_pipeline_state_for_engine_swap(self, state: str, _status: str) -> None:
        if state != AppState.IDLE.value:
            return
        try:
            self.controller.pipeline_state_changed.disconnect(
                self._on_pipeline_state_for_engine_swap
            )
        except (RuntimeError, TypeError):
            # Already disconnected (re-entrant safety): swallow.
            pass
        pending = self._pending_tts_engine
        self._pending_tts_engine = None
        if pending is None:
            return
        self._perform_tts_engine_swap(pending)

    @staticmethod
    def _chatterbox_extras_available() -> bool:
        """Probe for the optional Chatterbox extras. Mirror of the
        equivalent helper in `app.py` so the UI gate (here) and the
        startup factory (there) agree on which engines are usable.
        `huggingface_hub` is a hard dep so it's not in the probe list.
        """
        import importlib.util

        for name in ("onnxruntime", "transformers", "librosa", "soundfile"):
            if importlib.util.find_spec(name) is None:
                return False
        return True

    def _build_tts_service_for_engine(self, engine: str):
        """Construct a fresh TTS service for `engine`. Re-derives its
        configuration via `AppConfig.from_env()` so any env-var changes
        the user made post-launch (e.g. swapping `TTS_COMMAND`) are
        picked up. Does NOT mutate `model_root` for Chatterbox — the
        service constructor takes the explicit per-engine subdirectory.
        """
        config = AppConfig.from_env()
        if engine == "chatterbox":
            from voiceagent.paths import default_chatterbox_model_root
            from voiceagent.services.chatterbox_tts import ChatterboxTtsService

            # `tts_model_root` is Piper-specific (defaults to
            # `<data>/tts/piper/`) under the v0.12.1 hierarchical layout;
            # the Chatterbox model cache has its own engine-scoped root.
            return ChatterboxTtsService(
                model_root=default_chatterbox_model_root(),
                references_root=config.chatterbox_references_root,
                selected_item=config.tts_model,
            )
        from voiceagent.services.tts import PiperTtsService

        service = PiperTtsService(
            command=config.tts_command,
            model_path=config.tts_model,
            extra_args=config.tts_extra_args,
        )
        service.model_root = config.tts_model_root
        return service

    def _perform_tts_engine_swap(self, engine: str) -> None:
        """Tear down the live TTS loader/service and stand up fresh
        ones for `engine`. Restores the per-engine remembered voice
        from QSettings so the user's last selection on either side
        survives a round-trip swap. Re-points the catalog model's
        state adapter so per-row state queries hit the new backend.
        Mirrors v0.12 Kokoro reference design.
        """
        self._logger.info("Performing TTS engine swap engine=%s", engine)
        self.settings.setValue("selected_tts_engine", engine)

        # Build the new service + loader before tearing the old one
        # down, so a constructor failure doesn't leave the window with
        # neither a working old loader nor a working new one.
        new_service = self._build_tts_service_for_engine(engine)
        new_loader = TtsVoiceLoader(new_service)

        old_loader = self.tts_loader
        try:
            old_loader.shutdown()
        except Exception:
            self._logger.exception("Old TTS loader shutdown failed during engine swap")

        # Restore the per-engine remembered voice. If the value is
        # not in the new service's catalog (uninstalled / first run),
        # `select_model(None)` leaves the loader in the
        # "no-selection" state and `_sync_installed_selections`
        # later picks the first installed voice if any.
        remembered = self.settings.value(
            f"selected_tts_model_{engine}", "", str
        ) or ""
        if remembered and remembered in new_service.available_items():
            new_service.set_selected_item(remembered)
            new_loader.select_model(remembered)

        self.tts_loader = new_loader
        self._tts_state_adapter = _CatalogStateAdapter(
            loader=new_loader, backend=new_service
        )
        # CatalogModel stores the adapter on `_state` (verified
        # against `catalog_model.py` — the kokoro reference docstring
        # called this `_state_adapter` but the live attribute is
        # `_state`). Re-pointing here means subsequent per-row reads
        # hit the new backend without rebuilding the QAbstractListModel.
        self._tts_catalog_model._state = self._tts_state_adapter
        new_catalog = list(new_service.available_items())
        self._tts_catalog = new_catalog
        self._tts_catalog_model.replace_names(new_catalog)

        # Wire all the same loader signals on the new instance.
        new_loader.ready_changed.connect(self._emit_ui_changed)
        new_loader.loading_changed.connect(self._emit_ui_changed)
        new_loader.status_changed.connect(self._apply_tts_status)
        new_loader.progress_changed.connect(self._apply_tts_progress)
        new_loader.item_loading_changed.connect(self._on_tts_item_loading_changed)
        new_loader.item_progress_changed.connect(self._on_tts_item_progress_changed)
        new_loader.error_changed.connect(self._set_error_message)
        new_loader.selection_changed.connect(self._emit_ui_changed)
        new_loader.load_completed.connect(self._handle_inventory_change)
        new_loader.delete_completed.connect(self._handle_inventory_change)
        new_loader.catalog_changed.connect(self._on_tts_catalog_changed)

        # Hand the controller the new service so the next pipeline
        # turn synthesizes via the new engine.
        self.controller.set_tts_service(new_service)

        # Sync selections: if the per-engine remembered voice didn't
        # match (first-ever swap to this engine, or the saved name was
        # uninstalled in the meantime), fall back to the first
        # installed voice so the ComboBox has a sensible currentIndex.
        # Without this, the ComboBox `currentIndex` resolves to -1 and
        # `displayText` shows the "no voice selected" placeholder text
        # even though the catalog itself is populated and the user has
        # voices installed — a major UX trap (see swap-chain
        # reproduction: chatterbox→piper with no `selected_tts_model_piper`
        # in QSettings).
        self._sync_installed_selections()

        # Trigger the deferred remote-catalog refresh on the new
        # loader. The user just opted into a new engine — surface
        # what's available without making them wait until the next
        # process launch.
        if not new_loader.catalog_refresh_scheduled and self._window is not None:
            schedule_after_first_frame(
                self._window, new_loader.refresh_catalog_async
            )

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

    @Slot(str)
    def selectLlmModel(self, model_name: str) -> None:  # noqa: N802
        self._llm.select_model(model_name)

    @Slot(str)
    def toggleLlmServerConnection(self, value: str) -> None:  # noqa: N802
        if self._llm.connection_busy and self._llm.server_connected:
            return
        if self._llm.server_connected:
            self._disconnect_llm_server()
            return
        self._connect_llm_server(value, True)

    def _connect_llm_server(self, value: str, show_error: bool = True) -> None:
        self._llm.connect_server(value, show_error)

    def _disconnect_llm_server(self) -> None:
        if self._llm.connection_busy or self._llm.model_busy:
            return
        if self.voiceConnectionEnabled:
            self.controller.stop_recording()
        self._llm.disconnect_server()

    def _autoconnect_llm_server(self) -> None:
        self._llm.autoconnect_server()

    @Slot(bool)
    def setVoiceConnectionEnabled(self, enabled: bool) -> None:  # noqa: N802
        started = time.monotonic()
        if enabled:
            self.persistCurrentLlmUrl()
            self.controller.start_recording()
            log_ui_timing(self._logger, "setVoiceConnectionEnabled.on", started)
            return
        self.controller.stop_recording()
        log_ui_timing(self._logger, "setVoiceConnectionEnabled.off", started)

    @Slot()
    def stopSpeaking(self) -> None:  # noqa: N802
        """Cut the current speech mid-utterance. Stops both the
        in-pipeline auto-play (`controller.player`) and the
        user-triggered replay (`replay_player`). Does NOT touch the
        voice connection — the mic stays in whatever state it was in
        so the next user turn can begin immediately. The QML
        per-bubble button calls this when the user taps 🤫.

        `controller.cancel_playbacks()` does the actual main-player
        stop AND resets `_playing_response` / `_aux_playback_active`,
        which is required because the v0.11 teardown-error
        suppression in `services/playback.py` swallows
        `playback_finished` on stop-induced exits. Without that flag
        reset, the mic-resume gate stayed stuck and "Listening" was
        a lie (the user reported this exact symptom).
        """
        # Cancel an in-flight replay synth too. Without this, a
        # not-yet-started synth in the executor's queue would burn
        # through a full Piper run after stopSpeaking returned, then
        # the completion handler would see the stale request and
        # discard the audio — wasted work, and any FileHandler
        # shutdown that happened between stop and synth-completion
        # could surface as a noisy log error. CodeRabbit round-4
        # nitpick. The completion handler short-circuits on
        # `future.cancelled()` so this is safe.
        synth_future = self._replay_synth_future
        if synth_future is not None and not synth_future.done():
            synth_future.cancel()
        # Bump the request token. A synth job that was already
        # running (cancel can't stop it) will complete with its
        # original request_id; the completion handler's
        # `request_id == self._replay_request_id` check will be
        # False and the audio will be discarded. CodeRabbit round-4
        # P2.
        self._replay_request_id += 1
        self.replay_player.stop()
        self.controller.cancel_playbacks()
        if self._speaking_row != -1 or self._speaking_owner:
            self._speaking_row = -1
            self._speaking_owner = ""
            self.ui_changed.emit()

    @Slot(bool)
    def setLogVerboseMode(self, enabled: bool) -> None:  # noqa: N802
        if bool(enabled) == self.logVerboseMode:
            return
        # The coordinator reads `logVerboseMode` (and thus
        # `QSettings`) live via its callable provider — no need to
        # push the update separately. See `__init__`.
        self.settings.setValue("log_verbose_mode", bool(enabled))
        self.ui_changed.emit()

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

    @Slot(int, bool)
    def setThinkingExpanded(self, row: int, expanded: bool) -> None:  # noqa: N802
        # Thin forwarder — the coordinator owns the row-level mutation
        # so the conversation model is the only writer of bubble state.
        self._turn_coordinator.set_thinking_expanded(row, expanded)

    @Slot(int)
    def replayMessage(self, index: int) -> None:  # noqa: N802
        message = self._conversation_model.message(index)
        if message is None:
            return
        if message.get("role") != "assistant":
            return
        text = str(message.get("text", "")).strip()
        if not text:
            return
        # is_available checks both .onnx and .onnx.json exist for the
        # selected voice (tighter than `enabled`, which only checks
        # command + path). Without it, replay can call into a
        # half-installed voice and raise into the QML binding.
        if not self.tts_loader.tts_service.is_available:
            # Surface the click silently-failing through the
            # conversation log so the user can see why playback didn't
            # happen. Logger entry is for diagnostics; the conversation
            # log is the user-visible surface.
            self._logger.info("Replay skipped: TTS not available")
            self._append_log_message(
                "Replay unavailable: the selected voice is not ready.",
                "error",
            )
            return
        # Toggle the row's button to 🤫 IMMEDIATELY so the click is
        # responsive. Synth itself runs on the background executor —
        # Piper takes 5-7 s for a typical reply, and running it on the
        # GUI thread froze the click handler, queueing additional
        # clicks that cascaded into stacked PortAudio workers and an
        # eventual crash. If the user taps the same button while
        # synth is still running, `stopSpeaking` will reset
        # `_speaking_row`; the synth-completion handler sees the
        # mismatch and discards the audio without starting playback.
        self._speaking_row = index
        self._speaking_owner = "replay"
        self.ui_changed.emit()
        # Mint a fresh request token. Synth completions check this
        # value, not `_speaking_row` — `_speaking_row` can shift
        # under the synth via `_on_rows_dropped_from_front` (history
        # cap trim), so an index-based cancellation gate would
        # spuriously discard an in-flight replay whose row just got
        # reindexed. CodeRabbit round-4 P2.
        self._replay_request_id += 1
        request_id = self._replay_request_id
        # Stop any concurrent in-pipeline auto-play. Without this,
        # the user clicking ▶ on the freshly-finalized assistant row
        # produces TWO audio streams: the auto-play we never asked
        # to silence + the replay we just kicked off. Also closes
        # the ownership-claim race CodeRabbit round-4 Major flagged
        # in `_on_main_playback_started` — there's no longer a
        # late playback_started arriving from the main player to
        # overwrite our "replay" ownership.
        #
        # Route through `cancel_playbacks()`, NOT `player.stop()`
        # directly: the v0.11 teardown-error suppression in
        # `services/playback.py` swallows `playback_finished` on
        # stop_event-induced exits, so a bare `player.stop()` here
        # leaves `_playing_response` stuck `True` from the original
        # auto-play. `_partial_skip_reason` then keeps gating mic
        # resume even after the replay's aux audio finishes. Codex
        # round-4 P1.
        #
        # Stop `replay_player` BEFORE `cancel_playbacks()` so the
        # replay-on-replay path (replay A playing → user clicks ▶
        # on row B) doesn't leak audio: `cancel_playbacks()`
        # force-clears `_aux_playback_active` and may resume mic
        # input via `_resume_listening_if_possible`. If A's replay
        # audio is still streaming out of `replay_player` at that
        # point, it bleeds into STT and corrupts the next turn.
        # Same two-step pattern `stopSpeaking()` uses. Codex
        # round-6 P1.
        self.replay_player.stop()
        self.controller.cancel_playbacks()
        if self._replay_executor_shutdown:
            return
        # Cancel a still-pending prior synth before queueing the new
        # one. The single-worker executor serializes jobs, so a rapid
        # spam-click would otherwise stack the queue. Cancelling
        # outright (when possible) keeps the queue at one job and
        # lets `_handle_replay_synth_done` short-circuit on
        # `future.cancelled()`. CodeRabbit round-3 P2.
        previous_future = self._replay_synth_future
        if previous_future is not None and not previous_future.done():
            previous_future.cancel()
        try:
            future = self._replay_executor.submit(
                self._run_replay_synth, request_id, text
            )
        except RuntimeError:
            # Executor was shut down between the flag check above
            # and the submit. Roll back the optimistic toggle.
            if self._speaking_row == index:
                self._speaking_row = -1
                self._speaking_owner = ""
                self.ui_changed.emit()
            return
        self._replay_synth_future = future
        future.add_done_callback(self._handle_replay_synth_done)

    def _run_replay_synth(self, request_id: int, text: str) -> tuple[int, object, object]:
        """Run TTS synthesis on the executor thread. Returns a
        (request_id, audio_path | None, error_message | None) triple.
        `request_id` is the per-replay token minted by `replayMessage`;
        the GUI-thread completion handler compares it to the current
        `_replay_request_id` to decide whether the result is still
        wanted. The try/except keeps the worker thread itself from
        raising — the future's done-callback fires regardless and
        bridges the result back to the GUI thread via
        `_replay_synth_completed`.
        """
        try:
            audio_path = self.tts_loader.tts_service.synthesize(text)
        except Exception as exc:  # noqa: BLE001 - surfaced to the GUI
            self._logger.exception("Replay synthesis failed")
            return request_id, None, str(exc)
        return request_id, audio_path, None

    def _handle_replay_synth_done(self, future: Future) -> None:
        # Runs on the executor thread (or sync on the owner thread
        # when add_done_callback registers against an already-done
        # future). Bridge across `_replay_synth_completed` so the
        # GUI thread is the sole writer to `_speaking_row`.
        # Cancelled futures: a fresh `replayMessage` cancelled this
        # one before it completed. `future.result()` would raise
        # `CancelledError`; we want to skip the emit entirely so
        # the cancelled work doesn't trigger a stale completion
        # path on the GUI thread. CodeRabbit round-3 P2.
        if future.cancelled():
            return
        try:
            request_id, audio_path, error_message = future.result()
        except Exception as exc:  # pragma: no cover - defensive
            self._replay_synth_completed.emit(-1, (None, str(exc)))
            return
        self._replay_synth_completed.emit(request_id, (audio_path, error_message))

    @Slot(int, object)
    def _on_replay_synth_completed(self, request_id: int, payload: object) -> None:
        # Runs on the GUI thread (queued from the worker via
        # `_replay_synth_completed`).
        # Shutdown guard. If `MainWindow.shutdown()` already tore
        # down the replay player + conversation model, a late-
        # arriving completion that calls `play_file` would crash.
        # CodeRabbit round-3 P2.
        if self._replay_executor_shutdown or self._shutting_down:
            audio_path, _ = payload  # type: ignore[misc]
            if audio_path is not None:
                try:
                    Path(str(audio_path)).unlink(missing_ok=True)
                except OSError:
                    pass
            return
        audio_path, error_message = payload  # type: ignore[misc]
        # Token check: is this still the latest replay the user asked
        # for? If a fresh `replayMessage` (or `stopSpeaking`) bumped
        # the token, this completion is stale — discard. Decoupled
        # from `_speaking_row` so a trim-induced row shift can't
        # spuriously look like cancellation. CodeRabbit round-4 P2.
        is_current = request_id == self._replay_request_id
        if error_message is not None:
            # Stale failures: don't surface them. If replay A was
            # superseded by replay B (or cancelled via stopSpeaking),
            # a late exception from A's synth thread would otherwise
            # show as a user-visible "Replay failed" toast for an
            # abandoned request the user has already moved on from.
            # The audio-dispatch path below already gates on
            # `is_current`; the error path was the symmetric gap.
            # CodeRabbit round-5 Major.
            if not is_current:
                return
            wrapped = f"Replay failed: {error_message}"
            # `_set_error_message` already routes the wrapped reason to
            # the conversation log via the turn coordinator.
            self._set_error_message(wrapped, discard_draft=False)
            if self._speaking_owner == "replay":
                self._speaking_row = -1
                self._speaking_owner = ""
                self.ui_changed.emit()
            return
        if audio_path is None:
            if is_current and self._speaking_owner == "replay":
                self._speaking_row = -1
                self._speaking_owner = ""
                self.ui_changed.emit()
            return
        # Stale completion: user clicked 🤫 (stopSpeaking) OR started
        # a different replay while synth was running. Discard the
        # audio without dispatching it to the player.
        if not is_current:
            try:
                Path(str(audio_path)).unlink(missing_ok=True)
            except OSError:
                self._logger.exception(
                    "Discarded replay audio cleanup failed path=%s",
                    audio_path,
                )
            return
        # `play_file` returns False when a concurrent supersede mints
        # a newer generation between our entry and the worker
        # registering its own thread (the AudioPlayer drops superseded
        # play attempts entirely and unlinks the temp file). If that
        # happens we'd otherwise leave `_speaking_row` pinned to the
        # row forever, with no `playback_started` signal coming to
        # bring it back. CodeRabbit round-3 Major.
        dispatched = self.replay_player.play_file(audio_path)
        if not dispatched and self._speaking_owner == "replay":
            self._speaking_row = -1
            self._speaking_owner = ""
            self.ui_changed.emit()

    def _handle_inventory_change(self) -> None:
        self._refresh_stt_catalog_if_changed()
        self._sync_installed_selections()
        self.ui_changed.emit()

    def _refresh_stt_catalog_if_changed(self) -> None:
        # Whisper's `available_items()` is mostly static (managed
        # repository keys), but a custom path can enter / leave the list
        # when `set_model_name` is called with a path-shaped value. Rebuild
        # the catalog model when the underlying list shape actually shifts
        # so a custom-path row appears or disappears in the Model Manager
        # without a full app restart.
        new_catalog = self.model_loader.transcriber.available_items()
        if new_catalog == self._stt_catalog:
            return
        self._stt_catalog = list(new_catalog)
        self._stt_catalog_model.replace_names(self._stt_catalog)

    def _on_tts_catalog_changed(self, names: list[str]) -> None:
        # The deferred remote refresh landed new voices. Swap the list
        # that drives ttsOptions and push the new names into the
        # QAbstractListModel so the ComboBox / catalog list rebinds.
        new_names = list(names)
        if new_names == self._tts_catalog:
            return
        self._tts_catalog = new_names
        self._tts_catalog_model.replace_names(new_names)
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
        # Only persist managed STT selections — see `selectSttModel` for
        # the full reasoning. Custom paths live in `WHISPER_MODEL` and
        # should not leave a ghost entry in QSettings.
        if self.selectedSttModel and self.model_loader.transcriber.is_item_managed(self.selectedSttModel):
            self.settings.setValue("selected_stt_model", self.selectedSttModel)
        if self.selectedTtsModel:
            self.settings.setValue("selected_tts_model", self.selectedTtsModel)

    def _append_user_message(self, text: str) -> None:
        # Thin forwarder — coordinator owns ordering policy.
        self._turn_coordinator.on_user_transcript(text)

    def _append_assistant_message(self, text: str) -> None:
        # Thin forwarder — coordinator owns ordering policy.
        self._turn_coordinator.on_assistant_response(text)

    def _append_log_message(self, text: str, level: str) -> None:
        # Thin forwarder — coordinator owns ordering policy.
        self._turn_coordinator.on_log_message(text, level)

    def _on_main_playback_started(self, _path: str) -> None:
        # In-pipeline auto-play: the speaking row is the most recent
        # finalized assistant bubble. The pipeline appends the
        # assistant turn to the model BEFORE invoking play_file, so
        # this find is reliable.
        #
        # Ownership guard: don't overwrite "replay" ownership. If
        # the user clicked ▶ on the freshly-finalized assistant row
        # (the row this auto-play is for), `replayMessage` already
        # stopped our `controller.player` and claimed
        # `_speaking_owner = "replay"`. A late playback_started
        # arriving from the now-stopped main player would otherwise
        # clobber the replay's ownership, and the subsequent
        # main-player playback_finished would clear `_speaking_row`
        # mid-replay. CodeRabbit round-4 Major.
        if self._speaking_owner == "replay":
            return
        self._speaking_row = self._conversation_model.find_message_index(
            "assistant", bubble_state="sent", turn_pending=False
        )
        self._speaking_owner = "controller"
        self.ui_changed.emit()

    def _on_main_playback_ended(self, _path: str) -> None:
        # Only reset if the controller still owns the row. A
        # late-arriving finished signal from an abandoned in-pipeline
        # worker that completed AFTER the user started a replay
        # otherwise wipes the replay's row → 🤫 disappears mid-
        # replay. CodeRabbit round-3 P2.
        if self._speaking_owner == "controller":
            self._speaking_row = -1
            self._speaking_owner = ""
            self.ui_changed.emit()

    def _on_main_playback_failed(self, _path: str, _message: str) -> None:
        if self._speaking_owner == "controller":
            self._speaking_row = -1
            self._speaking_owner = ""
            self.ui_changed.emit()

    def _on_replay_playback_ended(self, _path: str) -> None:
        if self._speaking_owner == "replay":
            self._speaking_row = -1
            self._speaking_owner = ""
            self.ui_changed.emit()

    def _on_replay_playback_failed(self, _path: str, _message: str) -> None:
        if self._speaking_owner == "replay":
            self._speaking_row = -1
            self._speaking_owner = ""
            self.ui_changed.emit()

    def _on_rows_dropped_from_front(self, count: int) -> None:
        """Shift `_speaking_row` to track a coordinator front-trim.

        When `_trim_to_history_cap` removes the oldest N rows from
        the model, every row index above the dropped range shifts
        down by N. Our `_speaking_row` is a row index into the same
        model; if we don't shift it, the ▶/🤫 toggle renders on
        the wrong row after the trim. If `_speaking_row` itself was
        inside the dropped range, the bubble is gone — reset to -1.
        Mirrors the same pattern the coordinator applies to its own
        `_streaming_assistant_index`. CodeRabbit round-3 P1.
        """
        if count <= 0:
            return
        if self._speaking_row < 0:
            return
        if self._speaking_row < count:
            # The speaking bubble itself was inside the dropped range;
            # nothing to track. Clear ownership too — neither player
            # owns a row that no longer exists.
            self._speaking_row = -1
            self._speaking_owner = ""
        else:
            self._speaking_row -= count
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
            self._append_log_message(status, "status")
        self.ui_changed.emit()

    def _apply_model_progress(self, progress) -> None:
        self._model_progress_value, self._model_progress_indeterminate, self._model_progress_text = self._format_progress(
            progress
        )
        self.progress_changed.emit()

    def _apply_tts_status(self, status: str) -> None:
        if self.tts_loader.is_loading:
            self._append_log_message(status, "status")
        self.ui_changed.emit()

    def _apply_tts_progress(self, progress) -> None:
        self._tts_progress_value, self._tts_progress_indeterminate, self._tts_progress_text = self._format_progress(
            progress
        )
        self.progress_changed.emit()

    def _on_stt_item_loading_changed(self, model_name: str, _is_loading: bool) -> None:
        # Per-row state flip: installed/loading/downloadable can all change
        # together. Adapter pulls live state from the loader + backend.
        self._stt_catalog_model.refresh_row(model_name)
        # `*InstalledCount` and `sttOptions` watch ui_changed.
        self.ui_changed.emit()

    def _on_stt_item_progress_changed(self, model_name: str, _progress) -> None:
        # Narrow the dataChanged role list to ProgressRole only; sibling
        # bindings on installed/loading stay asleep between aria2 ticks.
        self._stt_catalog_model.refresh_progress(model_name)

    def _on_tts_item_loading_changed(self, model_name: str, _is_loading: bool) -> None:
        self._tts_catalog_model.refresh_row(model_name)
        self.ui_changed.emit()

    def _on_tts_item_progress_changed(self, model_name: str, _progress) -> None:
        self._tts_catalog_model.refresh_progress(model_name)

    def _apply_state(self, state: str) -> None:
        self._state = state
        # Per-turn ordering (promote-draft, dedupe, queue/flush of
        # verbose pipeline status rows, RECORDING/IDLE boundary
        # reset) lives in the coordinator. MainWindow only owns the
        # derived-state repaint here.
        self._turn_coordinator.on_state_changed(state)
        self.ui_changed.emit()

    def _set_error_message(self, message: str, *, discard_draft: bool = True) -> None:
        # The coordinator owns the discard-draft + append-log-row
        # policy. discard_draft default documented at its
        # `on_error_message`.
        self._turn_coordinator.on_error_message(message, discard_draft=discard_draft)
        self.ui_changed.emit()

    def _handle_connection_changed(self, enabled: bool) -> None:
        self._turn_coordinator.on_connection_changed(enabled)
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

    def _on_llm_selected_model_changed(self, model: str) -> None:
        # Short-circuit duplicate fires of the same model. LlmController
        # already tries to gate this internally, but several paths
        # (set_current_url, refresh_models, autoconnect retries) can
        # legitimately re-emit with an unchanged value; we don't want
        # to reset counters or queue a fresh context-length probe each
        # time a refresh re-confirms the loaded model.
        if model == self._last_selected_llm_model:
            return
        previous_model = self._last_selected_llm_model
        self._last_selected_llm_model = model
        # Genuine model swap — record it in the conversation log so the
        # debug surface ties pre/post-swap turns to a clear boundary.
        logging.getLogger(CONVERSATION_LOGGER_NAME).info(
            "model-changed previous=%r new=%r", previous_model, model
        )
        # When the loaded model changes, the previous ceiling no longer
        # applies. Reset immediately on the GUI thread; if a new model
        # is loaded, hop the (blocking) context-length probe to the
        # background executor so the GUI thread never waits on HTTP.
        self._context_tokens_ceiling = 0
        # Token usage from the previous model's last turn is meaningless
        # against the new (or absent) model; clear it too.
        self._context_tokens_used = 0
        # v0.11 multi-turn history: the conversation persists across
        # model swaps — the user gets continuity (e.g. ask the same
        # question to two models and compare) rather than a surprise
        # wipe. The new model's loaded_context_length is re-fetched
        # via the bar above so the visual warning stays accurate
        # under the new ceiling. If the existing transcript is too
        # large for the new model, the LM Studio call will truncate
        # from the front — the token-aware trim follow-up
        # (roadmap.md v0.11.x) addresses that automatically; for
        # v0.11 the user can shrink `VOICEAGENT_MAX_HISTORY_TURNS`.
        if model:
            self._submit_context_length_fetch(model)
        self.ui_changed.emit()

    def _build_chat_history_messages(self) -> list[dict[str, str]]:
        """Snapshot the visible conversation as an OpenAI-shaped messages
        list. Invoked by `VoiceController` on the GUI thread immediately
        before each pipeline future is submitted, so the read of the
        underlying `ConversationModel` is consistent with the visible
        transcript at submit time. The current turn's user message is
        NOT included here — `_run_pipeline` appends it after STT.
        """
        return self._conversation_model.to_openai_messages(
            system_prompt=self.controller.chat_client.system_prompt,
            max_turns=self.controller.max_history_turns,
        )

    def _submit_context_length_fetch(self, model: str) -> None:
        # `_shutting_down` is set BEFORE `_context_length_executor.shutdown()`
        # in `shutdown()`, so any queued Qt slot that lands here after the
        # window has begun tearing down sees the flag and bails out before
        # touching a dead pool. The `RuntimeError` catch closes the residual
        # race between the flag check above and the `submit` call itself —
        # if the executor was shut down between those two lines, treat the
        # submission as dropped (same outcome as if we'd checked after).
        if self._shutting_down:
            return
        chat_client = self.controller.chat_client
        try:
            future = self._context_length_executor.submit(
                chat_client.fetch_loaded_context_length
            )
        except RuntimeError:
            return
        future.add_done_callback(
            lambda completed, name=model: self._handle_context_length_future(
                completed, name
            )
        )

    def _handle_context_length_future(
        self, future: Future[int], model: str
    ) -> None:
        # Runs on the worker thread. Coerce + bridge across the
        # `_context_length_fetched` signal so the GUI thread is the
        # only writer to `_context_tokens_ceiling`.
        try:
            value = int(future.result() or 0)
        except Exception:  # pragma: no cover - defensive bridge
            value = 0
        self._context_length_fetched.emit(value, model)

    @Slot(int, str)
    def _on_context_length_fetched(self, value: int, model: str) -> None:
        # Drop late results that arrive after the user has already
        # switched away from the model whose ceiling was being probed.
        # The current selection is on the chat client (not _llm.models)
        # because LlmController writes through `set_model` immediately
        # on each transition.
        if model != self.controller.chat_client.model:
            return
        if value < 0:
            value = 0
        if value == self._context_tokens_ceiling:
            return
        self._context_tokens_ceiling = value
        self.ui_changed.emit()

    def _on_chat_thinking_chunk(self, text: str) -> None:
        # Thin forwarder — coordinator owns the streaming-thinking
        # bubble policy. `conversation_changed` propagates through the
        # coordinator's own signal, but emitting here also notifies any
        # direct MainWindow observers when test doubles replace the
        # coordinator method.
        self._turn_coordinator.on_chat_thinking_chunk(text)
        self.conversation_changed.emit()

    def _on_chat_content_chunk(self, text: str) -> None:
        self._turn_coordinator.on_chat_content_chunk(text)
        self.conversation_changed.emit()

    def _on_chat_usage_changed(self, prompt: int, completion: int) -> None:
        self._context_tokens_used = int(prompt) + int(completion)
        self.ui_changed.emit()

    def _on_llm_status_message(self, message: str) -> None:
        if not message:
            return
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
        # Thin forwarder — coordinator owns ordering policy.
        self._turn_coordinator.on_live_transcript(text)

    def _clock_time(self) -> str:
        return datetime.now().strftime("%H:%M:%S")
