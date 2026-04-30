from __future__ import annotations

import pytest
from PySide6.QtCore import QModelIndex, Qt
from PySide6.QtTest import QSignalSpy

from voiceagent.conversation_model import ConversationModel


@pytest.fixture
def model(qtbot):
    return ConversationModel()


def _make_message(role: str, text: str, **extra: object) -> dict[str, object]:
    base: dict[str, object] = {
        "role": role,
        "text": text,
        "replayable": False,
        "bubbleState": "sent",
        "turnPending": False,
        "timestampLabel": "",
    }
    base.update(extra)
    return base


def test_append_message_returns_inserted_index(model):
    first = model.append_message(_make_message("user", "hello"))
    second = model.append_message(_make_message("assistant", "hi there"))
    third = model.append_message(_make_message("system", "ready", level="status"))
    assert first == 0
    assert second == 1
    assert third == 2
    assert model.rowCount() == 3


def test_data_returns_none_for_invalid_index(model):
    model.append_message(_make_message("user", "hello"))
    invalid = QModelIndex()
    assert model.data(invalid, ConversationModel.TextRole) is None


def test_data_display_role_falls_through_to_text(model):
    model.append_message(_make_message("user", "hello there"))
    idx = model.index(0, 0)
    assert model.data(idx, Qt.ItemDataRole.DisplayRole) == "hello there"
    assert model.data(idx, ConversationModel.TextRole) == "hello there"


def test_update_message_updates_underlying_dict(model):
    model.append_message(_make_message("user", "draft", bubbleState="draft", turnPending=True))
    model.update_message(0, text="final", bubbleState="sent", turnPending=False)
    idx = model.index(0, 0)
    assert model.data(idx, ConversationModel.TextRole) == "final"
    assert model.data(idx, ConversationModel.BubbleStateRole) == "sent"
    assert model.data(idx, ConversationModel.TurnPendingRole) is False


def test_update_message_emits_data_changed_only_for_changed_roles(model):
    model.append_message(_make_message("user", "hello", bubbleState="sent"))
    spy = QSignalSpy(model.dataChanged)
    model.update_message(0, text="hello world", bubbleState="sent")
    assert spy.count() == 1
    # Roles list is the third arg of dataChanged(top_left, bottom_right, roles)
    roles = spy.at(0)[2]
    assert ConversationModel.TextRole in roles
    assert ConversationModel.BubbleStateRole not in roles


def test_update_message_no_emit_when_values_unchanged(model):
    model.append_message(_make_message("user", "hello"))
    spy = QSignalSpy(model.dataChanged)
    model.update_message(0, text="hello", bubbleState="sent")
    assert spy.count() == 0


def test_remove_message_decrements_row_count_and_reindexes(model):
    model.append_message(_make_message("user", "first"))
    model.append_message(_make_message("user", "second"))
    model.append_message(_make_message("user", "third"))
    model.remove_message(1)
    assert model.rowCount() == 2
    assert model.data(model.index(0, 0), ConversationModel.TextRole) == "first"
    assert model.data(model.index(1, 0), ConversationModel.TextRole) == "third"


def test_remove_message_out_of_range_is_noop(model):
    model.append_message(_make_message("user", "hello"))
    model.remove_message(5)
    model.remove_message(-1)
    assert model.rowCount() == 1


def test_find_message_index_reverse_search(model):
    model.append_message(_make_message("user", "first"))
    model.append_message(_make_message("assistant", "reply"))
    model.append_message(_make_message("user", "second"))
    # Most recent user message is at row 2.
    assert model.find_message_index("user") == 2
    assert model.find_message_index("assistant") == 1


def test_find_message_index_returns_minus_one_when_not_found(model):
    model.append_message(_make_message("user", "hi"))
    assert model.find_message_index("system") == -1
    assert model.find_message_index("assistant") == -1


def test_find_message_index_respects_bubble_state_filter(model):
    model.append_message(_make_message("user", "draft", bubbleState="draft", turnPending=True))
    model.append_message(_make_message("user", "final", bubbleState="sent"))
    assert model.find_message_index("user", bubble_state="draft") == 0
    assert model.find_message_index("user", bubble_state="sent") == 1
    assert model.find_message_index("user", bubble_state="missing") == -1


def test_find_message_index_respects_turn_pending_filter(model):
    model.append_message(_make_message("user", "queued", turnPending=True))
    model.append_message(_make_message("user", "delivered", turnPending=False))
    assert model.find_message_index("user", turn_pending=True) == 0
    assert model.find_message_index("user", turn_pending=False) == 1


def test_row_count_with_valid_parent_is_zero(model):
    model.append_message(_make_message("user", "hi"))
    parent = model.index(0, 0)
    assert model.rowCount(parent) == 0


def test_message_returns_shallow_copy_not_internal_dict(model):
    model.append_message(_make_message("user", "hi"))
    snapshot = model.message(0)
    assert snapshot is not None
    snapshot["text"] = "MUTATED"
    fresh = model.message(0)
    assert fresh is not None
    assert fresh["text"] == "hi"


def test_append_status_message_carries_state_name(model):
    model.append_message({"role": "status", "text": "Thinking…", "stateName": "thinking"})
    idx = model.index(0, 0)
    assert model.data(idx, ConversationModel.MessageRole) == "status"
    assert model.data(idx, ConversationModel.TextRole) == "Thinking…"
    assert model.data(idx, ConversationModel.StateNameRole) == "thinking"


def test_status_message_coexists_with_other_roles(model):
    model.append_message(_make_message("user", "what time is it"))
    model.append_message({"role": "status", "text": "Transcribing…", "stateName": "transcribing"})
    model.append_message({"role": "status", "text": "Thinking…", "stateName": "thinking"})
    model.append_message(_make_message("assistant", "It is noon"))
    assert model.rowCount() == 4
    assert model.data(model.index(0, 0), ConversationModel.MessageRole) == "user"
    assert model.data(model.index(1, 0), ConversationModel.MessageRole) == "status"
    assert model.data(model.index(1, 0), ConversationModel.StateNameRole) == "transcribing"
    assert model.data(model.index(2, 0), ConversationModel.MessageRole) == "status"
    assert model.data(model.index(2, 0), ConversationModel.StateNameRole) == "thinking"
    assert model.data(model.index(3, 0), ConversationModel.MessageRole) == "assistant"


def test_find_message_index_locates_status_role(model):
    model.append_message(_make_message("user", "hello"))
    model.append_message({"role": "status", "text": "Thinking…", "stateName": "thinking"})
    model.append_message(_make_message("assistant", "hi"))
    model.append_message({"role": "status", "text": "Speaking…", "stateName": "speaking"})
    # find_message_index walks in reverse, so most recent status row wins.
    assert model.find_message_index("status") == 3

def test_role_names_returns_a_real_dict(model):
    # PySide6 strictly type-checks the `roleNames()` return value —
    # anything that isn't a real `dict` (notably `MappingProxyType`)
    # triggers a RuntimeWarning and Qt then uses an empty role map,
    # which breaks every QML role binding silently.
    role_names = model.roleNames()
    assert type(role_names) is dict
    assert role_names is not ConversationModel._ROLE_NAMES
    assert model.roleNames() is not role_names


def test_update_message_uses_inverse_role_map():
    # `_KEY_TO_ROLE` is an O(1) inverse of `_ROLE_KEYS` built at
    # class-build time. Spot-check the wiring. Class-level inspection
    # only — no instance / qtbot needed.
    assert ConversationModel._KEY_TO_ROLE['text'] == ConversationModel.TextRole
    assert ConversationModel._KEY_TO_ROLE['bubbleState'] == ConversationModel.BubbleStateRole
    assert ConversationModel._KEY_TO_ROLE['stateName'] == ConversationModel.StateNameRole
    assert ConversationModel._KEY_TO_ROLE['thinkingText'] == ConversationModel.ThinkingTextRole
    assert (
        ConversationModel._KEY_TO_ROLE['thinkingExpanded']
        == ConversationModel.ThinkingExpandedRole
    )


def test_role_names_includes_thinking_roles(model):
    role_names = model.roleNames()
    assert role_names[ConversationModel.ThinkingTextRole] == b"thinkingText"
    assert role_names[ConversationModel.ThinkingExpandedRole] == b"thinkingExpanded"


def test_thinking_fields_default_to_none_when_unspecified(model):
    # A row inserted without thinkingText / thinkingExpanded should
    # report `None` through `data()` for both new roles. This preserves
    # the existing model behavior — keys not present in the underlying
    # dict resolve to `None`, never to a fabricated default.
    model.append_message(_make_message("assistant", "hi"))
    idx = model.index(0, 0)
    assert model.data(idx, ConversationModel.ThinkingTextRole) is None
    assert model.data(idx, ConversationModel.ThinkingExpandedRole) is None


def test_update_message_round_trips_thinking_text(model):
    model.append_message(_make_message("assistant", "hello"))
    model.update_message(0, thinkingText="step 1\nstep 2")
    idx = model.index(0, 0)
    assert model.data(idx, ConversationModel.ThinkingTextRole) == "step 1\nstep 2"


def test_update_message_round_trips_thinking_expanded(model):
    model.append_message(_make_message("assistant", "hello"))
    model.update_message(0, thinkingExpanded=True)
    idx = model.index(0, 0)
    assert model.data(idx, ConversationModel.ThinkingExpandedRole) is True
    model.update_message(0, thinkingExpanded=False)
    assert model.data(idx, ConversationModel.ThinkingExpandedRole) is False


def test_update_message_emits_data_changed_for_thinking_text(model):
    model.append_message(_make_message("assistant", "hello"))
    spy = QSignalSpy(model.dataChanged)
    model.update_message(0, thinkingText="reasoning…")
    assert spy.count() == 1
    roles = spy.at(0)[2]
    assert ConversationModel.ThinkingTextRole in roles


def test_update_message_emits_data_changed_for_thinking_expanded(model):
    model.append_message(_make_message("assistant", "hello"))
    spy = QSignalSpy(model.dataChanged)
    model.update_message(0, thinkingExpanded=True)
    assert spy.count() == 1
    roles = spy.at(0)[2]
    assert ConversationModel.ThinkingExpandedRole in roles


# --- v0.11 multi-turn history surface ----------------------------------


def test_clear_resets_messages_and_emits_reset(model):
    model.append_message(_make_message("user", "hi"))
    model.append_message(_make_message("assistant", "hello"))
    reset_spy = QSignalSpy(model.modelReset)
    model.clear()
    assert model.rowCount() == 0
    assert reset_spy.count() == 1


def test_clear_on_empty_model_is_noop_no_signal(model):
    reset_spy = QSignalSpy(model.modelReset)
    model.clear()
    assert reset_spy.count() == 0
    assert model.rowCount() == 0


def test_to_openai_messages_round_trip_user_assistant(model):
    model.append_message(_make_message("user", "what is the capital of France?"))
    model.append_message(_make_message("assistant", "Paris."))
    model.append_message(_make_message("user", "what is the population?"))
    model.append_message(_make_message("assistant", "About 2.1 million."))
    out = model.to_openai_messages("you are local")
    assert out == [
        {"role": "system", "content": "you are local"},
        {"role": "user", "content": "what is the capital of France?"},
        {"role": "assistant", "content": "Paris."},
        {"role": "user", "content": "what is the population?"},
        {"role": "assistant", "content": "About 2.1 million."},
    ]


def test_to_openai_messages_skips_thinking_content(model):
    """`reasoning_content` is the model's thinking channel — replaying
    it as assistant `content` would confuse the model on the next turn.
    `to_openai_messages` must serialize `text` only."""
    model.append_message(_make_message("user", "hi"))
    model.append_message(
        _make_message("assistant", "hello", thinkingText="let me think about this")
    )
    out = model.to_openai_messages("")
    assert out == [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
    ]


def test_to_openai_messages_omits_empty_system_prompt(model):
    model.append_message(_make_message("user", "hi"))
    out = model.to_openai_messages("")
    assert out == [{"role": "user", "content": "hi"}]
    out_whitespace = model.to_openai_messages("   ")
    assert out_whitespace == [{"role": "user", "content": "hi"}]


def test_to_openai_messages_skips_drafts_and_pending_turns(model):
    """Draft user bubbles, in-flight (turnPending=True) bubbles, and
    streaming-draft assistants must NOT appear in serialized history."""
    model.append_message(_make_message("user", "earlier"))
    model.append_message(_make_message("assistant", "earlier reply"))
    model.append_message(
        _make_message("user", "draft text", bubbleState="draft", turnPending=True)
    )
    model.append_message(
        _make_message("user", "in-flight", bubbleState="sent", turnPending=True)
    )
    model.append_message(
        _make_message(
            "assistant", "streaming…", bubbleState="draft", turnPending=True
        )
    )
    out = model.to_openai_messages("sys")
    assert out == [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "earlier"},
        {"role": "assistant", "content": "earlier reply"},
    ]


def test_to_openai_messages_skips_system_and_status_rows(model):
    """`role='system'` operational notices and `role='status'` pipeline
    rows are UI-only — they must never round-trip into LLM history."""
    model.append_message(_make_message("user", "u1"))
    model.append_message(
        _make_message("system", "model loaded", level="status", bubbleState="plain")
    )
    model.append_message(
        {
            "role": "status",
            "text": "Transcribing…",
            "stateName": "transcribing",
        }
    )
    model.append_message(_make_message("assistant", "a1"))
    out = model.to_openai_messages("")
    assert out == [
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": "a1"},
    ]


def test_to_openai_messages_skips_empty_text(model):
    model.append_message(_make_message("user", "u1"))
    model.append_message(_make_message("assistant", ""))
    model.append_message(_make_message("user", "   "))
    model.append_message(_make_message("assistant", "a2"))
    out = model.to_openai_messages("")
    assert out == [
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": "a2"},
    ]


def test_to_openai_messages_respects_max_turns_cap(model):
    for i in range(6):
        model.append_message(_make_message("user", f"u{i}"))
        model.append_message(_make_message("assistant", f"a{i}"))
    # 12 finalized rows → cap to last 4 (= 2 user/assistant pairs).
    out = model.to_openai_messages("sys", max_turns=4)
    assert out == [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "u4"},
        {"role": "assistant", "content": "a4"},
        {"role": "user", "content": "u5"},
        {"role": "assistant", "content": "a5"},
    ]


def test_to_openai_messages_max_turns_zero_or_none_uncapped(model):
    for i in range(3):
        model.append_message(_make_message("user", f"u{i}"))
        model.append_message(_make_message("assistant", f"a{i}"))
    out_none = model.to_openai_messages("", max_turns=None)
    out_zero = model.to_openai_messages("", max_turns=0)
    # Both treat the cap as "unbounded" — six finalized rows pass through.
    assert len(out_none) == 6
    assert len(out_zero) == 6


def test_to_openai_messages_pair_integrity_guard_on_odd_cut(model):
    """An odd `max_turns` could land the slice on an `assistant` head,
    which the model interprets as unfinished/stranded. Mirror LangChain
    `ConversationBufferWindowMemory`'s "drop a complete pair" — the
    serialized history must always lead with `user` (or system, or be
    empty). Drops one more entry to land on a user."""
    for i in range(4):
        model.append_message(_make_message("user", f"u{i}"))
        model.append_message(_make_message("assistant", f"a{i}"))
    # 8 finalized rows; max_turns=3 would slice [-3:] →
    # ["assistant a2", "user u3", "assistant a3"], stranding an
    # assistant at the head. Guard drops it.
    out = model.to_openai_messages("", max_turns=3)
    assert out == [
        {"role": "user", "content": "u3"},
        {"role": "assistant", "content": "a3"},
    ]

