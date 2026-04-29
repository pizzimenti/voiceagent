"""Conversation turn ordering coordinator.

Owns the per-turn state machine that round-2 review of PR #7 surfaced
as a recurring source of subtle bugs:

- Status rows landing before the user bubble on short turns with no
  partial transcript (the "thinking is status, not bubble" invariant
  from `AGENTS.md`).
- Verbose-vs-simple log mode gating, including mid-turn toggles where
  queued entries must drain silently.
- Per-turn dedupe of consecutive identical pipeline status states,
  reset on `RECORDING` / `IDLE` turn boundaries.
- Draft user-bubble promotion when the pipeline advances past
  recording, and discard-on-error / discard-on-disconnect.

Before this class existed the policy was spread across
`_apply_state`, `_set_error_message`, `_apply_model_status`,
`_apply_tts_status`, `_on_llm_status_message`,
`_append_user_message`, `_sync_live_user_message`, and
`_discard_draft_user_message` on `MainWindow`, plus three private
state fields on the same class. The coordinator centralizes all of
it; `MainWindow` becomes a thin forwarder.

Design choice — direct `ConversationModel` access, not pure signals
================================================================

The plan offered two shapes:

1. **Pure signals.** Coordinator emits `message_appended` /
   `message_updated` / `message_removed`; `MainWindow` translates
   each to a `ConversationModel` call. Decoupled, easy to test
   without a real model.
2. **Direct `ConversationModel` access.** Coordinator takes the
   model in `__init__` and writes through it. Tighter coupling but
   the policy lives next to the writes.

We took option 2 for one structural reason: every promote / discard
/ sync path on the coordinator must *read* from the model
(`find_message_index`, `message(idx)`) before deciding what to write.
With pure signals the coordinator would still need a `model`
reference to read, OR `MainWindow` would have to expose
`find_message_index` and `message(idx)` back through the API the
coordinator calls — at which point we have a half-coupled
"emit-a-signal-but-call-a-method-back" surface that is harder to
follow than the direct write.

The coordinator is still independently testable — `ConversationModel`
is itself a `QAbstractListModel` that constructs cheaply with no
external dependencies, so unit tests build a real model + a real
coordinator and assert on rows. `QSignalSpy` catches the single
`conversation_changed` notify signal the coordinator emits when the
model has been mutated; `MainWindow` re-emits that on its own
QML-bound `conversation_changed`.

This trade is documented at the top of the file precisely so future
maintainers (and `ConversationLogController` in cycle 7, which builds
on this one) do not relitigate it.
"""

from __future__ import annotations

from datetime import datetime
from typing import Callable

from PySide6.QtCore import QObject, Signal

from voiceagent.conversation_model import ConversationModel
from voiceagent.models import AppState


# Pipeline states surfaced into the verbose conversation log.
# Labels mirror the `micStatusLabel` vocabulary so the mic indicator
# and the log read identically. RECORDING is intentionally omitted
# because the draft user bubble already signals "listening".
STATUS_LOG_LABELS: dict[str, str] = {
    AppState.TRANSCRIBING.value: "Transcribing…",
    AppState.THINKING.value: "Thinking…",
    AppState.SYNTHESIZING.value: "Generating voice…",
    AppState.SPEAKING.value: "Speaking…",
}


def _default_clock_time() -> str:
    return datetime.now().strftime("%H:%M:%S")


class ConversationTurnCoordinator(QObject):
    """Owns per-turn ordering policy for the conversation transcript.

    Public surface:

    - **Inputs** (slots-from-`MainWindow`):
      `on_state_changed`, `on_live_transcript`, `on_user_transcript`,
      `on_assistant_response`, `on_log_message`, `on_error_message`,
      `on_connection_changed`, `set_verbose_mode`.
    - **Outputs** (signals): `conversation_changed` — fires once per
      mutation batch so `MainWindow` can re-emit on its own QML-bound
      signal. The coordinator does not expose row-level append /
      update / remove signals; consumers read the `ConversationModel`
      directly (it is a `QAbstractListModel`, designed to drive a
      `ListView` reactively).

    Internal state owned here:

    - `_current_turn_user_bubble_present`: whether this turn has a
      bubble we can anchor below.
    - `_pending_status_log_states`: queue of pipeline states that
      arrived before the user bubble materialized.
    - `_last_logged_status_state`: dedupe of consecutive identical
      verbose-mode pipeline state markers.
    - `_log_verbose_mode`: simple-vs-verbose gate, settable via
      `set_verbose_mode`.
    """

    conversation_changed = Signal()

    def __init__(
        self,
        conversation_model: ConversationModel,
        *,
        verbose_mode: bool | Callable[[], bool] = False,
        clock_time: Callable[[], str] | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._model = conversation_model
        # `verbose_mode` accepts either a static bool (set once via
        # `set_verbose_mode`) or a 0-arg callable that's queried every
        # time the gate matters. `MainWindow` passes a callable that
        # reads `QSettings` live — preserving the pre-refactor
        # behavior where the verbose-mode flag was always read fresh
        # so a mid-turn `QSettings.setValue("log_verbose_mode", ...)`
        # took effect immediately, even when bypassing the
        # `setLogVerboseMode` slot.
        if callable(verbose_mode):
            self._verbose_provider: Callable[[], bool] = verbose_mode
            self._verbose_static: bool | None = None
        else:
            self._verbose_static = bool(verbose_mode)
            self._verbose_provider = lambda: bool(self._verbose_static)
        self._clock_time = clock_time or _default_clock_time
        # See module docstring for why these three live here, not on
        # `MainWindow`.
        self._current_turn_user_bubble_present: bool = False
        self._pending_status_log_states: list[str] = []
        self._last_logged_status_state: str | None = None

    # -- introspection (used by integration tests) --------------------

    @property
    def current_turn_user_bubble_present(self) -> bool:
        return self._current_turn_user_bubble_present

    @property
    def pending_status_log_states(self) -> list[str]:
        # Tests assert against this list; return a live reference so
        # the assertion semantics match the previous `MainWindow`
        # field. Callers must not mutate.
        return self._pending_status_log_states

    @property
    def last_logged_status_state(self) -> str | None:
        return self._last_logged_status_state

    @property
    def verbose_mode(self) -> bool:
        return bool(self._verbose_provider())

    # -- inputs -------------------------------------------------------

    def set_verbose_mode(self, enabled: bool) -> None:
        # When the coordinator was constructed with a live-callable
        # provider (`MainWindow` reads `QSettings` directly), the
        # source-of-truth has already been mutated by the caller —
        # `set_verbose_mode` is a no-op in that case but accepted so
        # the slot wiring stays uniform across construction modes.
        if self._verbose_static is None:
            return
        self._verbose_static = bool(enabled)

    def on_state_changed(self, state: str) -> None:
        if state in {
            AppState.TRANSCRIBING.value,
            AppState.THINKING.value,
            AppState.SYNTHESIZING.value,
            AppState.SPEAKING.value,
        }:
            self._promote_live_user_message()
        if state in {AppState.RECORDING.value, AppState.IDLE.value}:
            # Turn boundary — reset the verbose-log dedupe AND the
            # user-bubble-anchor flag so a new turn starts clean.
            self._last_logged_status_state = None
            self._current_turn_user_bubble_present = False
            self._pending_status_log_states.clear()
        if self._verbose_provider() and state in STATUS_LOG_LABELS:
            if state != self._last_logged_status_state:
                self._last_logged_status_state = state
                if self._current_turn_user_bubble_present:
                    self._append_status_log_entry(state)
                else:
                    # Defer until the user bubble materializes, so the
                    # status row never lands before the user turn it
                    # belongs to.
                    self._pending_status_log_states.append(state)

    def on_live_transcript(self, text: str) -> None:
        cleaned = text.strip()
        draft_index = self._model.find_message_index(
            "user", bubble_state="draft"
        )
        if not cleaned:
            draft_message = self._model.message(draft_index)
            if draft_message is not None and not bool(
                draft_message.get("turnPending")
            ):
                self._model.remove_message(draft_index)
                self.conversation_changed.emit()
            return
        if draft_index >= 0:
            self._model.update_message(draft_index, text=cleaned)
        else:
            self._model.append_message(
                {
                    "role": "user",
                    "text": cleaned,
                    "replayable": False,
                    "bubbleState": "draft",
                    "turnPending": True,
                    "timestampLabel": "",
                }
            )
            self._current_turn_user_bubble_present = True
            self._flush_pending_status_log_entries()
        self.conversation_changed.emit()

    def on_user_transcript(self, text: str) -> None:
        cleaned = text.strip()
        if not cleaned:
            # No-speech outcome — drop any draft/pending user bubble that
            # was promoted at the TRANSCRIBING boundary so the turn ends
            # cleanly instead of leaving a stuck "sent" row.
            pending_index = self._model.find_message_index(
                "user", turn_pending=True
            )
            if pending_index >= 0:
                self._model.remove_message(pending_index)
                self.conversation_changed.emit()
            return
        pending_index = self._model.find_message_index(
            "user", turn_pending=True
        )
        if pending_index >= 0:
            self._model.update_message(
                pending_index,
                text=cleaned,
                bubbleState="sent",
                turnPending=False,
                timestampLabel=f"Sent {self._clock_time()}",
            )
        else:
            self._model.append_message(
                {
                    "role": "user",
                    "text": cleaned,
                    "replayable": False,
                    "bubbleState": "sent",
                    "turnPending": False,
                    "timestampLabel": f"Sent {self._clock_time()}",
                }
            )
        self._current_turn_user_bubble_present = True
        self._flush_pending_status_log_entries()
        self.conversation_changed.emit()

    def on_assistant_response(self, text: str) -> None:
        cleaned = text.strip()
        if not cleaned:
            return
        self._model.append_message(
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

    def on_log_message(self, text: str, level: str) -> None:
        """Append a `role='system'` operational notice (status / error
        text from the model loader, TTS loader, or LLM controller).
        Distinct from `role='status'` pipeline rows — those carry a
        `stateName` and are gated on verbose mode; system log rows
        always emit when called.
        """
        cleaned = text.strip()
        if not cleaned:
            return
        self._model.append_message(
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

    def on_error_message(self, message: str, *, discard_draft: bool = True) -> None:
        """`discard_draft=True` (default) is the right choice for STT
        / transcription / connection errors that signal the in-flight
        user turn is dead. Replay-of-prior-message failures are the
        exception and pass `discard_draft=False`.
        """
        if not message:
            return
        if discard_draft:
            self._discard_draft_user_message()
        self.on_log_message(message, "error")

    def on_connection_changed(self, enabled: bool) -> None:
        if not enabled:
            self._discard_draft_user_message()

    # -- internal helpers --------------------------------------------

    def _append_status_log_entry(self, state: str) -> None:
        self._model.append_message(
            {
                "role": "status",
                "text": STATUS_LOG_LABELS[state],
                "stateName": state,
            }
        )
        self.conversation_changed.emit()

    def _flush_pending_status_log_entries(self) -> None:
        if not self._pending_status_log_states:
            return
        # Honor a mid-turn verbose -> simple toggle. The queued states
        # are deferred entries from when verbose was on; if the user
        # has since switched to simple, drop them silently rather than
        # leaking pipeline rows into a transcript the user just chose
        # to keep clean. Always clear the queue since these states are
        # stale anyway (we've moved past them).
        if self._verbose_provider():
            for state in self._pending_status_log_states:
                self._append_status_log_entry(state)
        self._pending_status_log_states.clear()

    def _promote_live_user_message(self) -> None:
        draft_index = self._model.find_message_index(
            "user", bubble_state="draft"
        )
        if draft_index < 0:
            return
        self._model.update_message(
            draft_index, bubbleState="sent", turnPending=True
        )
        self.conversation_changed.emit()

    def _discard_draft_user_message(self) -> None:
        draft_index = self._model.find_message_index(
            "user", bubble_state="draft"
        )
        if draft_index < 0:
            return
        self._model.remove_message(draft_index)
        self.conversation_changed.emit()
