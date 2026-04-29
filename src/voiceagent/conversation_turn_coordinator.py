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
observer-facing `conversation_changed`.

This trade is documented at the top of the file precisely so future
maintainers (and `ConversationLogController` in cycle 7, which builds
on this one) do not relitigate it.
"""

from __future__ import annotations

from datetime import datetime
import logging
from typing import Callable

from PySide6.QtCore import QObject, Signal

from voiceagent.conversation_model import ConversationModel
from voiceagent.logging_utils import CONVERSATION_LOGGER_NAME
from voiceagent.models import AppState


_CONVERSATION_LOGGER = logging.getLogger(CONVERSATION_LOGGER_NAME)


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
    # Emitted with the number of rows just removed from the FRONT of
    # `_model` by `_trim_to_history_cap`. `MainWindow` connects to
    # this so its `_speaking_row` (a row index into the same model)
    # can be reindexed atomically with the trim — without it, after
    # a trim that drops oldest pairs, `speakingRow` points at the
    # wrong (or no longer existing) bubble and the inline ▶/🤫
    # toggle renders on the wrong row. CodeRabbit round-3 P1.
    rows_dropped_from_front = Signal(int)

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
        # v0.10.0 streaming chat — the row index of the in-flight draft
        # assistant bubble being built up from `on_chat_*_chunk` calls,
        # or -1 when no draft exists. Reset on turn boundaries
        # (RECORDING / IDLE) and after promotion to "sent".
        self._streaming_assistant_index: int = -1
        # v0.11 multi-turn cap. Trimmed at the end of every assistant
        # turn so the visible transcript matches what the LLM sees on
        # the *next* call. 0 = unbounded (defensive matches what
        # `to_openai_messages(max_turns=0)` does). Settable via
        # `set_max_history_turns` from the window.
        self._max_history_turns: int = 20

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

    def set_max_history_turns(self, value: int) -> None:
        """Set the cap on finalized user/assistant rows kept in the
        visible transcript. Mirrors `AppConfig.max_history_turns`.
        Negative clamps to 0 (= unbounded). Trim is applied lazily on
        the next assistant-response landing — does not retroactively
        prune the model when the cap shrinks (call `clear()` if you
        need an immediate reset)."""
        self._max_history_turns = max(0, int(value))

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
            # v0.10.0 streaming — release the draft-assistant pointer so
            # the next turn's first chunk creates a fresh row instead of
            # appending into the previous turn's (now-promoted) bubble.
            self._streaming_assistant_index = -1
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
        # v0.10.0 streaming: if `on_chat_*_chunk` already built a draft
        # assistant row for this turn, promote it in place instead of
        # appending a second bubble. The accumulated `thinkingText`
        # stays whatever the stream collected; only `text` swaps to the
        # canonical final response (which may differ from the streamed
        # accumulation if the chat client normalizes / strips between
        # the per-chunk callbacks and the final return value).
        draft_index = self._streaming_assistant_index
        promoted_in_place = False
        if draft_index >= 0:
            draft = self._model.message(draft_index)
            if (
                draft is not None
                and draft.get("role") == "assistant"
                and draft.get("bubbleState") == "draft"
            ):
                self._model.update_message(
                    draft_index,
                    text=cleaned,
                    replayable=True,
                    bubbleState="sent",
                    turnPending=False,
                    timestampLabel=f"Received {self._clock_time()}",
                )
                self._streaming_assistant_index = -1
                promoted_in_place = True
            else:
                # Stale pointer (row was removed / mutated underneath
                # us). Drop it and fall through to the legacy append
                # path so the final response still lands in the
                # transcript.
                self._streaming_assistant_index = -1
        if not promoted_in_place:
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
        # v0.11 multi-turn cap: a complete user/assistant pair just
        # landed; trim oldest pairs if we've passed the cap so the
        # visible transcript matches what we'll send to the LLM next
        # turn. Trimmed pairs include any per-turn status / system
        # rows that fell before the new kept-window head — those
        # breadcrumbs belong to dropped turns.
        self._trim_to_history_cap()
        self.conversation_changed.emit()

    # -- streaming chat chunks (v0.10.0) -----------------------------

    def on_chat_thinking_chunk(self, text: str) -> None:
        """Append a thinking-stream chunk to the in-flight draft
        assistant row, creating the row on first chunk.

        Empty chunks are ignored to avoid creating a draft row on a
        no-op flush.
        """
        if not text:
            return
        index = self._ensure_streaming_assistant_row()
        msg = self._model.message(index)
        if msg is None:
            return
        accumulated = str(msg.get("thinkingText") or "") + text
        self._model.update_message(index, thinkingText=accumulated)
        self.conversation_changed.emit()

    def on_chat_content_chunk(self, text: str) -> None:
        """Append a content-stream chunk to the in-flight draft
        assistant row, creating the row on first chunk.
        """
        if not text:
            return
        index = self._ensure_streaming_assistant_row()
        msg = self._model.message(index)
        if msg is None:
            return
        accumulated = str(msg.get("text") or "") + text
        self._model.update_message(index, text=accumulated)
        self.conversation_changed.emit()

    def set_thinking_expanded(self, row: int, expanded: bool) -> None:
        """QML-facing slot: toggle the per-row `thinkingExpanded`
        sticky flag. No signal is emitted explicitly — the underlying
        `update_message` call emits `dataChanged` for the role, which
        is what QML bindings observe.

        Out-of-range rows and non-assistant roles are silently ignored
        so QML doesn't have to guard the call site.
        """
        if row < 0 or row >= self._model.rowCount():
            return
        msg = self._model.message(row)
        if msg is None or msg.get("role") != "assistant":
            return
        self._model.update_message(row, thinkingExpanded=bool(expanded))

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

    def clear(self) -> None:
        """Reset the conversation transcript and all per-turn coordinator
        state. Used by the model-switch path: history accumulated against
        a different LLM (different tokenizer / context window /
        fine-tuning) is not meaningful when replayed against the new one,
        so the transcript is wiped along with the streaming-draft pointer
        and the verbose-log dedupe flags. A no-op if the transcript is
        already empty so a redundant signal does not spuriously emit
        `conversation_changed`.
        """
        was_empty = self._model.rowCount() == 0
        self._model.clear()
        self._streaming_assistant_index = -1
        self._current_turn_user_bubble_present = False
        self._pending_status_log_states.clear()
        self._last_logged_status_state = None
        if not was_empty:
            self.conversation_changed.emit()

    # -- internal helpers --------------------------------------------

    def _trim_to_history_cap(self) -> None:
        """Drop oldest rows so finalized user/assistant entries fit
        within `_max_history_turns`. Pair-integrity guard rounds the
        excess up to even so we always drop a complete user/assistant
        pair (mirrors LangChain `ConversationBufferWindowMemory`
        semantics). Status / system rows that fall before the new
        kept-window head are dropped along with the pair — they are
        per-turn breadcrumbs and would dangle without their parent
        turn. The streaming-draft assistant pointer is updated to
        track its new row index after the front-of-model removal; it
        cannot itself be in the dropped range because drafts are
        excluded from the cap (`turnPending=True`).

        Caller emits `conversation_changed` once after this returns —
        the helper does not emit, to keep the on_assistant_response
        notify-once contract intact.
        """
        cap = self._max_history_turns
        if cap <= 0:
            return
        finalized: list[int] = []
        for i in range(self._model.rowCount()):
            msg = self._model.message(i) or {}
            if msg.get("role") not in ("user", "assistant"):
                continue
            if msg.get("bubbleState") != "sent":
                continue
            if bool(msg.get("turnPending")):
                continue
            text = msg.get("text") or ""
            if not isinstance(text, str) or not text.strip():
                continue
            finalized.append(i)
        if len(finalized) <= cap:
            return
        excess = len(finalized) - cap
        if excess % 2 == 1:
            excess += 1
        if excess >= len(finalized):
            # Cap is smaller than a complete pair (e.g. `cap=1`).
            # Drop EVERY finalized row plus any non-pair rows
            # interleaved between them — pair-integrity beats keeping
            # a stranded single-side row at the head. Anything after
            # the last finalized row (a trailing status breadcrumb,
            # or a freshly-arriving draft) belongs to a still-open
            # turn and stays. Without this branch the early `return`
            # left the visible transcript growing unbounded at
            # `cap=1`, which Codex P2 surfaced.
            keep_from_index = finalized[-1] + 1
        else:
            keep_from_index = finalized[excess]
        if keep_from_index <= 0:
            return
        # Adjust the streaming-draft pointer for the row shift before
        # mutating the model — `_streaming_assistant_index` is a row
        # index into the same model.
        if self._streaming_assistant_index >= 0:
            if self._streaming_assistant_index < keep_from_index:
                # Draft was inside the dropped range. Should not happen
                # — drafts are turnPending=True so they were not
                # counted as finalized — but defensively reset.
                self._streaming_assistant_index = -1
            else:
                self._streaming_assistant_index -= keep_from_index
        # Bulk-drop from the front. `remove_message` emits
        # beginRemoveRows / endRemoveRows per call; QML's ListView
        # batches the resulting layout updates, so the visual cost is
        # roughly linear in dropped-row count.
        for _ in range(keep_from_index):
            self._model.remove_message(0)
        _CONVERSATION_LOGGER.info(
            "trim dropped_rows=%d cap=%d remaining=%d",
            keep_from_index,
            cap,
            self._model.rowCount(),
        )
        # Notify external row-index trackers (e.g.
        # `MainWindow._speaking_row`) so they can shift in sync. The
        # coordinator only knows about its OWN row-index field
        # (`_streaming_assistant_index`, adjusted just above);
        # everyone else hooks this signal. CodeRabbit round-3 P1.
        self.rows_dropped_from_front.emit(keep_from_index)

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

    def _ensure_streaming_assistant_row(self) -> int:
        """Return the row index of the in-flight draft assistant row,
        creating one if none exists.

        Reuses `self._streaming_assistant_index` when it still points
        at a `role='assistant', bubbleState='draft'` row; otherwise
        appends a fresh draft, captures `verbose_mode` ONCE at
        insertion as the default `thinkingExpanded` value, and stores
        the new index. The expanded flag is intentionally NOT
        re-evaluated on every chunk so a user toggle (via
        `set_thinking_expanded`) sticks for the rest of the stream.
        """
        if self._streaming_assistant_index >= 0:
            existing = self._model.message(self._streaming_assistant_index)
            if (
                existing is not None
                and existing.get("role") == "assistant"
                and existing.get("bubbleState") == "draft"
            ):
                return self._streaming_assistant_index
            # Pointer went stale — fall through to append a new draft.
            self._streaming_assistant_index = -1
        new_index = self._model.append_message(
            {
                "role": "assistant",
                "text": "",
                "thinkingText": "",
                "thinkingExpanded": bool(self._verbose_provider()),
                "replayable": False,
                "bubbleState": "draft",
                "turnPending": True,
                "timestampLabel": "",
            }
        )
        self._streaming_assistant_index = new_index
        return new_index

    def _discard_draft_user_message(self) -> None:
        draft_index = self._model.find_message_index(
            "user", bubble_state="draft"
        )
        if draft_index < 0:
            return
        self._model.remove_message(draft_index)
        self.conversation_changed.emit()
