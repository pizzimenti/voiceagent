"""Unit tests for `ConversationTurnCoordinator` in isolation.

Cycle 6 extracted the per-turn ordering policy out of `MainWindow`
into a standalone QObject. These tests cover the coordinator's
contract directly — no `MainWindow`, no fake controller / loaders —
so a regression in the ordering invariants surfaces here before it
reaches the integration tests.

Each test builds a real `ConversationModel` + a real
`ConversationTurnCoordinator` and observes the
`conversation_changed` signal via `QSignalSpy`. Mutation results are
asserted against the model's row contents.

The companion file is
`tests/test_mainwindow_integration.py`, which exercises the same
invariants end-to-end against a real `MainWindow`. Both must stay
green; if only one fails, the failure tells you whether the
regression is in the coordinator (this file) or in the
`MainWindow` ↔ coordinator wiring (integration file).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Resolve the worktree's `src/` before any editable install.
_HERE = Path(__file__).resolve().parent
_SRC = _HERE.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtTest import QSignalSpy

from voiceagent.conversation_model import ConversationModel
from voiceagent.conversation_turn_coordinator import ConversationTurnCoordinator
from voiceagent.models import AppState


# --- helpers -------------------------------------------------------------


def _all_rows(model: ConversationModel) -> list[dict]:
    return [model.message(i) for i in range(model.rowCount())]


def _roles(model: ConversationModel) -> list[str]:
    return [(model.message(i) or {}).get("role") for i in range(model.rowCount())]


def _state_names(model: ConversationModel) -> list[str | None]:
    return [
        (model.message(i) or {}).get("stateName")
        for i in range(model.rowCount())
    ]


# --- fixtures ------------------------------------------------------------


@pytest.fixture
def model_and_coordinator(qtbot):
    """Build a real model + coordinator with a deterministic clock.

    `verbose_mode` defaults to True (most tests want to observe
    pipeline status rows). Tests that need simple-mode behavior pass
    `verbose=False` to the inner factory.
    """
    created: list = []

    def _make(
        *,
        verbose: bool = True,
        verbose_provider=None,
    ) -> tuple[ConversationModel, ConversationTurnCoordinator]:
        model = ConversationModel()
        gate = verbose_provider if verbose_provider is not None else verbose
        coordinator = ConversationTurnCoordinator(
            model,
            verbose_mode=gate,
            clock_time=lambda: "00:00:00",
        )
        created.append((model, coordinator))
        return model, coordinator

    yield _make

    # No explicit teardown needed — both objects are pure Qt and get
    # collected at scope end.
    created.clear()


# --- queue + flush -------------------------------------------------------


def test_status_rows_queue_until_user_bubble_lands(model_and_coordinator):
    """Round-2 ordering invariant: TRANSCRIBING/THINKING fired with
    no user bubble must NOT insert status rows. They queue, then
    flush AFTER the user bubble materializes via
    `on_user_transcript`.
    """
    model, coord = model_and_coordinator(verbose=True)

    coord.on_state_changed(AppState.RECORDING.value)
    coord.on_state_changed(AppState.TRANSCRIBING.value)
    coord.on_state_changed(AppState.THINKING.value)

    # Conversation must still be empty.
    assert _all_rows(model) == []
    assert coord.pending_status_log_states == ["transcribing", "thinking"]
    assert coord.current_turn_user_bubble_present is False

    # Final transcript arrives → user bubble materializes → queued
    # statuses flush in order, AFTER the user row.
    coord.on_user_transcript("hello there")

    rows = _all_rows(model)
    assert rows[0]["role"] == "user"
    assert rows[0]["text"] == "hello there"
    assert rows[0]["bubbleState"] == "sent"
    assert rows[0]["turnPending"] is False
    state_names = [r["stateName"] for r in rows[1:] if r["role"] == "status"]
    assert state_names == ["transcribing", "thinking"]
    assert coord.pending_status_log_states == []
    assert coord.current_turn_user_bubble_present is True


def test_status_row_emits_immediately_when_draft_present(model_and_coordinator):
    """A draft user bubble (created by `on_live_transcript`) is the
    turn anchor. Subsequent verbose status states emit immediately —
    no queue.
    """
    model, coord = model_and_coordinator(verbose=True)

    coord.on_state_changed(AppState.RECORDING.value)
    coord.on_live_transcript("partial trans")
    assert coord.current_turn_user_bubble_present is True

    coord.on_state_changed(AppState.TRANSCRIBING.value)

    rows = _all_rows(model)
    # Draft was promoted to 'sent' (still turnPending), then status
    # row landed after.
    assert rows[0]["role"] == "user"
    assert rows[0]["bubbleState"] == "sent"
    assert rows[0]["turnPending"] is True
    assert rows[1]["role"] == "status"
    assert rows[1]["stateName"] == "transcribing"


# --- verbose-mode gate ---------------------------------------------------


def test_simple_mode_drops_pending_status_silently(model_and_coordinator):
    """verbose=False: pipeline status states must never reach the
    transcript, even when they would otherwise have been queued.
    """
    model, coord = model_and_coordinator(verbose=False)

    coord.on_state_changed(AppState.RECORDING.value)
    coord.on_state_changed(AppState.TRANSCRIBING.value)
    coord.on_state_changed(AppState.THINKING.value)
    coord.on_user_transcript("simple-mode turn")
    coord.on_assistant_response("response")

    assert _roles(model) == ["user", "assistant"]


def test_verbose_to_simple_toggle_mid_turn_drops_queue(model_and_coordinator):
    """Mid-turn verbose→simple toggle: queued entries (deferred from
    when verbose was on) must drain silently rather than leak.
    """
    verbose_state = {"value": True}
    model, coord = model_and_coordinator(
        verbose_provider=lambda: verbose_state["value"]
    )

    coord.on_state_changed(AppState.RECORDING.value)
    coord.on_state_changed(AppState.TRANSCRIBING.value)
    coord.on_state_changed(AppState.THINKING.value)
    assert coord.pending_status_log_states == ["transcribing", "thinking"]

    # Toggle to simple while statuses are still queued.
    verbose_state["value"] = False

    coord.on_user_transcript("turn after toggle")

    assert "status" not in _roles(model)
    assert coord.pending_status_log_states == []


def test_set_verbose_mode_static_path_round_trip(model_and_coordinator):
    """When constructed with a static bool, `set_verbose_mode` flips
    the gate.
    """
    model, coord = model_and_coordinator(verbose=False)
    assert coord.verbose_mode is False

    coord.set_verbose_mode(True)
    assert coord.verbose_mode is True

    coord.on_state_changed(AppState.RECORDING.value)
    coord.on_live_transcript("draft text")
    coord.on_state_changed(AppState.TRANSCRIBING.value)

    # Verbose now → status row emits immediately.
    rows = _all_rows(model)
    assert rows[1]["role"] == "status"


def test_set_verbose_mode_noop_in_callable_path(model_and_coordinator):
    """Coordinators built with a callable provider treat
    `set_verbose_mode` as a no-op — the source of truth is the
    callable. Test that calling it doesn't raise and doesn't shadow
    the live read.
    """
    flag = {"value": True}
    model, coord = model_and_coordinator(
        verbose_provider=lambda: flag["value"]
    )
    assert coord.verbose_mode is True

    coord.set_verbose_mode(False)  # no-op in callable mode
    # Live source still wins.
    assert coord.verbose_mode is True

    flag["value"] = False
    assert coord.verbose_mode is False


# --- dedupe --------------------------------------------------------------


def test_consecutive_identical_status_states_emit_once(model_and_coordinator):
    """Dedupe: repeated identical state markers don't double-emit."""
    model, coord = model_and_coordinator(verbose=True)

    coord.on_state_changed(AppState.RECORDING.value)
    coord.on_user_transcript("anchor")  # turn anchor present

    coord.on_state_changed(AppState.THINKING.value)
    coord.on_state_changed(AppState.THINKING.value)
    coord.on_state_changed(AppState.THINKING.value)

    state_names = [
        n for n in _state_names(model) if n is not None
    ]
    assert state_names == ["thinking"]


def test_dedupe_resets_on_recording_boundary(model_and_coordinator):
    """A turn-boundary RECORDING/IDLE clears the dedupe so the next
    turn's first marker emits even if it matches the previous turn's
    last marker.
    """
    model, coord = model_and_coordinator(verbose=True)

    # Turn 1.
    coord.on_state_changed(AppState.RECORDING.value)
    coord.on_user_transcript("first turn")
    coord.on_state_changed(AppState.THINKING.value)

    # Turn boundary.
    coord.on_state_changed(AppState.IDLE.value)
    # Turn 2 — re-enters THINKING, which would be deduped without
    # the boundary reset.
    coord.on_state_changed(AppState.RECORDING.value)
    coord.on_user_transcript("second turn")
    coord.on_state_changed(AppState.THINKING.value)

    state_names = [
        n for n in _state_names(model) if n is not None
    ]
    assert state_names == ["thinking", "thinking"]


# --- per-turn reset ------------------------------------------------------


def test_recording_boundary_clears_pending_queue(model_and_coordinator):
    """RECORDING entering with a pending queue from a prior turn
    must clear the queue so the new turn starts clean.
    """
    model, coord = model_and_coordinator(verbose=True)

    coord.on_state_changed(AppState.TRANSCRIBING.value)
    assert coord.pending_status_log_states == ["transcribing"]

    coord.on_state_changed(AppState.RECORDING.value)
    assert coord.pending_status_log_states == []
    assert coord.current_turn_user_bubble_present is False
    assert coord.last_logged_status_state is None


def test_idle_boundary_clears_pending_queue(model_and_coordinator):
    """IDLE acts as the same turn boundary as RECORDING."""
    model, coord = model_and_coordinator(verbose=True)

    coord.on_state_changed(AppState.TRANSCRIBING.value)
    coord.on_state_changed(AppState.THINKING.value)
    assert coord.pending_status_log_states == ["transcribing", "thinking"]

    coord.on_state_changed(AppState.IDLE.value)
    assert coord.pending_status_log_states == []
    assert coord.current_turn_user_bubble_present is False


# --- error / connection paths --------------------------------------------


def test_error_message_discards_draft_and_logs(model_and_coordinator):
    """`on_error_message` (default discard_draft=True) removes the
    in-flight draft user bubble AND appends an error log row.
    """
    model, coord = model_and_coordinator(verbose=True)

    coord.on_state_changed(AppState.RECORDING.value)
    coord.on_live_transcript("draft mid-typing")
    assert _roles(model) == ["user"]

    coord.on_error_message("STT exploded")

    rows = _all_rows(model)
    # Draft removed, error log appended.
    assert _roles(model) == ["system"]
    assert rows[0]["level"] == "error"
    assert rows[0]["text"] == "STT exploded"


def test_error_message_preserves_draft_when_discard_false(model_and_coordinator):
    """The replay-path uses `discard_draft=False` — a synthesis
    failure on a prior assistant message must not tear down the
    user's active draft.
    """
    model, coord = model_and_coordinator(verbose=True)

    coord.on_live_transcript("draft mid-typing")
    coord.on_error_message("Replay failed", discard_draft=False)

    rows = _all_rows(model)
    # Draft survived; error log appended after it.
    assert _roles(model) == ["user", "system"]
    assert rows[0]["bubbleState"] == "draft"
    assert rows[1]["level"] == "error"


def test_connection_changed_disabled_discards_draft(model_and_coordinator):
    """A connection drop while a draft exists discards the draft
    (the in-flight turn is no longer reachable).
    """
    model, coord = model_and_coordinator(verbose=True)

    coord.on_live_transcript("draft mid-typing")
    assert _roles(model) == ["user"]

    coord.on_connection_changed(enabled=False)
    assert _all_rows(model) == []


def test_connection_changed_enabled_no_op(model_and_coordinator):
    """Re-enabling a connection mid-draft should NOT remove the
    draft — only `enabled=False` triggers discard.
    """
    model, coord = model_and_coordinator(verbose=True)

    coord.on_live_transcript("draft mid-typing")
    coord.on_connection_changed(enabled=True)

    assert _roles(model) == ["user"]


def test_blank_error_is_silent(model_and_coordinator):
    """An empty error string must not append a row OR discard a
    draft — `_set_error_message("")` is the canonical "clear the
    error UI text" call.
    """
    model, coord = model_and_coordinator(verbose=True)

    coord.on_live_transcript("draft mid-typing")
    coord.on_error_message("", discard_draft=True)

    # Draft survives; no log row appended.
    assert _roles(model) == ["user"]


# --- draft → final promotion (bubble lifecycle) --------------------------


def test_draft_promoted_to_sent_on_final_transcript(model_and_coordinator):
    """Partial transcript creates a draft; final transcript promotes
    the SAME row to `bubbleState='sent', turnPending=False`.
    """
    model, coord = model_and_coordinator(verbose=False)

    coord.on_live_transcript("hel")
    coord.on_live_transcript("hello wor")
    rows = _all_rows(model)
    assert len(rows) == 1
    assert rows[0]["bubbleState"] == "draft"
    assert rows[0]["turnPending"] is True
    assert rows[0]["text"] == "hello wor"

    coord.on_user_transcript("hello world")

    rows = _all_rows(model)
    assert len(rows) == 1
    assert rows[0]["bubbleState"] == "sent"
    assert rows[0]["turnPending"] is False
    assert rows[0]["text"] == "hello world"


def test_empty_user_transcript_drops_pending_bubble(model_and_coordinator):
    """No-speech outcome (v0.9.14): when `_run_pipeline` short-circuits
    on an empty Whisper transcript and emits `transcript_changed("")`,
    the coordinator must remove the bubble that was promoted to
    `sent/turnPending=true` at the TRANSCRIBING boundary. Otherwise the
    UI would show a stuck "sent" row for a turn the user didn't speak.
    """
    model, coord = model_and_coordinator(verbose=False)

    coord.on_live_transcript("partial guess")
    coord.on_state_changed(AppState.TRANSCRIBING.value)
    rows = _all_rows(model)
    assert len(rows) == 1
    assert rows[0]["turnPending"] is True
    assert rows[0]["bubbleState"] == "sent"

    coord.on_user_transcript("")  # empty Whisper result

    assert _all_rows(model) == []


def test_empty_user_transcript_no_pending_bubble_is_noop(model_and_coordinator):
    """Empty transcript with no pending bubble must not error or insert
    anything (e.g. user pressed-and-released without speaking)."""
    model, coord = model_and_coordinator(verbose=False)

    coord.on_user_transcript("")
    coord.on_user_transcript("   ")

    assert _all_rows(model) == []


def test_blank_live_transcript_removes_draft_when_not_pending(
    model_and_coordinator,
):
    """A draft with `turnPending=False` is a leftover the live
    transcript empty-clear should remove. (`turnPending=True` means
    the pipeline has already taken responsibility for finalizing it,
    so leave it alone.)
    """
    model, coord = model_and_coordinator(verbose=False)

    coord.on_live_transcript("partial")
    # Manually flip turnPending=False to mimic the post-promote state
    # where another path has already reset the flag (defensive).
    model.update_message(0, turnPending=False)

    coord.on_live_transcript("")  # blank

    assert _all_rows(model) == []


def test_assistant_response_appended_after_user(model_and_coordinator):
    """`on_assistant_response` always appends — order in the
    transcript is therefore caller-driven (callers must call after
    the user bubble has settled).
    """
    model, coord = model_and_coordinator(verbose=False)

    coord.on_user_transcript("question")
    coord.on_assistant_response("answer")

    rows = _all_rows(model)
    assert _roles(model) == ["user", "assistant"]
    assert rows[1]["replayable"] is True


# --- conversation_changed signal ----------------------------------------


def test_conversation_changed_emits_on_user_append(model_and_coordinator):
    """`QSignalSpy` confirms the coordinator emits its single
    notification signal once per mutation batch.
    """
    model, coord = model_and_coordinator(verbose=False)
    spy = QSignalSpy(coord.conversation_changed)

    coord.on_user_transcript("hello")
    assert spy.count() == 1

    coord.on_assistant_response("world")
    assert spy.count() == 2


def test_conversation_changed_emits_on_status_flush(model_and_coordinator):
    """A queued-then-flushed status row should emit
    `conversation_changed` for each row appended (one per status
    row + one for the user bubble).
    """
    model, coord = model_and_coordinator(verbose=True)
    spy = QSignalSpy(coord.conversation_changed)

    coord.on_state_changed(AppState.RECORDING.value)
    coord.on_state_changed(AppState.TRANSCRIBING.value)
    coord.on_state_changed(AppState.THINKING.value)
    # Pre-flush: nothing was appended (still queued), so no signal.
    assert spy.count() == 0

    coord.on_user_transcript("hi")
    # 1 for user append + 2 for status flushes.
    assert spy.count() == 3


# --- full-turn ordering --------------------------------------------------


def test_full_turn_pipeline_ordering(model_and_coordinator):
    """End-to-end ordering for one turn:
    user bubble → status rows in order → assistant bubble.
    """
    model, coord = model_and_coordinator(verbose=True)

    coord.on_state_changed(AppState.RECORDING.value)
    coord.on_state_changed(AppState.TRANSCRIBING.value)
    coord.on_user_transcript("what is 2 plus 2?")
    coord.on_state_changed(AppState.THINKING.value)
    coord.on_state_changed(AppState.SYNTHESIZING.value)
    coord.on_state_changed(AppState.SPEAKING.value)
    coord.on_assistant_response("four")

    rows = _all_rows(model)
    role_state = [(r["role"], r.get("stateName")) for r in rows]
    assert role_state == [
        ("user", None),
        ("status", "transcribing"),
        ("status", "thinking"),
        ("status", "synthesizing"),
        ("status", "speaking"),
        ("assistant", None),
    ]


# --- streaming chat chunks (v0.10.0) ------------------------------------


def test_content_chunk_creates_draft_assistant_row(model_and_coordinator):
    """First `on_chat_content_chunk` call creates a draft assistant
    row that snapshots `verbose_mode` into `thinkingExpanded` at
    insertion time.
    """
    model, coord = model_and_coordinator(verbose=True)

    coord.on_chat_content_chunk("Hello")

    rows = _all_rows(model)
    assert len(rows) == 1
    assert rows[0]["role"] == "assistant"
    assert rows[0]["bubbleState"] == "draft"
    assert rows[0]["text"] == "Hello"
    assert rows[0]["thinkingText"] == ""
    # verbose=True at insertion → thinkingExpanded captured as True.
    assert rows[0]["thinkingExpanded"] is True
    assert rows[0]["replayable"] is False
    assert rows[0]["turnPending"] is True
    assert rows[0]["timestampLabel"] == ""


def test_content_chunk_thinking_expanded_default_simple_mode(
    model_and_coordinator,
):
    """`thinkingExpanded` defaults to False when verbose is off at
    the moment the draft row is inserted.
    """
    model, coord = model_and_coordinator(verbose=False)

    coord.on_chat_content_chunk("hi")

    rows = _all_rows(model)
    assert rows[0]["thinkingExpanded"] is False


def test_content_chunks_append_into_same_row(model_and_coordinator):
    """Subsequent content chunks accumulate into the same row's
    `text` rather than creating new rows.
    """
    model, coord = model_and_coordinator(verbose=False)

    coord.on_chat_content_chunk("Hel")
    coord.on_chat_content_chunk("lo, ")
    coord.on_chat_content_chunk("world")

    rows = _all_rows(model)
    assert len(rows) == 1
    assert rows[0]["text"] == "Hello, world"


def test_thinking_chunks_accumulate_into_thinking_text(model_and_coordinator):
    """Thinking chunks append into `thinkingText` and leave `text`
    untouched.
    """
    model, coord = model_and_coordinator(verbose=True)

    coord.on_chat_thinking_chunk("Let me ")
    coord.on_chat_thinking_chunk("think...")

    rows = _all_rows(model)
    assert len(rows) == 1
    assert rows[0]["thinkingText"] == "Let me think..."
    assert rows[0]["text"] == ""


def test_thinking_and_content_share_same_draft_row(model_and_coordinator):
    """A turn that emits both thinking and content chunks builds a
    single draft row carrying both fields.
    """
    model, coord = model_and_coordinator(verbose=True)

    coord.on_chat_thinking_chunk("hmm... ")
    coord.on_chat_content_chunk("Sure, ")
    coord.on_chat_thinking_chunk("the user wants X. ")
    coord.on_chat_content_chunk("here's the answer.")

    rows = _all_rows(model)
    assert len(rows) == 1
    assert rows[0]["text"] == "Sure, here's the answer."
    assert rows[0]["thinkingText"] == "hmm... the user wants X. "


def test_thinking_expanded_not_overwritten_by_chunks(model_and_coordinator):
    """A user-driven `set_thinking_expanded` toggle must persist
    across subsequent chunk callbacks — chunks never re-read
    `verbose_mode` for the row's `thinkingExpanded`.
    """
    flag = {"value": True}
    model, coord = model_and_coordinator(verbose_provider=lambda: flag["value"])

    coord.on_chat_content_chunk("first")
    # User collapses it.
    coord.set_thinking_expanded(0, False)
    assert model.message(0)["thinkingExpanded"] is False

    # More chunks land. The collapse must stick.
    coord.on_chat_thinking_chunk("debug trace")
    coord.on_chat_content_chunk(" more")
    assert model.message(0)["thinkingExpanded"] is False

    # And a verbose-mode flip mid-stream must not retroactively
    # uncollapse it either.
    flag["value"] = True
    coord.on_chat_content_chunk(" still more")
    assert model.message(0)["thinkingExpanded"] is False


def test_turn_boundary_resets_streaming_pointer(model_and_coordinator):
    """After a RECORDING or IDLE state, the next chunk must create
    a fresh row rather than appending into the prior turn's bubble.
    """
    model, coord = model_and_coordinator(verbose=False)

    coord.on_chat_content_chunk("turn 1 reply")
    coord.on_state_changed(AppState.IDLE.value)

    coord.on_chat_content_chunk("turn 2 reply")
    rows = _all_rows(model)
    assert len(rows) == 2
    assert rows[0]["text"] == "turn 1 reply"
    assert rows[1]["text"] == "turn 2 reply"

    coord.on_state_changed(AppState.RECORDING.value)
    coord.on_chat_content_chunk("turn 3 reply")
    rows = _all_rows(model)
    assert len(rows) == 3
    assert rows[2]["text"] == "turn 3 reply"


def test_assistant_response_promotes_streaming_draft(model_and_coordinator):
    """When `on_assistant_response` fires after a streaming session,
    the existing draft row flips to `bubbleState='sent'` with
    `replayable=True` and the canonical final text — no new row is
    created.
    """
    model, coord = model_and_coordinator(verbose=True)

    coord.on_chat_thinking_chunk("reasoning...")
    coord.on_chat_content_chunk("partial ans")
    assert len(_all_rows(model)) == 1

    coord.on_assistant_response("final canonical answer")

    rows = _all_rows(model)
    assert len(rows) == 1
    assert rows[0]["role"] == "assistant"
    assert rows[0]["bubbleState"] == "sent"
    assert rows[0]["text"] == "final canonical answer"
    # thinkingText preserved from streaming.
    assert rows[0]["thinkingText"] == "reasoning..."
    assert rows[0]["replayable"] is True
    assert rows[0]["turnPending"] is False
    assert rows[0]["timestampLabel"] == "Received 00:00:00"
    # Pointer reset so the next turn doesn't recycle this row.
    assert coord._streaming_assistant_index == -1


def test_assistant_response_appends_when_no_streaming_draft(
    model_and_coordinator,
):
    """Legacy non-streaming path: with no draft from chunk callbacks,
    `on_assistant_response` falls back to appending a fresh sent row.
    Preserves the pre-v0.10 behavior end-to-end test relies on.
    """
    model, coord = model_and_coordinator(verbose=False)

    coord.on_user_transcript("question")
    coord.on_assistant_response("answer")

    rows = _all_rows(model)
    assert _roles(model) == ["user", "assistant"]
    assert rows[1]["bubbleState"] == "sent"
    assert rows[1]["replayable"] is True


def test_streaming_then_next_turn_creates_new_draft(model_and_coordinator):
    """End-to-end: stream → promote → state-boundary → next stream
    creates a NEW draft (i.e., promotion clears the pointer).
    """
    model, coord = model_and_coordinator(verbose=False)

    coord.on_chat_content_chunk("turn 1")
    coord.on_assistant_response("turn 1 final")
    coord.on_state_changed(AppState.RECORDING.value)
    coord.on_chat_content_chunk("turn 2")

    rows = _all_rows(model)
    assert len(rows) == 2
    assert rows[0]["bubbleState"] == "sent"
    assert rows[0]["text"] == "turn 1 final"
    assert rows[1]["bubbleState"] == "draft"
    assert rows[1]["text"] == "turn 2"


def test_empty_chunk_does_not_create_row(model_and_coordinator):
    """A bare empty-string chunk is a no-op — it must not create a
    draft row out of thin air.
    """
    model, coord = model_and_coordinator(verbose=False)

    coord.on_chat_content_chunk("")
    coord.on_chat_thinking_chunk("")

    assert _all_rows(model) == []


def test_set_thinking_expanded_updates_row(model_and_coordinator):
    """`set_thinking_expanded(row, expanded)` writes the bool through
    to the model row.
    """
    model, coord = model_and_coordinator(verbose=False)

    coord.on_chat_content_chunk("text")
    assert model.message(0)["thinkingExpanded"] is False

    coord.set_thinking_expanded(0, True)
    assert model.message(0)["thinkingExpanded"] is True

    coord.set_thinking_expanded(0, False)
    assert model.message(0)["thinkingExpanded"] is False


def test_set_thinking_expanded_invalid_row_is_noop(model_and_coordinator):
    """Out-of-range row indices must not raise."""
    model, coord = model_and_coordinator(verbose=False)

    coord.on_chat_content_chunk("text")

    # Negative.
    coord.set_thinking_expanded(-1, True)
    # Beyond rowCount.
    coord.set_thinking_expanded(99, True)

    # Existing row untouched.
    assert model.message(0)["thinkingExpanded"] is False


def test_set_thinking_expanded_non_assistant_row_is_noop(
    model_and_coordinator,
):
    """Calling on a non-assistant row (e.g., a user bubble) must not
    inject a `thinkingExpanded` field that QML can't make sense of.
    """
    model, coord = model_and_coordinator(verbose=False)

    coord.on_user_transcript("hi")
    assert _roles(model) == ["user"]

    coord.set_thinking_expanded(0, True)

    msg = model.message(0)
    # Either the field is absent, or — if any defensive code
    # initialized one — it stays at its pre-call value. The contract
    # is that the call is a no-op for non-assistant rows.
    assert "thinkingExpanded" not in msg or msg.get("thinkingExpanded") is None


def test_streaming_chunks_emit_conversation_changed(model_and_coordinator):
    """Each accepted chunk emits the single coordinator notify
    signal so QML can debounce on a stable counter.
    """
    model, coord = model_and_coordinator(verbose=False)
    spy = QSignalSpy(coord.conversation_changed)

    coord.on_chat_content_chunk("a")
    coord.on_chat_content_chunk("b")
    coord.on_chat_thinking_chunk("c")
    # 3 emits — one per accepted chunk.
    assert spy.count() == 3

    # Empty chunks are silent.
    coord.on_chat_content_chunk("")
    assert spy.count() == 3


# --- v0.11 multi-turn history surface --------------------------------


def test_clear_resets_model_and_internal_state(model_and_coordinator):
    """Clear wipes rows AND the streaming-draft pointer, the
    user-bubble-anchor flag, and the verbose-log dedupe state — so the
    next turn starts from a fully fresh per-turn machine."""
    model, coord = model_and_coordinator(verbose=True)
    coord.on_state_changed(AppState.TRANSCRIBING.value)
    coord.on_user_transcript("u1")
    coord.on_chat_content_chunk("partial answer")
    assert model.rowCount() > 0
    assert coord.current_turn_user_bubble_present
    assert coord._streaming_assistant_index >= 0  # pyright: ignore[reportPrivateUsage]

    coord.clear()

    assert model.rowCount() == 0
    assert coord.current_turn_user_bubble_present is False
    assert coord._streaming_assistant_index == -1  # pyright: ignore[reportPrivateUsage]
    assert coord.last_logged_status_state is None
    assert list(coord.pending_status_log_states) == []


def test_clear_emits_conversation_changed_when_rows_present(model_and_coordinator):
    _model, coord = model_and_coordinator(verbose=False)
    coord.on_user_transcript("u1")
    spy = QSignalSpy(coord.conversation_changed)
    coord.clear()
    assert spy.count() == 1


def test_clear_on_empty_transcript_is_silent(model_and_coordinator):
    model, coord = model_and_coordinator(verbose=False)
    spy = QSignalSpy(coord.conversation_changed)
    coord.clear()
    assert model.rowCount() == 0
    assert spy.count() == 0


# --- v0.11 trim-on-assistant-response --------------------------------


def test_trim_drops_oldest_pair_after_cap_exceeded(model_and_coordinator):
    """When `on_assistant_response` lands a pair beyond the cap,
    the oldest user/assistant pair is dropped from the model."""
    model, coord = model_and_coordinator(verbose=False)
    coord.set_max_history_turns(4)  # last 4 entries = 2 pairs
    coord.on_user_transcript("u0")
    coord.on_assistant_response("a0")
    coord.on_user_transcript("u1")
    coord.on_assistant_response("a1")
    coord.on_user_transcript("u2")
    coord.on_assistant_response("a2")  # this lands the 3rd pair → trim 1
    rows = _all_rows(model)
    assert [(r or {}).get("text") for r in rows] == ["u1", "a1", "u2", "a2"]


def test_trim_emits_rows_dropped_from_front_signal(model_and_coordinator):
    """Round-3 #4: the coordinator emits `rows_dropped_from_front`
    with the count of rows just removed from the front of the model.
    External row-index trackers (e.g. `MainWindow._speaking_row`)
    rely on this to shift in lockstep with the trim — without it,
    the inline ▶/🤫 toggle renders on the wrong row after a trim
    fires."""
    model, coord = model_and_coordinator(verbose=False)
    coord.set_max_history_turns(4)
    spy = QSignalSpy(coord.rows_dropped_from_front)

    # First 2 pairs land — no trim yet (count == cap).
    for i in range(2):
        coord.on_user_transcript(f"u{i}")
        coord.on_assistant_response(f"a{i}")
    assert spy.count() == 0

    # 3rd pair pushes past the cap → trim drops 2 rows from the front.
    coord.on_user_transcript("u2")
    coord.on_assistant_response("a2")
    assert spy.count() == 1
    assert spy.at(0)[0] == 2

    # 4th pair → trim again, drops another 2.
    coord.on_user_transcript("u3")
    coord.on_assistant_response("a3")
    assert spy.count() == 2
    assert spy.at(1)[0] == 2


def test_trim_skipped_when_cap_zero(model_and_coordinator):
    """`max_history_turns=0` is the unbounded-history sentinel —
    trim must NOT fire."""
    model, coord = model_and_coordinator(verbose=False)
    coord.set_max_history_turns(0)
    for i in range(8):
        coord.on_user_transcript(f"u{i}")
        coord.on_assistant_response(f"a{i}")
    assert model.rowCount() == 16


def test_trim_does_not_fire_below_cap(model_and_coordinator):
    """When the conversation hasn't reached the cap, nothing is
    dropped on assistant-response landings."""
    model, coord = model_and_coordinator(verbose=False)
    coord.set_max_history_turns(20)
    coord.on_user_transcript("u0")
    coord.on_assistant_response("a0")
    coord.on_user_transcript("u1")
    coord.on_assistant_response("a1")
    assert model.rowCount() == 4


def test_trim_drops_status_rows_belonging_to_removed_turns(model_and_coordinator):
    """Per-turn status breadcrumbs (Transcribing, Thinking) that
    landed inside a turn's window must be dropped along with the
    user/assistant pair they belong to — they're meaningless once
    their parent pair is gone."""
    model, coord = model_and_coordinator(verbose=True)
    coord.set_max_history_turns(2)  # last 2 entries = 1 pair
    # Turn 1 with status breadcrumbs.
    coord.on_state_changed(AppState.RECORDING.value)
    coord.on_user_transcript("u0")
    coord.on_state_changed(AppState.TRANSCRIBING.value)
    coord.on_state_changed(AppState.THINKING.value)
    coord.on_assistant_response("a0")
    assert model.rowCount() > 2  # u0 + a0 + status rows
    # Turn 2 lands → trim. Older status rows should also disappear.
    coord.on_state_changed(AppState.RECORDING.value)
    coord.on_user_transcript("u1")
    coord.on_assistant_response("a1")
    rows = _all_rows(model)
    # Only the keep-window survives — turn 1's status rows are gone
    # along with u0/a0.
    texts = [(r or {}).get("text") for r in rows]
    assert "u0" not in texts
    assert "a0" not in texts


def test_trim_pair_integrity_drops_complete_pair(model_and_coordinator):
    """Excess of 1 (odd) is rounded up to 2 so we drop a complete
    user/assistant pair, never a stranded single-side row."""
    model, coord = model_and_coordinator(verbose=False)
    coord.set_max_history_turns(3)  # cap=3 means after a 4th row, drop 2
    coord.on_user_transcript("u0")
    coord.on_assistant_response("a0")
    coord.on_user_transcript("u1")
    coord.on_assistant_response("a1")
    rows = _all_rows(model)
    texts = [(r or {}).get("text") for r in rows]
    # After (u0, a0, u1, a1), excess=1 → rounded to 2 → drop u0+a0.
    # Window starts on a user, never on a stranded assistant.
    assert texts == ["u1", "a1"]


def test_trim_emits_conversation_changed_once_per_assistant_landing(
    model_and_coordinator,
):
    """The on_assistant_response handler emits exactly one
    `conversation_changed` even when the helper trims rows — the
    trim itself does not double-emit."""
    model, coord = model_and_coordinator(verbose=False)
    coord.set_max_history_turns(2)
    coord.on_user_transcript("u0")
    coord.on_assistant_response("a0")
    coord.on_user_transcript("u1")
    spy = QSignalSpy(coord.conversation_changed)
    coord.on_assistant_response("a1")  # lands + trims u0/a0
    # Exactly one signal: the on_assistant_response final emit. The
    # trim helper deliberately doesn't fan out additional signals.
    assert spy.count() == 1


def test_set_max_history_turns_clamps_negative(model_and_coordinator):
    model, coord = model_and_coordinator(verbose=False)
    coord.set_max_history_turns(-5)
    # Negative clamps to 0 = unbounded.
    for i in range(5):
        coord.on_user_transcript(f"u{i}")
        coord.on_assistant_response(f"a{i}")
    assert model.rowCount() == 10


def test_trim_at_cap_one_drops_all_finalized(model_and_coordinator):
    """Codex P2 edge case: with `cap=1`, the pair-integrity rounding
    pushes `excess` to equal `len(finalized)`. The earlier early-
    return left the transcript growing unbounded — fix drops every
    finalized row instead so the cap is actually enforced."""
    model, coord = model_and_coordinator(verbose=False)
    coord.set_max_history_turns(1)
    coord.on_user_transcript("u0")
    coord.on_assistant_response("a0")
    # First pair lands → trim drops both (pair integrity beats
    # keeping a single user turn at cap=1).
    assert model.rowCount() == 0
    coord.on_user_transcript("u1")
    coord.on_assistant_response("a1")
    # Same again — the visible transcript does NOT grow.
    assert model.rowCount() == 0


