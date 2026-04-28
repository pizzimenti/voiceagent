"""QML-side wiring test for PR #24's replay-failure toast.

`tests/test_mainwindow_integration.py` already covers the Python-side
contract: `MainWindow.replayMessage(idx)` emits `replay_failed(reason)`
when the selected voice is unavailable or synthesis raises.

This test covers the OTHER half of that flow — the QML side. The
production wiring lives in `MainWindow.qml`'s `Component.onCompleted`
block:

    Component.onCompleted: {
        if (voiceAgent && voiceAgent.replay_failed) {
            voiceAgent.replay_failed.connect(function(reason) {
                root.showPassiveNotification(reason, "short");
            });
        }
    }

If the connection block is removed, renamed, or `showPassiveNotification`
stops being called, this test fails. The pre-PR-#24 wiring used a
`Connections { function onReplayFailed(...) }` element which silently
*never fired* — Qt does not auto-camelCase snake_case Python signal
names for Connections-style handlers at runtime. The direct
`signal.connect(fn)` form does bind correctly. Locking in the working
form is exactly the kind of QML-side regression this suite exists to
catch.

Verification mechanism: Kirigami's `showPassiveNotification` appends
to an internal `notificationsModel` ListModel inside the
`PassiveNotificationsManager` overlay. We reach into the overlay
via `QObject.findChildren` and inspect the model's `count` plus
each row's `text` role after the test action. This is a true
round-trip: signal emission → QML connect handler → real
`showPassiveNotification` → real Kirigami overlay model entry.
(We deliberately do not monkeypatch `showPassiveNotification` from
the test side: it's a read-only QML method and the recipe of
"replace it with a recorder" gives a less faithful assertion than
reading the actual overlay model anyway.)
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_SRC = _HERE.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QCoreApplication, QObject

from tests.fakes import build_compiletest_window


def _drain(times: int = 8) -> None:
    app = QCoreApplication.instance()
    assert app is not None
    for _ in range(times):
        app.processEvents()


def _find_notifications_model(qml_root):
    """Locate the `notificationsModel` ListModel that Kirigami's
    `PassiveNotificationsManager` populates on
    `showPassiveNotification(...)`. The QML `ListModel` element
    surfaces in PySide6 as `QAbstractListModel`; on a fresh
    `Kirigami.ApplicationWindow` there is exactly one such child
    reachable from the root (the passive-notifications model). If
    Kirigami's plugin grows other ListModels nested under the root
    in the future, fall back to picking the one whose `roleNames()`
    contain the `text` role expected by the notifications schema.
    """
    candidates = qml_root.findChildren(QObject)
    list_models = [c for c in candidates if type(c).__name__ == "QAbstractListModel"]
    if not list_models:
        return None
    if len(list_models) == 1:
        return list_models[0]
    # Multiple — pick the one whose roleNames include "text".
    for model in list_models:
        roles = model.property("roleNames") or {}
        if isinstance(roles, dict) and any(
            (b.data().decode() if hasattr(b, "data") else str(b)) == "text"
            for b in roles.values()
        ):
            return model
    return list_models[0]


def _read_notification_messages(qml_root, model) -> list[str]:
    """Read the `text` role from every row of the notifications
    ListModel.

    `ListModel.get(int)` is a QML-only method (not a Qt-meta slot),
    so `QMetaObject.invokeMethod` cannot reach it from Python. We
    instead use Qt's standard `QAbstractItemModel` surface, which
    PySide6 *does* expose: `.index(row, 0)` → `.data(idx, role)`
    where `role` is the integer role-id resolved from
    `model.roleNames()`. This avoids any QML / JS bridging.
    """
    if model is None:
        return []
    count = model.property("count") or 0
    if count <= 0:
        return []
    role_names = model.roleNames() if hasattr(model, "roleNames") else {}
    text_role_id = None
    for role_id, role_bytes in role_names.items():
        name = (
            role_bytes.data().decode()
            if hasattr(role_bytes, "data")
            else str(role_bytes)
        )
        if name == "text":
            text_role_id = role_id
            break
    if text_role_id is None:
        return []
    messages: list[str] = []
    for i in range(count):
        idx = model.index(i, 0)
        value = model.data(idx, text_role_id)
        messages.append(str(value) if value is not None else "")
    return messages


# --- fixtures ------------------------------------------------------------


@pytest.fixture
def real_qml_window():
    """Build a real MainWindow whose engine actually loads
    `MainWindow.qml`, including the Kirigami passive-notification
    overlay. Caller drives state and reads the overlay model.
    """
    window = build_compiletest_window()
    _drain()
    yield window
    try:
        window.shutdown()
    except Exception:  # noqa: BLE001 — teardown best-effort
        pass


# --- tests ---------------------------------------------------------------


def test_replay_failure_triggers_qml_passive_notification(real_qml_window):
    """The integration assertion: emitting `replay_failed` must reach
    the QML signal handler installed in `Component.onCompleted`,
    which in turn must call `showPassiveNotification(reason, "short")`
    on the root window. We verify the call landed by reading the
    Kirigami overlay's notification model AFTER the emission.

    Lock-in: pre-PR-#24's `Connections { function onReplayFailed }`
    form silently dropped emissions; this test would have caught
    that the moment it was added.
    """
    window = real_qml_window
    qml_root = window._window
    assert qml_root is not None, "engine should have loaded MainWindow.qml"

    model = _find_notifications_model(qml_root)
    assert model is not None, (
        "Kirigami passive-notifications model not reachable on QML root; "
        "the test cannot verify toast wiring without it"
    )
    initial_count = model.property("count") or 0

    # Drive the not-ready-TTS path: assistant message exists, voice
    # is_available=False → replayMessage emits replay_failed.
    tts = window.tts_loader.tts_service
    tts.set_available(False)
    window._append_assistant_message("hello user")
    _drain()
    window.replayMessage(0)
    _drain()

    final_count = model.property("count") or 0
    assert final_count > initial_count, (
        "showPassiveNotification should have appended a row to the "
        f"notifications model; count went from {initial_count} → "
        f"{final_count}"
    )

    messages = _read_notification_messages(qml_root, model)
    assert messages, "model has rows but no text payloads readable"
    last = messages[-1]
    assert last, "last notification message must be non-empty"
    # The static readiness reason wraps via the i18n shim — match
    # loosely so future translations don't break the test.
    assert any(
        keyword in last.lower() for keyword in ("voice", "unavailable", "replay")
    ), f"reason should reference replay/voice readiness; saw: {last!r}"


def test_replay_failure_carries_exception_message(real_qml_window):
    """The synthesis-exception path: replayMessage emits a reason
    derived from the underlying exception text. Assert the QML
    overlay model receives that text.
    """
    window = real_qml_window
    qml_root = window._window

    model = _find_notifications_model(qml_root)
    assert model is not None
    initial_count = model.property("count") or 0

    tts = window.tts_loader.tts_service
    tts.set_available(True)
    tts.synthesize_raises = RuntimeError("piper exploded")

    window._append_assistant_message("reply that fails")
    _drain()
    window.replayMessage(0)
    _drain()

    final_count = model.property("count") or 0
    assert final_count > initial_count, (
        "showPassiveNotification should have appended a row for the "
        "synthesis-exception path"
    )
    messages = _read_notification_messages(qml_root, model)
    last = messages[-1]
    assert "piper exploded" in last, (
        "synthesis-exception reason must round-trip through to the "
        f"Kirigami overlay model; saw: {last!r}"
    )
