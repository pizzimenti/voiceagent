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
from pathlib import Path
from typing import Any, Callable

# Ensure the worktree's `src/` resolves before any editable install.
_HERE = Path(__file__).resolve().parent
_SRC = _HERE.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

# Headless Qt for any environment that didn't already set this.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QCoreApplication, QObject, Signal

from voiceagent.controller import VoiceController
from voiceagent.model_loader import WhisperModelLoader
from voiceagent.models import AppState
from voiceagent.tts_loader import TtsVoiceLoader


# --- test doubles --------------------------------------------------------


class _FakeRecorder:
    """Minimal MicrophoneRecorder stand-in."""

    def __init__(self) -> None:
        self.sample_rate = 16000
        self.is_recording = False

    def start(self, *, segment_ready_callback=None) -> None:
        self.is_recording = True

    def stop(self, *, discard: bool = False) -> None:
        self.is_recording = False

    def take_pending_segment(self):
        return None

    def snapshot_active_segment(self):
        return None

    def force_finalize_active_segment(self, reason: str) -> bool:
        return False

    def suspend_input(self) -> None:
        pass

    def resume_input(self, warmup_seconds: float = 0.0, reason: str = "") -> None:
        pass


class _FakeChatClient:
    """LmStudioClient stand-in covering the surface LlmController reads."""

    def __init__(self) -> None:
        self.base_url = ""
        self.model = ""

    def complete(self, text: str) -> str:
        return ""

    def set_base_url(self, url: str) -> None:
        self.base_url = self.normalize_base_url(url)

    def set_model(self, model: str) -> None:
        self.model = model

    @staticmethod
    def normalize_base_url(value: str) -> str:
        return (value or "").strip().rstrip("/")


class _FakePlayer(QObject):
    """AudioPlayer signal-shape stand-in."""

    playback_started = Signal(str)
    playback_finished = Signal(str)
    playback_failed = Signal(str, str)

    def __init__(self) -> None:
        super().__init__()
        self.played_paths: list[Path] = []

    def stop(self) -> None:
        pass

    def play_file(self, path) -> bool:
        self.played_paths.append(path)
        return True

    def set_muted(self, muted: bool) -> None:
        self._muted = muted


class _FakeTranscriber:
    """`SpeechToTextBackend` stand-in covering both protocol surfaces:
    the basic backend interface AND the WhisperTranscriber-specific
    methods MainWindow's catalog adapter calls.
    """

    backend_name = "Fake-Whisper"
    selection_label = "Model"
    is_loaded = True

    MODEL_REPOSITORIES: dict[str, str] = {
        "tiny.en": "Systran/faster-whisper-tiny.en",
        "base.en": "Systran/faster-whisper-base.en",
    }

    def __init__(self, model_root: Path, model_name: str = "tiny.en") -> None:
        self.model_root = model_root
        self.model_name = model_name
        self._installed: set[str] = set()
        self._custom_path: str | None = (
            model_name if self._is_custom_path(model_name) else None
        )

    @classmethod
    def _is_custom_path(cls, name: str | None) -> bool:
        if not name:
            return False
        if name in cls.MODEL_REPOSITORIES:
            return False
        return "/" in name or name.startswith("~") or Path(name).is_absolute()

    @classmethod
    def available_model_names(cls) -> list[str]:
        return list(cls.MODEL_REPOSITORIES.keys())

    def available_items(self) -> list[str]:
        names = self.available_model_names()
        if self._custom_path:
            names.append(self._custom_path)
        return names

    @property
    def selected_item(self) -> str:
        return self.model_name

    @property
    def is_available(self) -> bool:
        return self.is_item_available(self.model_name)

    def set_model_name(self, model_name: str) -> None:
        if self.model_name == model_name:
            return
        self.model_name = model_name
        self._custom_path = (
            model_name if self._is_custom_path(model_name) else None
        )

    def set_selected_item(self, item_name: str) -> None:
        self.set_model_name(item_name)

    def is_item_available(self, name: str) -> bool:
        return name in self._installed

    def is_item_managed(self, name: str) -> bool:
        return name in self.MODEL_REPOSITORIES

    def is_item_downloadable(self, name: str) -> bool:
        return self.is_item_managed(name)

    def download_item(self, name: str, progress_callback=None) -> None:
        self._installed.add(name)

    def remove_item(self, name: str) -> None:
        self._installed.discard(name)

    def artifact_paths(self, name: str) -> list[Path]:
        return []

    def ensure_loaded(self) -> None:
        pass

    def transcribe(self, path: Path) -> str:
        return ""


class _FakeTts:
    """`TextToSpeechBackend` stand-in covering the surface MainWindow
    + TtsVoiceLoader expect.
    """

    backend_name = "Fake-Piper"
    selection_label = "Voice"

    def __init__(self, model_root: Path) -> None:
        self.model_root = model_root
        self.command = ["piper"]
        self.model_path: str | None = None
        self._installed: set[str] = set()
        self._available_flag = False
        # Counters for replay-path tests.
        self.synthesize_calls: list[str] = []
        # If set, synthesize raises with this exception.
        self.synthesize_raises: Exception | None = None

    # -- properties ----------------------------------------------------

    @property
    def enabled(self) -> bool:
        return bool(self.command and self.model_path)

    @property
    def is_available(self) -> bool:
        return self._available_flag

    def set_available(self, value: bool) -> None:
        self._available_flag = value

    @property
    def selected_item(self) -> str | None:
        return self.model_path

    def set_selected_item(self, item_name: str | None) -> None:
        self.model_path = item_name

    # -- catalog protocol ---------------------------------------------

    def available_items(self) -> list[str]:
        return sorted(self._installed)

    def is_item_available(self, name: str) -> bool:
        return name in self._installed

    def is_item_managed(self, name: str) -> bool:
        return True

    def is_item_downloadable(self, name: str) -> bool:
        return True

    def download_item(self, name: str, progress_callback=None) -> None:
        self._installed.add(name)

    def remove_item(self, name: str) -> None:
        self._installed.discard(name)

    def artifact_paths(self, name: str) -> list[Path]:
        return []

    def refresh_catalog(self) -> list[str]:
        return self.available_items()

    def describe_selection_state(self) -> dict:
        return {
            "selected_model": self.model_path or "",
            "available": self._available_flag,
            "can_download": False,
            "resolved_model_path": "",
            "direct_candidate": "",
            "local_candidate": "",
            "onnx_candidate": "",
            "json_candidate": "",
        }

    # -- synthesis ----------------------------------------------------

    def synthesize(self, text: str, progress_callback=None) -> Path | None:
        self.synthesize_calls.append(text)
        if self.synthesize_raises is not None:
            raise self.synthesize_raises
        out = self.model_root / f"replay-{len(self.synthesize_calls)}.wav"
        out.write_bytes(b"fake-wav")
        return out


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


class _StubQmlEngine:
    """Stand-in for QQmlApplicationEngine — no-op load(); returns a
    non-empty rootObjects() so MainWindow's load-failure check passes.
    """

    def __init__(self) -> None:
        self._root = _StubQmlWindow()

    def setInitialProperties(self, props) -> None:
        pass

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

        transcriber = _FakeTranscriber(
            model_root=tmp_path, model_name=stt_model_name
        )
        # Pre-install requested models so `_sync_installed_selections`
        # has something to anchor on. Without at least one installed
        # managed model, MainWindow's startup syncer falls back to the
        # first installed entry — which would clobber a custom-path
        # selection set up at construction time.
        for name in installed_stt:
            transcriber.download_item(name)

        tts_service = _FakeTts(model_root=tmp_path)

        controller = VoiceController(
            recorder=_FakeRecorder(),
            transcriber=transcriber,
            chat_client=_FakeChatClient(),
            tts_service=tts_service,
            player=_FakePlayer(),
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
    assert window._current_turn_user_bubble_present is True

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
    assert window._pending_status_log_states == ["transcribing", "thinking"]

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
    assert window._pending_status_log_states == []


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
    _drain_events()

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
    _drain_events()

    # Synthesis WAS attempted.
    assert tts.synthesize_calls == ["reply that will fail to replay"]
    # The draft bubble is still there, untouched.
    drafts_after = [
        m for m in _user_messages(window) if m["bubbleState"] == "draft"
    ]
    assert len(drafts_after) == 1
    assert drafts_after[0]["text"] == "draft mid-typing"


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
    """`disconnectLlmServer` must early-return when EITHER
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

    window.disconnectLlmServer()
    _drain_events()

    assert disconnect_calls == [], (
        "disconnect while model is busy must be a no-op"
    )
