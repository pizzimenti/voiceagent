"""Tests for the v0.11 conversation log: session-rotation + per-turn
content capture.

The conversation log lives at `$XDG_STATE_HOME/voiceagent/logs/conversation.log`
and rotates by SESSION (each app launch shifts the previous file to
`.1`, drops the oldest beyond `CONVERSATION_BACKUP_COUNT`). It captures
the full `messages` list shipped to LM Studio per turn, the assistant
response, token usage, and per-turn lifecycle events (trim, model
swap). Tests:

- `rotate_conversation_log` shifts files in the right order, drops
  the overflow oldest, and is a safe no-op when nothing exists yet.
- The controller emits the documented per-turn lines into the
  conversation logger when `_run_pipeline` runs end-to-end.
- The coordinator emits a `trim` line when the cap fires.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from voiceagent.logging_utils import (
    CONVERSATION_BACKUP_COUNT,
    CONVERSATION_LOGGER_NAME,
    rotate_conversation_log,
)


# --- session rotation ---------------------------------------------------


def test_rotate_conversation_log_no_op_when_missing(tmp_path: Path) -> None:
    log_path = tmp_path / "conversation.log"
    rotate_conversation_log(log_path)
    # Nothing existed; nothing should appear.
    assert list(tmp_path.iterdir()) == []


def test_rotate_conversation_log_shifts_existing_to_dot_one(tmp_path: Path) -> None:
    log_path = tmp_path / "conversation.log"
    log_path.write_text("session A")
    rotate_conversation_log(log_path)
    # Current file is gone; .1 carries the prior content.
    assert not log_path.exists()
    assert (tmp_path / "conversation.log.1").read_text() == "session A"


def test_rotate_conversation_log_shifts_chain(tmp_path: Path) -> None:
    log_path = tmp_path / "conversation.log"
    # Pre-populate a full chain of session backups.
    for n in range(1, CONVERSATION_BACKUP_COUNT + 1):
        (tmp_path / f"conversation.log.{n}").write_text(f"session {n}")
    log_path.write_text("session 0")  # current

    rotate_conversation_log(log_path)

    # The oldest (CONVERSATION_BACKUP_COUNT) gets overwritten by the
    # one before it; the rest each shift one slot older.
    assert not log_path.exists()
    assert (tmp_path / "conversation.log.1").read_text() == "session 0"
    for n in range(2, CONVERSATION_BACKUP_COUNT + 1):
        assert (tmp_path / f"conversation.log.{n}").read_text() == (
            f"session {n - 1}"
        )
    # The would-be (BACKUP_COUNT + 1)th never existed and shouldn't
    # appear after rotation.
    assert not (tmp_path / f"conversation.log.{CONVERSATION_BACKUP_COUNT + 1}").exists()


def test_rotate_conversation_log_drops_overflow_when_chain_already_full(
    tmp_path: Path,
) -> None:
    """When the chain is already at capacity, the would-be overflow
    `.{N+1}` slot should not appear — rotation drops the oldest
    silently rather than letting backups accumulate forever."""
    log_path = tmp_path / "conversation.log"
    log_path.write_text("current")
    for n in range(1, CONVERSATION_BACKUP_COUNT + 1):
        (tmp_path / f"conversation.log.{n}").write_text(f"backup-{n}")
    rotate_conversation_log(log_path)
    overflow = tmp_path / f"conversation.log.{CONVERSATION_BACKUP_COUNT + 1}"
    assert not overflow.exists()


def test_rotate_conversation_log_with_gaps(tmp_path: Path) -> None:
    """Sparse chain: only some `.N` exist. Rotation should still shift
    each existing one and not create empty slots."""
    log_path = tmp_path / "conversation.log"
    log_path.write_text("current")
    (tmp_path / "conversation.log.2").write_text("backup-2")
    (tmp_path / "conversation.log.4").write_text("backup-4")

    rotate_conversation_log(log_path)

    assert (tmp_path / "conversation.log.1").read_text() == "current"
    # .2 didn't exist before, so .1->.2 was a no-op for the prior .1
    # (which also didn't exist). The current file shifts to .1.
    assert not (tmp_path / "conversation.log.2").exists()
    # The old .2 shifted to .3.
    assert (tmp_path / "conversation.log.3").read_text() == "backup-2"
    # The old .4 shifted to .5.
    assert (tmp_path / "conversation.log.5").read_text() == "backup-4"


# --- controller emits the documented lines ------------------------------


class _FakeTranscriber:
    is_loaded = True
    backend_name = "Fake"
    selection_label = "Model"

    def __init__(self, transcript: str) -> None:
        self._transcript = transcript

    def ensure_loaded(self) -> None:
        pass

    def transcribe(self, _path: Path) -> str:
        return self._transcript


class _FakeChatClient:
    system_prompt = "you are local"

    def __init__(self, response: str = "ok") -> None:
        self._response = response

    def complete(
        self,
        messages,  # noqa: ARG002 — kept for shape parity
        *,
        on_content_chunk=None,
        on_thinking_chunk=None,
        on_usage=None,
    ) -> str:
        if on_usage is not None:
            on_usage({"prompt_tokens": 80, "completion_tokens": 12})
        return self._response


class _FakeTts:
    enabled = False

    def synthesize(self, _text: str):
        return None


@pytest.fixture
def conversation_log_capture(qtbot, caplog):
    """Captures records emitted to the dedicated conversation logger."""
    caplog.set_level(logging.INFO, logger=CONVERSATION_LOGGER_NAME)
    return caplog


def test_controller_emits_conversation_log_for_full_turn(
    conversation_log_capture, tmp_path: Path
) -> None:
    from PySide6.QtCore import QObject, Signal

    from voiceagent.controller import VoiceController

    class _FakePlayer(QObject):
        playback_started = Signal(str)
        playback_finished = Signal(str)
        playback_failed = Signal(str, str)

        def stop(self) -> None:  # pragma: no cover - unused on this path
            pass

        def play_file(self, _path) -> bool:  # pragma: no cover
            return True

    class _FakeRecorder:
        sample_rate = 16000
        is_recording = False

    ctrl = VoiceController(
        recorder=_FakeRecorder(),
        transcriber=_FakeTranscriber("how big is Paris?"),
        chat_client=_FakeChatClient(response="About 2.1 million."),
        tts_service=_FakeTts(),
        player=_FakePlayer(),
    )
    audio_path = tmp_path / "turn.wav"
    audio_path.write_bytes(b"")
    history = [
        {"role": "system", "content": "you are local"},
        {"role": "user", "content": "what is the capital of France?"},
        {"role": "assistant", "content": "Paris."},
    ]
    try:
        ctrl._run_pipeline(audio_path, history)
    finally:
        ctrl.shutdown()

    text = "\n".join(
        record.getMessage()
        for record in conversation_log_capture.records
        if record.name == CONVERSATION_LOGGER_NAME
    )
    # The full turn surface should land in the conversation log.
    assert "turn-start" in text
    assert "how big is Paris?" in text
    assert "turn-messages count=4" in text
    assert "what is the capital of France?" in text
    assert "About 2.1 million." in text
    assert "turn-response" in text
    assert "turn-usage prompt_tokens=80 completion_tokens=12" in text


def test_controller_logs_empty_transcript_skip(
    conversation_log_capture, tmp_path: Path
) -> None:
    from PySide6.QtCore import QObject, Signal

    from voiceagent.controller import VoiceController

    class _FakePlayer(QObject):
        playback_started = Signal(str)
        playback_finished = Signal(str)
        playback_failed = Signal(str, str)

        def stop(self) -> None:  # pragma: no cover
            pass

        def play_file(self, _path) -> bool:  # pragma: no cover
            return True

    class _FakeRecorder:
        sample_rate = 16000
        is_recording = False

    ctrl = VoiceController(
        recorder=_FakeRecorder(),
        transcriber=_FakeTranscriber("   "),
        chat_client=_FakeChatClient(),
        tts_service=_FakeTts(),
        player=_FakePlayer(),
    )
    audio_path = tmp_path / "turn.wav"
    audio_path.write_bytes(b"")
    try:
        ctrl._run_pipeline(audio_path, [])
    finally:
        ctrl.shutdown()
    text = "\n".join(
        record.getMessage()
        for record in conversation_log_capture.records
        if record.name == CONVERSATION_LOGGER_NAME
    )
    assert "turn-skipped reason=empty-transcript" in text


# --- coordinator emits trim line ----------------------------------------


def test_coordinator_logs_trim_event(conversation_log_capture, qtbot) -> None:
    from voiceagent.conversation_model import ConversationModel
    from voiceagent.conversation_turn_coordinator import (
        ConversationTurnCoordinator,
    )

    model = ConversationModel()
    coord = ConversationTurnCoordinator(model, verbose_mode=False)
    coord.set_max_history_turns(2)  # keep last 1 pair
    # 3 pairs → 1 trim, dropping the first 2 rows.
    for i in range(3):
        coord.on_user_transcript(f"u{i}")
        coord.on_assistant_response(f"a{i}")
    text = "\n".join(
        record.getMessage()
        for record in conversation_log_capture.records
        if record.name == CONVERSATION_LOGGER_NAME
    )
    assert "trim dropped_rows=2 cap=2" in text
