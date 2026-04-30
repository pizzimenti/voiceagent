"""v0.11 multi-turn history surface tests for `VoiceController`.

The roadmap in `roadmap.md` § v0.11 specifies three contract changes the
controller must honor:

1. Capture a conversation-history snapshot on the GUI thread (via
   `chat_history_provider`) before the executor runs the pipeline, so
   the `ConversationModel` is read while the GUI thread is still the
   sole mutator.
2. Append the new user turn AFTER the captured history, so the LLM
   sees `[history…, current-user-turn]`.
3. Fall back to a single-turn payload (system + user) when no provider
   is configured — preserves v0.10 behavior for tests that build the
   controller in isolation without a `MainWindow`.

These tests drive `_run_pipeline` directly with stub backends so the
contract is locked at the pipeline level, independent of the
`MainWindow` ↔ controller wiring.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtCore import QObject, Signal

from voiceagent.controller import VoiceController


class _FakeRecorder:
    sample_rate = 16000
    is_recording = False

    def stop(self, *, discard: bool = False) -> None:
        self.is_recording = False

    def start(self, *, segment_ready_callback=None) -> None:
        self.is_recording = True

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


class _FakeTranscriber:
    is_loaded = True
    backend_name = "Fake"
    selection_label = "Model"

    def __init__(self, transcript: str = "what is the population?") -> None:
        self._transcript = transcript

    def ensure_loaded(self) -> None:
        pass

    def transcribe(self, path: Path) -> str:
        return self._transcript


class _CapturingChatClient:
    """Records the messages list passed to `complete` and returns a
    fixed reply. Mirrors `LmStudioClient.complete`'s keyword args so
    the controller's call site needs no shape changes."""

    def __init__(
        self, response: str = "ok", system_prompt: str = "you are local"
    ) -> None:
        self.system_prompt = system_prompt
        self._response = response
        self.captured_messages: list[dict[str, str]] | None = None
        self.call_count = 0

    def complete(
        self,
        messages,
        *,
        on_content_chunk=None,
        on_thinking_chunk=None,
        on_usage=None,
    ) -> str:
        self.captured_messages = list(messages)
        self.call_count += 1
        return self._response


class _FakeTts:
    enabled = False

    def synthesize(self, text: str):
        return None


class _FakePlayer(QObject):
    playback_started = Signal(str)
    playback_finished = Signal(str)
    playback_failed = Signal(str, str)

    def stop(self) -> None:
        pass

    def play_file(self, path) -> bool:
        return True


@pytest.fixture
def make_controller(qtbot, tmp_path):
    """Builds a controller wired to a capturing chat client. Yields a
    factory that returns `(controller, chat_client, audio_path)` so
    tests can drive `_run_pipeline` directly."""

    created: list[VoiceController] = []

    def _make(
        *,
        transcript: str = "what is the population?",
        system_prompt: str = "you are local",
    ) -> tuple[VoiceController, _CapturingChatClient, Path]:
        chat = _CapturingChatClient(system_prompt=system_prompt)
        ctrl = VoiceController(
            recorder=_FakeRecorder(),
            transcriber=_FakeTranscriber(transcript=transcript),
            chat_client=chat,
            tts_service=_FakeTts(),
            player=_FakePlayer(),
        )
        created.append(ctrl)
        # An empty file is enough — the fake transcriber never reads it.
        audio_path = tmp_path / "turn.wav"
        audio_path.write_bytes(b"")
        return ctrl, chat, audio_path

    yield _make
    for ctrl in created:
        ctrl.shutdown()


def test_run_pipeline_appends_user_turn_after_history(make_controller):
    ctrl, chat, audio_path = make_controller(transcript="how big is it?")
    history_snapshot = [
        {"role": "system", "content": "you are local"},
        {"role": "user", "content": "what is the capital of France?"},
        {"role": "assistant", "content": "Paris."},
    ]
    ctrl._run_pipeline(audio_path, history_snapshot)
    assert chat.captured_messages == [
        {"role": "system", "content": "you are local"},
        {"role": "user", "content": "what is the capital of France?"},
        {"role": "assistant", "content": "Paris."},
        {"role": "user", "content": "how big is it?"},
    ]


def test_run_pipeline_falls_back_to_system_plus_user_when_no_history(make_controller):
    """Single-turn safety net: no provider wired (e.g. controller-only
    tests with no MainWindow) → payload mirrors v0.10 single-turn shape."""
    ctrl, chat, audio_path = make_controller(transcript="hi")
    ctrl._run_pipeline(audio_path, None)
    assert chat.captured_messages == [
        {"role": "system", "content": "you are local"},
        {"role": "user", "content": "hi"},
    ]


def test_run_pipeline_omits_system_when_chat_client_has_no_prompt(make_controller):
    """If the chat client carries no system prompt AND no history is
    provided, the fall-back payload skips the system entry — never
    posts an empty-string system message."""
    ctrl, chat, audio_path = make_controller(transcript="hi", system_prompt="")
    ctrl._run_pipeline(audio_path, None)
    assert chat.captured_messages == [{"role": "user", "content": "hi"}]


def test_run_pipeline_does_not_mutate_caller_history(make_controller):
    """The provider hands a list to the controller; the executor must
    NOT mutate it in place — appending a current-turn user message to
    the snapshot would corrupt the shared list if the controller
    reused the same provider closure across turns. Use a deep copy
    so an in-place edit to any nested dict still trips the assertion
    (a shallow `list(history)` would silently miss it)."""
    import copy

    ctrl, _chat, audio_path = make_controller(transcript="follow up")
    history = [
        {"role": "system", "content": "you are local"},
        {"role": "user", "content": "earlier"},
        {"role": "assistant", "content": "earlier reply"},
    ]
    snapshot_before = copy.deepcopy(history)
    ctrl._run_pipeline(audio_path, history)
    assert history == snapshot_before


def test_run_pipeline_skips_chat_call_on_empty_transcript(make_controller):
    """An empty Whisper transcript short-circuits the pipeline (v0.9.14
    behavior). The chat client must not be invoked at all — no
    spurious turn lands in history just because the user recorded
    silence."""
    ctrl, chat, audio_path = make_controller(transcript="   ")
    result = ctrl._run_pipeline(audio_path, [])
    assert chat.call_count == 0
    assert result.transcript == ""
    assert result.response == ""


def test_chat_history_provider_default_none_keeps_v0_10_behavior(make_controller):
    ctrl, _chat, _audio = make_controller()
    assert ctrl.chat_history_provider is None
    assert ctrl.max_history_turns == 20


# --- AppConfig env wiring --------------------------------------------


def test_app_config_max_history_turns_default(monkeypatch):
    """Default cap is 20 (10 user/assistant pairs) when no env override."""
    from voiceagent.config import AppConfig

    monkeypatch.delenv("VOICEAGENT_MAX_HISTORY_TURNS", raising=False)
    config = AppConfig.from_env()
    assert config.max_history_turns == 20


def test_app_config_max_history_turns_env_override(monkeypatch):
    from voiceagent.config import AppConfig

    monkeypatch.setenv("VOICEAGENT_MAX_HISTORY_TURNS", "6")
    config = AppConfig.from_env()
    assert config.max_history_turns == 6


def test_app_config_max_history_turns_invalid_falls_back_to_default(monkeypatch):
    from voiceagent.config import AppConfig

    monkeypatch.setenv("VOICEAGENT_MAX_HISTORY_TURNS", "not-a-number")
    config = AppConfig.from_env()
    assert config.max_history_turns == 20


def test_app_config_max_history_turns_clamps_negative_to_zero(monkeypatch):
    from voiceagent.config import AppConfig

    monkeypatch.setenv("VOICEAGENT_MAX_HISTORY_TURNS", "-5")
    config = AppConfig.from_env()
    assert config.max_history_turns == 0


# --- AppConfig.lm_studio_load_timeout_seconds ---------------------------


def test_app_config_lm_studio_load_timeout_default(monkeypatch):
    from voiceagent.config import AppConfig

    monkeypatch.delenv("LM_STUDIO_LOAD_TIMEOUT_SECONDS", raising=False)
    config = AppConfig.from_env()
    assert config.lm_studio_load_timeout_seconds == 300


def test_app_config_lm_studio_load_timeout_env_override(monkeypatch):
    from voiceagent.config import AppConfig

    monkeypatch.setenv("LM_STUDIO_LOAD_TIMEOUT_SECONDS", "120")
    config = AppConfig.from_env()
    assert config.lm_studio_load_timeout_seconds == 120


def test_app_config_lm_studio_load_timeout_invalid_falls_back(monkeypatch):
    from voiceagent.config import AppConfig

    monkeypatch.setenv("LM_STUDIO_LOAD_TIMEOUT_SECONDS", "not-a-number")
    config = AppConfig.from_env()
    assert config.lm_studio_load_timeout_seconds == 300
