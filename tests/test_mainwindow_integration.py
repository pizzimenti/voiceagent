"""MainWindow-level integration tests.

These exercise the Python-side state machine in `voiceagent.window.MainWindow`
that round-2 reviews of PR #7 surfaced as a recurring source of subtle
ordering and lifecycle bugs:

- Status rows landing before the user bubble on short turns with no
  partial transcript (the "thinking is status, not bubble" invariant
  from AGENTS.md).
- Draft user-bubble promotion on final transcript arrival, with and
  without a prior partial.
- Verbose vs simple log-mode contract — status rows hidden in simple
  mode, including mid-turn toggles.
- `replayMessage` failure paths preserving the user's draft turn.
- Custom STT path selection surfacing as a catalog row.
- Connect/disconnect spam-click guard wired against
  `LlmController.connection_busy`.

The QML engine is stubbed so the tests run headless without needing
a real Kirigami environment; everything else (controller, loaders,
conversation model, LlmController) is real.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any

# Ensure the worktree's `src/` resolves before any editable install.
_HERE = Path(__file__).resolve().parent
_SRC = _HERE.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

# Headless Qt for any environment that didn't already set this.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QCoreApplication

from tests.fakes import (
    FakeChatClient,
    FakePlayer,
    FakeRecorder,
    FakeTranscriber,
    FakeTts,
)
from voiceagent.controller import VoiceController
from voiceagent.model_loader import WhisperModelLoader
from voiceagent.models import AppState
from voiceagent.tts_loader import TtsVoiceLoader


# --- QML stub ------------------------------------------------------------


class _StubQmlWindow:
    """Stand-in for the QML root object. Implements the surface
    MainWindow.shutdown() probes via `hasattr`."""

    def setVisible(self, visible: bool) -> None:
        pass

    def show(self) -> None:
        pass

    def raise_(self) -> None:
        pass

    def requestActivate(self) -> None:
        pass

    def close(self) -> None:
        pass

    def deleteLater(self) -> None:
        pass


class _StubQmlContext:
    """Stand-in for QQmlContext — accepts setContextProperty no-op."""

    def setContextProperty(self, name: str, value) -> None:  # noqa: N802
        pass


class _StubQmlEngine:
    """Stand-in for QQmlApplicationEngine — no-op load(); returns a
    non-empty rootObjects() so MainWindow's load-failure check passes.
    """

    def __init__(self) -> None:
        self._root = _StubQmlWindow()
        self._context = _StubQmlContext()

    def setInitialProperties(self, props) -> None:
        pass

    def rootContext(self):  # noqa: N802 — Qt API name
        return self._context

    def load(self, url) -> None:
        pass

    def rootObjects(self) -> list:
        return [self._root]

    def collectGarbage(self) -> None:
        pass

    def clearComponentCache(self) -> None:
        pass

    def deleteLater(self) -> None:
        pass


# --- fixtures ------------------------------------------------------------


@pytest.fixture
def main_window_factory(qtbot, monkeypatch, tmp_path):
    """Return a factory that builds a real MainWindow with stubbed QML
    + minimal real backends. Each call gets a fresh window; the fixture
    tears them all down at the end of the test.
    """
    monkeypatch.setattr(
        "voiceagent.window.QQmlApplicationEngine", _StubQmlEngine
    )

    created: list = []

    def _make(
        *,
        log_verbose: bool = True,
        stt_model_name: str = "tiny.en",
        installed_stt: tuple[str, ...] = ("tiny.en",),
    ) -> Any:
        from voiceagent.window import MainWindow  # imported AFTER monkeypatch

        transcriber = FakeTranscriber(
            model_root=tmp_path, model_name=stt_model_name
        )
        # Pre-install requested models so `_sync_installed_selections`
        # has something to anchor on. Without at least one installed
        # managed model, MainWindow's startup syncer falls back to the
        # first installed entry — which would clobber a custom-path
        # selection set up at construction time.
        for name in installed_stt:
            transcriber.download_item(name)

        tts_service = FakeTts(model_root=tmp_path)

        controller = VoiceController(
            recorder=FakeRecorder(),
            transcriber=transcriber,
            chat_client=FakeChatClient(),
            tts_service=tts_service,
            player=FakePlayer(),
        )
        model_loader = WhisperModelLoader(transcriber)
        tts_loader = TtsVoiceLoader(tts_service)

        window = MainWindow(controller, model_loader, tts_loader)
        # Set the verbose-log preference BEFORE driving any state.
        window.settings.setValue("log_verbose_mode", log_verbose)

        created.append((window, controller, model_loader, tts_loader, tts_service, transcriber))
        return window

    yield _make

    teardown_logger = logging.getLogger(__name__)
    for window, _ctrl, _ml, _tl, _tts, _tx in created:
        try:
            window.shutdown()
        except Exception:  # noqa: BLE001 - teardown best-effort, but logged
            # Log instead of swallowing: a regression in MainWindow.shutdown
            # (leaked signal-disconnect, QObject lifetime bug, etc.) should
            # surface in pytest output rather than vanish into a green bar.
            teardown_logger.exception(
                "MainWindow.shutdown() raised during fixture teardown"
            )


def _drain_events(times: int = 5) -> None:
    """Pump the Qt event loop so queued slot deliveries (auto-connection
    becomes queued for cross-thread signals) land before assertions.
    """
    app = QCoreApplication.instance()
    assert app is not None
    for _ in range(times):
        app.processEvents()


def _wait_until(predicate, *, timeout: float = 2.0, interval: float = 0.02) -> None:
    """Pump Qt events in a loop until `predicate()` is truthy or the
    timeout fires. Lets tests wait deterministically on signal-driven
    state writes that hop through a worker thread (e.g. the v0.11
    replay-synth executor → `_replay_synth_completed` bridge)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        _drain_events(times=2)
        time.sleep(interval)
    raise AssertionError(
        f"_wait_until: predicate never became true within {timeout}s"
    )


def _user_messages(window) -> list[dict]:
    return [
        window._conversation_model.message(i)
        for i in range(window._conversation_model.rowCount())
        if (window._conversation_model.message(i) or {}).get("role") == "user"
    ]


def _all_messages(window) -> list[dict]:
    return [
        window._conversation_model.message(i)
        for i in range(window._conversation_model.rowCount())
    ]


# --- conversation lifecycle ----------------------------------------------


def test_status_rows_queue_until_user_bubble_lands(main_window_factory):
    """Round-2 ordering invariant: TRANSCRIBING/THINKING fired before
    a user bubble exists (short turn, no partial transcript) must NOT
    insert status rows above the user bubble. They are queued and
    flushed once the user bubble materializes via `_append_user_message`.
    """
    window = main_window_factory(log_verbose=True)

    # Drive a fresh turn and step into TRANSCRIBING/THINKING with NO
    # prior live_transcript_changed (no draft bubble created).
    window._apply_state(AppState.RECORDING.value)
    window._apply_state(AppState.TRANSCRIBING.value)
    window._apply_state(AppState.THINKING.value)
    _drain_events()

    # Conversation must still be empty — both states queued, none emitted.
    assert _all_messages(window) == [], (
        "status rows must not appear before the user bubble for the turn"
    )

    # Final transcript arrives → user bubble materializes → queued
    # statuses flush AFTER it, in order.
    window._append_user_message("hello there")
    _drain_events()

    messages = _all_messages(window)
    assert messages[0]["role"] == "user"
    assert messages[0]["text"] == "hello there"
    assert messages[0]["bubbleState"] == "sent"
    # Status rows from the queue, in the order they were observed.
    status_rows = [m for m in messages[1:] if m["role"] == "status"]
    state_names = [m["stateName"] for m in status_rows]
    assert state_names == ["transcribing", "thinking"]


def test_status_rows_appear_immediately_when_draft_bubble_exists(
    main_window_factory,
):
    """When a partial transcript has already created the draft user
    bubble, subsequent status states emit status rows immediately
    (the turn anchor is present, no queueing needed). Note: entering
    TRANSCRIBING also promotes the draft to `bubbleState='sent',
    turnPending=True` via `_promote_live_user_message`; the row is
    NOT finalized (turnPending stays True) until `_append_user_message`
    flips it.
    """
    window = main_window_factory(log_verbose=True)

    window._apply_state(AppState.RECORDING.value)
    window._sync_live_user_message("partial trans")  # creates draft
    _drain_events()
    assert window._turn_coordinator.current_turn_user_bubble_present is True

    window._apply_state(AppState.TRANSCRIBING.value)
    _drain_events()

    messages = _all_messages(window)
    # User bubble was promoted (sent + still turn-pending) and the
    # status row landed immediately after, since the turn anchor was
    # already present.
    assert messages[0]["role"] == "user"
    assert messages[0]["bubbleState"] == "sent"
    assert messages[0]["turnPending"] is True
    assert messages[1]["role"] == "status"
    assert messages[1]["stateName"] == "transcribing"


def test_simple_mode_drops_pending_status_rows_silently(main_window_factory):
    """Verbose=False: pending status states must never reach the
    transcript, even when they would otherwise have been queued.
    """
    window = main_window_factory(log_verbose=False)

    window._apply_state(AppState.RECORDING.value)
    window._apply_state(AppState.TRANSCRIBING.value)
    window._apply_state(AppState.THINKING.value)
    window._append_user_message("simple-mode turn")
    window._append_assistant_message("response")
    _drain_events()

    messages = _all_messages(window)
    roles = [m["role"] for m in messages]
    # User bubble + assistant bubble, no status rows.
    assert roles == ["user", "assistant"]


def test_verbose_to_simple_toggle_mid_turn_clears_queued_status_rows(
    main_window_factory,
):
    """User toggles verbose→simple while statuses are queued (no user
    bubble yet). The queue must drain silently — those entries belong
    to the previous mode and should not leak into the simple-mode
    transcript.
    """
    window = main_window_factory(log_verbose=True)

    window._apply_state(AppState.RECORDING.value)
    window._apply_state(AppState.TRANSCRIBING.value)
    window._apply_state(AppState.THINKING.value)
    # Queue holds two pending states; no user bubble yet.
    assert window._turn_coordinator.pending_status_log_states == [
        "transcribing",
        "thinking",
    ]

    # Mid-turn switch to simple.
    window.settings.setValue("log_verbose_mode", False)

    # Final transcript flushes the queue — but in simple mode it drops.
    window._append_user_message("turn after toggle")
    _drain_events()

    messages = _all_messages(window)
    roles = [m["role"] for m in messages]
    assert "status" not in roles, (
        "queued status rows must drop silently when verbose was disabled "
        "between queuing and flush"
    )
    # Queue is empty regardless of mode.
    assert window._turn_coordinator.pending_status_log_states == []


# --- draft → final user-bubble promotion ---------------------------------


def test_draft_promoted_to_sent_on_final_transcript(main_window_factory):
    """Partial transcript creates a draft bubble; final transcript
    promotes the SAME row to sent (no second user bubble appended).
    """
    window = main_window_factory()

    window._sync_live_user_message("hel")
    window._sync_live_user_message("hello wor")
    _drain_events()

    drafts = _user_messages(window)
    assert len(drafts) == 1
    assert drafts[0]["bubbleState"] == "draft"
    assert drafts[0]["turnPending"] is True
    assert drafts[0]["text"] == "hello wor"

    window._append_user_message("hello world")
    _drain_events()

    finals = _user_messages(window)
    assert len(finals) == 1, (
        "final transcript must promote the existing draft, not append "
        "a second user bubble"
    )
    assert finals[0]["bubbleState"] == "sent"
    assert finals[0]["turnPending"] is False
    assert finals[0]["text"] == "hello world"


def test_user_bubble_appended_when_no_draft_existed(main_window_factory):
    """Final transcript with NO prior partial creates a fresh sent
    user bubble (the `_append_user_message` else-branch).
    """
    window = main_window_factory()

    window._append_user_message("no partial seen")
    _drain_events()

    users = _user_messages(window)
    assert len(users) == 1
    assert users[0]["bubbleState"] == "sent"
    assert users[0]["turnPending"] is False


def test_full_pipeline_ordering_stt_llm_tts(main_window_factory):
    """End-to-end ordering for one turn: user bubble first, then
    pipeline status rows in time order, then assistant bubble. Verbose
    mode so the status rows are visible.
    """
    window = main_window_factory(log_verbose=True)

    # Turn boundary into RECORDING resets per-turn flags.
    window._apply_state(AppState.RECORDING.value)
    # Short turn — no partial transcript. Status states queue.
    window._apply_state(AppState.TRANSCRIBING.value)
    # Final transcript lands → user bubble + queue flush.
    window._append_user_message("what is 2 plus 2?")
    # Pipeline continues through THINKING → SYNTHESIZING → SPEAKING.
    window._apply_state(AppState.THINKING.value)
    window._apply_state(AppState.SYNTHESIZING.value)
    window._apply_state(AppState.SPEAKING.value)
    # Assistant response lands AFTER the speaking transition.
    window._append_assistant_message("four")
    _drain_events()

    messages = _all_messages(window)
    roles_and_states = [
        (m["role"], m.get("stateName")) for m in messages
    ]

    # Expected:
    # 1. user bubble (sent)
    # 2. status row for transcribing (was queued, flushed after user bubble)
    # 3. status row for thinking
    # 4. status row for synthesizing
    # 5. status row for speaking
    # 6. assistant bubble
    assert roles_and_states == [
        ("user", None),
        ("status", "transcribing"),
        ("status", "thinking"),
        ("status", "synthesizing"),
        ("status", "speaking"),
        ("assistant", None),
    ]


# --- replayMessage paths -------------------------------------------------


def test_replay_message_synthesizes_and_forwards_to_player(main_window_factory):
    """Happy path: assistant message + TTS ready → synthesize called
    with the assistant text AND the resulting audio path forwarded
    into the replay player. The player call is the half this test
    exists to lock in — a future regression that synthesizes but
    skips the play_file dispatch must fail this test.

    `window.replay_player` is a real `AudioPlayer` (constructed inside
    `MainWindow.__init__`), so we monkeypatch its `play_file` to a
    recording stand-in. That bypasses the wave-parse step (the fake
    TTS doesn't emit valid WAV bytes) without compromising the
    "did the dispatch happen?" assertion.
    """
    window = main_window_factory()
    tts = window.tts_loader.tts_service
    tts.set_available(True)

    play_calls: list = []
    window.replay_player.play_file = lambda path: (
        play_calls.append(path) or True
    )

    window._append_assistant_message("hello user")
    window.replayMessage(0)
    _wait_until(lambda: len(play_calls) == 1)

    assert tts.synthesize_calls == ["hello user"]
    assert len(play_calls) == 1
    assert str(play_calls[0]).endswith("replay-1.wav")


def test_replay_message_skipped_when_tts_not_ready(main_window_factory):
    """Readiness check (`is_available`) gates synthesis. A not-ready
    TTS must skip synthesis silently — no error, no call.
    """
    window = main_window_factory()
    tts = window.tts_loader.tts_service
    tts.set_available(False)

    window._append_assistant_message("hello user")
    window.replayMessage(0)
    _drain_events()

    assert tts.synthesize_calls == []


def test_replay_message_ignores_non_assistant_rows(main_window_factory):
    """`replayMessage` is only meaningful for assistant rows. A
    user/status/system row must not trigger synthesis even if
    `is_available` is True.
    """
    window = main_window_factory()
    tts = window.tts_loader.tts_service
    tts.set_available(True)

    window._append_user_message("user line")
    window.replayMessage(0)
    _drain_events()

    assert tts.synthesize_calls == []


def test_replay_message_synthesis_failure_preserves_draft(main_window_factory):
    """Round-2 invariant: a replay synthesis exception must NOT discard
    the user's active draft turn. The error is logged; the draft stays.
    """
    window = main_window_factory()
    tts = window.tts_loader.tts_service
    tts.set_available(True)
    tts.synthesize_raises = RuntimeError("piper exploded")

    # User has an in-flight draft (e.g. mid-recording while reading
    # an old reply).
    window._sync_live_user_message("draft mid-typing")
    window._append_assistant_message("reply that will fail to replay")
    _drain_events()

    drafts_before = [
        m for m in _user_messages(window) if m["bubbleState"] == "draft"
    ]
    assert len(drafts_before) == 1

    # Replay the assistant message → synthesize raises.
    window.replayMessage(1)
    _wait_until(lambda: tts.synthesize_calls == ["reply that will fail to replay"])

    # Synthesis WAS attempted.
    assert tts.synthesize_calls == ["reply that will fail to replay"]
    # The draft bubble is still there, untouched.
    drafts_after = [
        m for m in _user_messages(window) if m["bubbleState"] == "draft"
    ]
    assert len(drafts_after) == 1
    assert drafts_after[0]["text"] == "draft mid-typing"


def test_replay_message_emits_failure_signal_when_tts_not_ready(
    main_window_factory,
):
    """Cycle 9: a replay click against a not-ready TTS must fire
    `replay_failed` with a non-empty reason so the QML layer can show
    a passive notification toast. The signal is the only user-visible
    feedback for this path (synthesis is silently skipped otherwise).
    """
    window = main_window_factory()
    tts = window.tts_loader.tts_service
    tts.set_available(False)

    reasons: list[str] = []
    window.replay_failed.connect(reasons.append)

    window._append_assistant_message("hello user")
    window.replayMessage(0)
    _drain_events()

    assert tts.synthesize_calls == []
    assert len(reasons) == 1
    assert reasons[0]  # non-empty


def test_replay_message_emits_failure_signal_on_synthesis_exception(
    main_window_factory,
):
    """Cycle 9: a synthesis exception must emit `replay_failed` so the
    QML toast surfaces the failure (in addition to the existing error
    banner via `_set_error_message`).
    """
    window = main_window_factory()
    tts = window.tts_loader.tts_service
    tts.set_available(True)
    tts.synthesize_raises = RuntimeError("piper exploded")

    reasons: list[str] = []
    window.replay_failed.connect(reasons.append)

    window._append_assistant_message("reply that will fail to replay")
    window.replayMessage(0)
    _wait_until(lambda: len(reasons) == 1)

    assert tts.synthesize_calls == ["reply that will fail to replay"]
    assert len(reasons) == 1
    # The exception text travels through to the toast payload.
    assert "piper exploded" in reasons[0]


# --- custom STT path selection -------------------------------------------


def test_custom_stt_path_appears_in_catalog_at_startup(
    main_window_factory, tmp_path,
):
    """A path-shaped `WHISPER_MODEL` env var (resolved into the
    transcriber at construction time) surfaces as a catalog row from
    the moment the window opens. The custom path lives alongside the
    managed catalog entries and must NOT be dropped by the startup
    `_sync_installed_selections` syncer just because it isn't a
    managed item — the syncer only reverts when the selected name
    isn't in `available_items()`, and `available_items()` returns the
    custom path when `_custom_path` is set.
    """
    # tmp_path-derived path keeps the test hermetic + portable;
    # `_is_custom_path` triggers on `Path(name).is_absolute()` which
    # an arbitrary tmp_path subdirectory satisfies.
    custom = str(tmp_path / "my-fine-tuned" / "model.bin")

    # The custom path is "on disk" (in the fake's installed set) for
    # this test scenario — without that, `sttOptions` (filtered by
    # `is_item_available`) would not include the custom row, the
    # syncer would fall back to a managed model, and `_custom_path`
    # would be cleared. Mirrors the production invariant: a
    # `WHISPER_MODEL=/path/...` only sticks if the file actually
    # exists.
    window = main_window_factory(
        stt_model_name=custom,
        installed_stt=("tiny.en", custom),
    )

    assert custom in window._stt_catalog
    assert window.model_loader.transcriber.selected_item == custom
    # `selectedSttModel` resolves through to the custom path.
    assert window.selectedSttModel == custom


def test_managed_selection_clears_custom_row_from_catalog(
    main_window_factory, tmp_path,
):
    """When the user picks a managed model after launching with a
    custom-path STT, `_handle_inventory_change` rebuilds the catalog
    without the custom row (because `_custom_path` is cleared by
    `set_model_name`). The selection_changed → catalog-refresh wire
    is what drives this; the test pins it down.
    """
    custom = str(tmp_path / "legacy-model.bin")
    window = main_window_factory(
        stt_model_name=custom,
        installed_stt=("tiny.en", "base.en", custom),
    )
    assert custom in window._stt_catalog

    # User picks a managed model via the QML slot (which goes through
    # the same `_handle_inventory_change` wire as production).
    window.selectSttModel("base.en")
    _drain_events()

    assert custom not in window._stt_catalog, (
        "switching to a managed model must drop the custom row from "
        "the catalog"
    )
    assert "base.en" in window._stt_catalog


# --- connect spam-click guard --------------------------------------------


def test_toggle_llm_connection_blocked_while_busy(
    main_window_factory, monkeypatch,
):
    """`toggleLlmServerConnection` must early-return when
    `LlmController.connection_busy` AND `server_connected` are both
    True. The guard prevents stacking disconnect requests on top of
    an in-flight connect.
    """
    window = main_window_factory()
    llm = window._llm

    # Set the busy + connected state without going through the real
    # LLM connection paths.
    monkeypatch.setattr(
        type(llm), "connection_busy", property(lambda self: True)
    )
    monkeypatch.setattr(
        type(llm), "server_connected", property(lambda self: True)
    )

    disconnect_calls: list = []
    monkeypatch.setattr(
        llm, "disconnect_server", lambda: disconnect_calls.append("disconnect")
    )
    connect_calls: list = []
    monkeypatch.setattr(
        llm,
        "connect_server",
        lambda url, show_error: connect_calls.append((url, show_error)),
    )

    window.toggleLlmServerConnection("http://localhost:1234/v1")
    _drain_events()

    assert disconnect_calls == [], (
        "toggle while busy must not invoke disconnect"
    )
    assert connect_calls == [], (
        "toggle while busy must not invoke connect"
    )


def test_disconnect_llm_blocked_while_model_busy(
    main_window_factory, monkeypatch,
):
    """The internal LLM disconnect helper must early-return when EITHER
    `connection_busy` OR `model_busy` is set — disconnecting mid-load
    would race the in-flight HTTP requests.
    """
    window = main_window_factory()
    llm = window._llm

    monkeypatch.setattr(
        type(llm), "connection_busy", property(lambda self: False)
    )
    monkeypatch.setattr(
        type(llm), "model_busy", property(lambda self: True)
    )

    disconnect_calls: list = []
    monkeypatch.setattr(
        llm, "disconnect_server", lambda: disconnect_calls.append("disconnect")
    )

    window._disconnect_llm_server()
    _drain_events()

    assert disconnect_calls == [], (
        "disconnect while model is busy must be a no-op"
    )


# --- v0.10 chat-stream wiring + context-token Q_PROPERTYs ----------------


def test_initial_context_token_values_are_zero(main_window_factory):
    """Both Q_PROPERTYs default to 0 at construction so QML can divide
    used/ceiling without a guard for "model not loaded yet".
    """
    window = main_window_factory()
    assert window.contextTokensUsed == 0
    assert window.contextTokensCeiling == 0


def test_set_thinking_expanded_forwards_to_coordinator(main_window_factory):
    """The QML expander toggle goes through MainWindow's slot to the
    coordinator's `set_thinking_expanded` (Layer 5 owns the row-level
    mutation). MainWindow is a thin forwarder.
    """
    window = main_window_factory()
    calls: list[tuple[int, bool]] = []
    # Layer 5 adds `set_thinking_expanded` to ConversationTurnCoordinator
    # in parallel; until that lands we mock it on the live instance to
    # lock the call shape MainWindow promises QML.
    window._turn_coordinator.set_thinking_expanded = (
        lambda row, expanded: calls.append((row, expanded))
    )

    window.setThinkingExpanded(3, True)
    window.setThinkingExpanded(7, False)
    _drain_events()

    assert calls == [(3, True), (7, False)]


def test_chat_thinking_chunk_forwards_to_coordinator(main_window_factory):
    """`_on_chat_thinking_chunk` threads the chunk text into the
    coordinator's streaming-thinking method and pulses
    `conversation_changed` for direct MainWindow observers.
    """
    window = main_window_factory()
    chunks: list[str] = []
    window._turn_coordinator.on_chat_thinking_chunk = chunks.append

    notifications: list[None] = []
    window.conversation_changed.connect(lambda: notifications.append(None))

    window.controller.chat_thinking_chunk.emit("step 1...")
    window.controller.chat_thinking_chunk.emit(" step 2.")
    _drain_events()

    assert chunks == ["step 1...", " step 2."]
    # At least one notification per chunk arrival; the coordinator may
    # also emit on its own, so we only require >= 2.
    assert len(notifications) >= 2


def test_chat_content_chunk_forwards_to_coordinator(main_window_factory):
    """`_on_chat_content_chunk` threads the chunk text into the
    coordinator's content-streaming method and pulses
    `conversation_changed`.
    """
    window = main_window_factory()
    chunks: list[str] = []
    window._turn_coordinator.on_chat_content_chunk = chunks.append

    notifications: list[None] = []
    window.conversation_changed.connect(lambda: notifications.append(None))

    window.controller.chat_content_chunk.emit("Hello")
    window.controller.chat_content_chunk.emit(" world!")
    _drain_events()

    assert chunks == ["Hello", " world!"]
    assert len(notifications) >= 2


def test_chat_usage_changed_sums_prompt_and_completion_tokens(
    main_window_factory,
):
    """`contextTokensUsed` is the running sum of prompt + completion
    tokens carried on the final SSE chunk's `usage` payload. Each
    `chat_usage_changed` overwrites (the LM Studio totals replace,
    they don't accumulate per chunk).
    """
    window = main_window_factory()
    notifications: list[None] = []
    window.ui_changed.connect(lambda: notifications.append(None))

    window.controller.chat_usage_changed.emit(120, 8)
    _drain_events()

    assert window.contextTokensUsed == 128
    assert len(notifications) >= 1

    # Subsequent turn replaces, not adds.
    window.controller.chat_usage_changed.emit(200, 50)
    _drain_events()

    assert window.contextTokensUsed == 250


def test_context_ceiling_fetched_when_llm_model_changes(
    main_window_factory,
):
    """When the LM Studio model selection lands a non-empty model name,
    MainWindow probes `chat_client.fetch_loaded_context_length()` off
    the GUI thread and writes the result through to
    `contextTokensCeiling`.
    """
    window = main_window_factory()
    chat_client = window.controller.chat_client
    chat_client.context_length_value = 32768
    chat_client.set_model("test-model-7b")

    # Drive the LlmController-side signal that triggers the probe.
    window._llm.selected_model_changed.emit("test-model-7b")

    # The probe runs on a worker thread and posts back via a queued
    # signal; waitUntil drains the event loop until the property
    # converges or the timeout fires.
    qtbot_timeout_ms = 2000
    deadline = time.monotonic() + qtbot_timeout_ms / 1000.0
    while time.monotonic() < deadline and window.contextTokensCeiling != 32768:
        _drain_events(times=2)

    assert chat_client.fetch_context_length_calls >= 1
    assert window.contextTokensCeiling == 32768


def test_context_ceiling_resets_to_zero_when_model_unloaded(
    main_window_factory,
):
    """Selecting the empty model (LlmController's "unload" path) drops
    the ceiling back to 0 immediately on the GUI thread — the worker
    fetch is skipped because there is no model to probe.
    """
    window = main_window_factory()
    chat_client = window.controller.chat_client

    # Seed a non-zero ceiling first so we can assert it's cleared.
    window._context_tokens_ceiling = 8192
    chat_client.fetch_context_length_calls = 0
    # Simulate the workflow precondition: a real model was loaded
    # before this unload event. Without seeding the prior selection,
    # MainWindow's same-model short-circuit (added to suppress
    # refresh-spam re-fires) would no-op the "" → "" transition.
    window._last_selected_llm_model = "previously-loaded"

    window._llm.selected_model_changed.emit("")
    _drain_events()

    assert window.contextTokensCeiling == 0
    # Empty-model path skips the worker fetch.
    assert chat_client.fetch_context_length_calls == 0


def test_context_ceiling_drops_late_result_for_stale_model(
    main_window_factory,
):
    """Late `_context_length_fetched` results for a model the user has
    already moved away from are dropped — the ceiling stays bound to
    the current selection.
    """
    window = main_window_factory()
    chat_client = window.controller.chat_client
    # Current selection is "current-model"; the late result is for
    # a previously-selected "old-model" and must be ignored.
    chat_client.set_model("current-model")
    window._context_tokens_ceiling = 0

    # Simulate the worker callback firing with stale model name.
    window._context_length_fetched.emit(16384, "old-model")
    _drain_events()

    assert window.contextTokensCeiling == 0

    # The matching-model result IS applied.
    window._context_length_fetched.emit(4096, "current-model")
    _drain_events()

    assert window.contextTokensCeiling == 4096


# --- v0.11 multi-turn history integration -----------------------------


def test_chat_history_provider_wired_to_window(main_window_factory):
    """MainWindow installs a closure on `controller.chat_history_provider`
    so the history snapshot captured before each pipeline future is
    consistent with the visible transcript at submit time."""
    window = main_window_factory()
    assert window.controller.chat_history_provider is not None
    # Snapshot should round-trip an empty conversation as just the
    # system prompt (or nothing if the chat client has no prompt).
    snapshot = window.controller.chat_history_provider()
    chat_client = window.controller.chat_client
    if chat_client.system_prompt:
        assert snapshot == [
            {"role": "system", "content": chat_client.system_prompt}
        ]
    else:
        assert snapshot == []


def test_chat_history_provider_serializes_visible_turns(main_window_factory):
    window = main_window_factory()
    coord = window._turn_coordinator
    coord.on_user_transcript("what is the capital of France?")
    coord.on_assistant_response("Paris.")
    coord.on_user_transcript("what is the population?")
    _drain_events()

    snapshot = window.controller.chat_history_provider()
    # Drop the system prompt entry (which depends on chat_client config)
    # and assert the user/assistant round-trip.
    user_assistant = [m for m in snapshot if m["role"] != "system"]
    assert user_assistant == [
        {"role": "user", "content": "what is the capital of France?"},
        {"role": "assistant", "content": "Paris."},
        {"role": "user", "content": "what is the population?"},
    ]


def test_model_switch_preserves_conversation(main_window_factory):
    """v0.11 design choice: switching loaded LLMs does NOT wipe the
    transcript. The user gets continuity (e.g. ask the same question
    to two models and compare); modern instruction-tuned local models
    handle foreign transcripts well, and a surprise wipe is the
    bigger UX cost. Only context-token state resets — that's bound
    to the new model's `loaded_context_length`."""
    window = main_window_factory()
    coord = window._turn_coordinator
    coord.on_user_transcript("u1")
    coord.on_assistant_response("a1")
    _drain_events()
    rows_before = window._conversation_model.rowCount()
    assert rows_before > 0

    # Seed a non-zero ceiling so we can assert it's reset.
    window._context_tokens_ceiling = 8192
    window._context_tokens_used = 1000

    window._llm.selected_model_changed.emit("llama")
    _drain_events()

    # Conversation persists; per-model context counters reset.
    assert window._conversation_model.rowCount() == rows_before
    assert window.contextTokensCeiling == 0
    assert window.contextTokensUsed == 0


def test_max_history_turns_propagated_to_coordinator(main_window_factory):
    """`AppConfig.max_history_turns` flows controller → coordinator
    via `set_max_history_turns` in MainWindow.__init__, so the
    coordinator's trim invariant uses the user's configured cap."""
    window = main_window_factory()
    assert (
        window._turn_coordinator._max_history_turns  # pyright: ignore[reportPrivateUsage]
        == window.controller.max_history_turns
    )


def test_same_model_repeat_does_not_reset_counters_or_refetch(
    main_window_factory,
):
    """LM Studio refresh / reconnect / URL-change paths can re-emit
    `selected_model_changed` with the SAME model name. Resetting the
    context-token counters and queueing a fresh
    `fetch_loaded_context_length` HTTP probe each time would be
    wasteful (and visibly flicker the bar). MainWindow short-circuits
    on the model-equality check."""
    window = main_window_factory()
    chat_client = window.controller.chat_client
    chat_client.context_length_value = 8192
    # Bring the window into a steady state under "test-model".
    chat_client.set_model("test-model")
    window._llm.selected_model_changed.emit("test-model")
    # Drain enough events for the worker probe to land.
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline and window.contextTokensCeiling != 8192:
        _drain_events(times=2)
    assert window.contextTokensCeiling == 8192
    fetch_calls_after_first = chat_client.fetch_context_length_calls

    # Seed visible "used" so we can detect a reset.
    window.controller.chat_usage_changed.emit(120, 8)
    _drain_events()
    assert window.contextTokensUsed == 128

    # Refresh re-emits the SAME model. Should be a no-op.
    window._llm.selected_model_changed.emit("test-model")
    _drain_events()

    assert window.contextTokensCeiling == 8192  # unchanged
    assert window.contextTokensUsed == 128       # unchanged
    assert chat_client.fetch_context_length_calls == fetch_calls_after_first


def test_speaking_row_tracks_in_pipeline_playback(main_window_factory):
    """When the in-pipeline player starts playing the just-finalized
    assistant turn, `speakingRow` updates to that row's index. Drives
    the per-bubble ▶/🤫 toggle in the conversation pane."""
    window = main_window_factory()
    coord = window._turn_coordinator
    coord.on_user_transcript("hi")
    coord.on_assistant_response("hello")
    _drain_events()
    # The assistant row is the second visible row (after the user
    # turn).
    assistant_idx = window._conversation_model.find_message_index(
        "assistant", bubble_state="sent", turn_pending=False
    )
    assert assistant_idx == 1
    assert window.speakingRow == -1

    # Simulate the controller player firing playback_started, the
    # signal MainWindow listens to.
    window.controller.player.playback_started.emit("/tmp/dummy.wav")
    _drain_events()
    assert window.speakingRow == assistant_idx

    # Playback finishes naturally → reset.
    window.controller.player.playback_finished.emit("/tmp/dummy.wav")
    _drain_events()
    assert window.speakingRow == -1


def test_speaking_row_set_immediately_on_replay_click(main_window_factory):
    """v0.11 fix: `replayMessage` sets `speakingRow` synchronously the
    moment the user clicks ▶ — NOT when `playback_started` later fires.
    The synth call now runs on a background executor (Piper takes 5-7 s
    for a typical reply); waiting for playback_started would leave the
    button reading ▶ during synth, queueing additional clicks while
    the GUI is otherwise responsive. The synchronous set lets the
    button toggle to 🤫 the same frame as the click."""
    window = main_window_factory()
    tts = window.tts_loader.tts_service
    tts.set_available(True)
    window.replay_player.play_file = lambda path: True

    window._append_assistant_message("first")
    window._append_assistant_message("second")
    _drain_events()

    window.replayMessage(1)
    # No _drain_events / _wait_until — speakingRow must be set
    # synchronously during the call, before the worker even starts.
    assert window.speakingRow == 1

    # The worker eventually completes synth + dispatches play_file.
    _wait_until(lambda: bool(tts.synthesize_calls))
    assert window.speakingRow == 1

    window.replay_player.playback_finished.emit("/tmp/replay.wav")
    _drain_events()
    assert window.speakingRow == -1


def test_replay_cancellation_during_synth_discards_audio(
    main_window_factory, tmp_path
):
    """If the user clicks 🤫 (stopSpeaking) while synth is still
    running on the executor, the synth-completion handler must see
    `speakingRow == -1` and discard the audio without dispatching it
    to the player. The audio file is unlinked. Without this guard,
    a click cascade would queue additional play_file calls that pile
    up as abandoned PortAudio workers and eventually crash the app
    (the bug the user reported)."""
    window = main_window_factory()
    tts = window.tts_loader.tts_service
    tts.set_available(True)

    # Use a synth that blocks until the test releases it, so we can
    # interleave a stopSpeaking() in between.
    synth_can_complete = threading.Event()
    real_synthesize = tts.synthesize

    def _slow_synthesize(text: str):
        synth_can_complete.wait(timeout=2.0)
        return real_synthesize(text)

    tts.synthesize = _slow_synthesize  # type: ignore[method-assign]

    play_calls: list = []
    window.replay_player.play_file = lambda path: (
        play_calls.append(path) or True
    )

    window._append_assistant_message("a long reply")
    window.replayMessage(0)
    # Synth is blocked; speakingRow already set.
    assert window.speakingRow == 0

    # User clicks 🤫 mid-synth.
    window.stopSpeaking()
    assert window.speakingRow == -1

    # Release synth. The completion handler should see the mismatch
    # and skip play_file.
    synth_can_complete.set()
    _wait_until(lambda: bool(tts.synthesize_calls))
    _drain_events()  # let the queued completion slot run.

    assert play_calls == [], (
        "play_file must NOT fire when speakingRow no longer points at "
        "this index — the synth result is stale and should be discarded"
    )
    assert window.speakingRow == -1


def test_speaking_row_resets_on_playback_failure(main_window_factory):
    """A playback_failed signal resets speakingRow even though it
    arrives with a 2-arg payload (path, message)."""
    window = main_window_factory()
    coord = window._turn_coordinator
    coord.on_user_transcript("hi")
    coord.on_assistant_response("hello")
    _drain_events()
    window.controller.player.playback_started.emit("/tmp/dummy.wav")
    _drain_events()
    assert window.speakingRow >= 0

    window.controller.player.playback_failed.emit(
        "/tmp/dummy.wav", "device gone"
    )
    _drain_events()
    assert window.speakingRow == -1


def test_stop_speaking_stops_both_players_and_resets_row(main_window_factory):
    """`stopSpeaking()` stops both `controller.player` and
    `replay_player`, and resets `speakingRow` immediately even if the
    workers don't subsequently emit a finished signal (which they
    don't, post v0.11 fix to the teardown-error path)."""
    window = main_window_factory()
    main_stops: list[None] = []
    replay_stops: list[None] = []
    window.controller.player.stop = lambda: main_stops.append(None)
    window.replay_player.stop = lambda: replay_stops.append(None)
    # Seed a non-default speaking row so we can assert the reset.
    window._speaking_row = 3

    window.stopSpeaking()
    _drain_events()

    assert len(main_stops) == 1
    assert len(replay_stops) == 1
    assert window.speakingRow == -1


def test_stop_speaking_resets_controller_playback_flags(main_window_factory):
    """v0.11 fix: when the user clicks 🤫 the playback worker's
    teardown-error suppression means `playback_finished` is NOT
    emitted, so `_playing_response` / `_aux_playback_active` would
    otherwise stay stuck `True` and gate the mic-resume callback
    forever ("Listening" label but mic not actually hot — exactly the
    user report). `controller.cancel_playbacks()` resets both flags
    explicitly."""
    window = main_window_factory()
    # Simulate the post-stuck state: mid-auto-play AND mid-replay.
    window.controller._playing_response = True
    window.controller._aux_playback_active = True

    window.stopSpeaking()
    _drain_events()

    assert window.controller._playing_response is False
    assert window.controller._aux_playback_active is False


def test_stop_speaking_schedules_input_resume_when_voice_connected(
    main_window_factory,
):
    """After the user stops speech, the mic must come back. The
    controller schedules a cooldown-deferred resume when voice
    connection is enabled — without it, the user has to manually
    toggle the mic to record again."""
    window = main_window_factory()
    schedules: list[str] = []
    window.controller._schedule_input_resume_after_cooldown = (
        lambda reason: schedules.append(reason)
    )
    # Simulate mic-on + currently-playing-something.
    window.controller._voice_connection_enabled = True
    window.controller._playing_response = True

    window.stopSpeaking()
    _drain_events()

    assert schedules == ["user_stop_speaking"]


def test_stop_speaking_does_not_schedule_resume_when_voice_off(
    main_window_factory,
):
    """When voice connection is OFF, no input-resume is scheduled —
    the user explicitly turned off the mic, the resume would defy
    that intent."""
    window = main_window_factory()
    schedules: list[str] = []
    window.controller._schedule_input_resume_after_cooldown = (
        lambda reason: schedules.append(reason)
    )
    window.controller._voice_connection_enabled = False
    window.controller._playing_response = True

    window.stopSpeaking()
    _drain_events()

    assert schedules == []


def test_speaking_row_shifts_with_history_trim(main_window_factory):
    """Round-3 #4: when the coordinator's history-cap trim drops
    rows from the FRONT of the model, MainWindow's `_speaking_row`
    must shift in lockstep. Otherwise the ▶/🤫 toggle renders on
    the wrong row after a trim fires mid-playback."""
    window = main_window_factory()
    coord = window._turn_coordinator
    coord.set_max_history_turns(4)  # last 4 entries = 2 pairs
    # Land 4 pairs (8 finalized rows). Trim doesn't fire yet because
    # the count == cap*2 only after the 2nd-to-last pair lands AND
    # the threshold is exceeded.
    for i in range(4):
        coord.on_user_transcript(f"u{i}")
        coord.on_assistant_response(f"a{i}")
    _drain_events()
    # Now the model has the last 4 rows: u2, a2, u3, a3 (2 pairs).
    assert window._conversation_model.rowCount() == 4
    # Pretend playback is currently on the LAST row (a3 — index 3).
    window._speaking_row = 3
    window._speaking_owner = "controller"

    # Land a 5th pair → trim fires, drops u2/a2 (2 rows from the front).
    coord.on_user_transcript("u4")
    coord.on_assistant_response("a4")
    _drain_events()

    # After trim, indices shifted: a3 is now at row 1 (was row 3).
    # speaking_row should track that shift.
    assert window.speakingRow == 1
    rows = [
        (window._conversation_model.message(i) or {}).get("text")
        for i in range(window._conversation_model.rowCount())
    ]
    assert rows[1] == "a3", (
        f"speaking_row=1 should still point at a3 after trim; saw {rows!r}"
    )


def test_speaking_row_resets_when_speaking_bubble_trimmed(main_window_factory):
    """Round-3 #4: if the trim drops the bubble that's currently
    speaking (e.g. cap shrunk drastically), `_speaking_row` must
    reset to -1 — there's no bubble to point at anymore."""
    window = main_window_factory()
    coord = window._turn_coordinator
    coord.set_max_history_turns(4)
    for i in range(4):
        coord.on_user_transcript(f"u{i}")
        coord.on_assistant_response(f"a{i}")
    _drain_events()
    # Speaking on the OLDEST row (u2 at index 0). The next trim will
    # drop it.
    window._speaking_row = 0
    window._speaking_owner = "replay"

    coord.on_user_transcript("u4")
    coord.on_assistant_response("a4")
    _drain_events()

    assert window.speakingRow == -1
    assert window._speaking_owner == ""


def test_main_playback_finished_does_not_clear_replay_row(main_window_factory):
    """Round-3 #9: the in-pipeline `controller.player`'s
    `playback_finished` arriving LATE (e.g. an abandoned auto-play
    worker that finally exited) must not wipe a `_speaking_row`
    that's currently owned by the replay player."""
    window = main_window_factory()
    # Simulate: replay is currently playing row 3.
    window._speaking_row = 3
    window._speaking_owner = "replay"

    # Late finish from the controller player arrives.
    window.controller.player.playback_finished.emit("/tmp/abandoned.wav")
    _drain_events()

    assert window.speakingRow == 3
    assert window._speaking_owner == "replay"


def test_replay_play_file_returning_false_resets_speaking_row(
    main_window_factory,
):
    """Round-3 #6: if `replay_player.play_file()` returns False
    (e.g. concurrent supersede), speakingRow must reset — otherwise
    the toggle stays pinned forever because no `playback_started`
    will follow."""
    window = main_window_factory()
    tts = window.tts_loader.tts_service
    tts.set_available(True)

    window.replay_player.play_file = lambda _path: False  # supersede simulation

    window._append_assistant_message("doomed reply")
    window.replayMessage(0)
    _wait_until(lambda: window.speakingRow == -1)
    assert window.speakingRow == -1
    assert window._speaking_owner == ""


def test_replay_synth_completion_after_shutdown_is_silent(
    main_window_factory,
):
    """Round-3 #10: a synth-completion signal that arrives AFTER
    `MainWindow.shutdown()` started must not call into a torn-down
    replay player. Set the shutdown flag and verify the completion
    handler short-circuits."""
    window = main_window_factory()
    tts = window.tts_loader.tts_service
    tts.set_available(True)

    play_calls: list = []
    window.replay_player.play_file = lambda path: (
        play_calls.append(path) or True
    )
    window._append_assistant_message("post-shutdown reply")
    # Force the shutdown flag BEFORE the synth completes.
    window._replay_executor_shutdown = True

    # Drive the completion handler directly — simulating a future that
    # was already in flight when shutdown flipped.
    window._replay_synth_completed.emit(
        0, (Path("/tmp/dummy.wav"), None)
    )
    _drain_events()

    assert play_calls == [], (
        "play_file must not run after _replay_executor_shutdown is set"
    )


def test_stale_replay_synth_future_is_cancelled(main_window_factory):
    """Round-3 #11: when the user spam-clicks ▶, a still-QUEUED
    prior synth future is cancelled before queueing a new one.
    `Future.cancel()` only succeeds on tasks that haven't started
    yet — so this test fires three clicks: the first is running
    (blocked), the second is queued, the third's submission cancels
    the second and queues itself. The cancelled future must report
    `cancelled() == True`."""
    window = main_window_factory()
    tts = window.tts_loader.tts_service
    tts.set_available(True)
    synth_can_complete = threading.Event()
    real = tts.synthesize

    def _slow(text: str):
        synth_can_complete.wait(timeout=3.0)
        return real(text)

    tts.synthesize = _slow  # type: ignore[method-assign]
    window.replay_player.play_file = lambda _path: True

    window._append_assistant_message("a0")
    window._append_assistant_message("a1")
    window._append_assistant_message("a2")

    # Click 1 → submit A. The single-worker executor picks it up
    # immediately and blocks inside _slow.
    window.replayMessage(0)
    future_a = window._replay_synth_future
    assert future_a is not None
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline and not future_a.running():
        _drain_events(2)
        time.sleep(0.01)
    assert future_a.running(), "first synth never started running"

    # Click 2 → cancel A (running, no-op), submit B. B sits in the
    # executor's queue waiting for A to finish.
    window.replayMessage(1)
    future_b = window._replay_synth_future
    assert future_b is not future_a

    # Click 3 → cancel B (queued, cancellation succeeds), submit C.
    window.replayMessage(2)
    future_c = window._replay_synth_future
    assert future_c is not future_b
    assert future_b.cancelled(), (
        "queued previous future must be cancelled when a fresh "
        f"replayMessage supersedes it; state was {future_b}"
    )

    # Release A so the executor can drain to C.
    synth_can_complete.set()
    _wait_until(
        lambda: future_c.done() or window.speakingRow == -1,
        timeout=3.0,
    )


def test_visible_transcript_trims_when_cap_hits(main_window_factory):
    """v0.11 invariant: the visible transcript matches what the LLM
    sees on the next call. When new pairs push past the cap, oldest
    pairs disappear from the model — they don't just get hidden from
    the LLM payload."""
    window = main_window_factory()
    coord = window._turn_coordinator
    coord.set_max_history_turns(4)  # last 4 entries = 2 pairs
    # Land 4 pairs.
    for i in range(4):
        coord.on_user_transcript(f"u{i}")
        coord.on_assistant_response(f"a{i}")
    _drain_events()
    # Cap = 4; only the last 2 pairs survive.
    assert window._conversation_model.rowCount() == 4
    rows = [
        window._conversation_model.message(i)
        for i in range(window._conversation_model.rowCount())
    ]
    texts = [(r or {}).get("text") for r in rows]
    assert texts == ["u2", "a2", "u3", "a3"]
    # And the history-provider snapshot reads the same trimmed view.
    snapshot = window.controller.chat_history_provider()
    user_assistant = [m for m in snapshot if m["role"] != "system"]
    assert user_assistant == [
        {"role": "user", "content": "u2"},
        {"role": "assistant", "content": "a2"},
        {"role": "user", "content": "u3"},
        {"role": "assistant", "content": "a3"},
    ]
