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
