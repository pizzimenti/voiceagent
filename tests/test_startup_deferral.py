"""Tests for `voiceagent.startup_deferral.schedule_after_first_frame`.

The helper has three correctness obligations:

1. Connect to `frameSwapped` and run `work` exactly once on the
   first emission, even if the signal subsequently fires more times.
2. Self-disconnect after the first emission so a long-lived window
   doesn't accumulate stale slots.
3. Fall back to `QTimer.singleShot(0, work)` when `window` is None
   or doesn't expose a `frameSwapped` signal — production never
   relies on the fallback, but defensive call sites must not be
   silently dropped during teardown.

We use a minimal `QObject` subclass with a `Signal()` named
`frameSwapped` rather than a real `QQuickWindow` — `frameSwapped` is
the only API the helper touches.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Ensure the worktree's `src/` resolves before any editable install.
_HERE = Path(__file__).resolve().parent
_SRC = _HERE.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

# Headless Qt for environments that didn't already set this.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QObject, Signal

from voiceagent.startup_deferral import schedule_after_first_frame


class _FakeQuickWindow(QObject):
    """Minimal stand-in for `QQuickWindow` exposing only `frameSwapped`.

    `schedule_after_first_frame` only ever touches `.frameSwapped`, so
    a real `QQuickWindow` (which requires an OpenGL context to
    actually swap) is not necessary. The signal carries no payload
    because Qt's real `QQuickWindow.frameSwapped()` is also payload-
    less.
    """

    frameSwapped = Signal()


def _drain_events(rounds: int = 5) -> None:
    """Pump Qt's event loop a few times so queued signal callbacks land."""
    app = QCoreApplication.instance()
    for _ in range(rounds):
        app.processEvents()


def test_helper_connects_and_invokes_on_first_frame_swap(qtbot):
    """The first `frameSwapped` emission triggers the work callable."""
    window = _FakeQuickWindow()
    calls: list[int] = []

    schedule_after_first_frame(window, lambda: calls.append(1))

    # Pre-condition: nothing has fired yet.
    assert calls == []

    window.frameSwapped.emit()
    _drain_events()

    assert calls == [1]


def test_helper_invokes_work_exactly_once_on_repeated_emissions(qtbot):
    """Subsequent `frameSwapped` emissions must not re-fire `work`."""
    window = _FakeQuickWindow()
    calls: list[int] = []

    schedule_after_first_frame(window, lambda: calls.append(1))

    # Emit several times in a row — `frameSwapped` is per-frame in
    # production. We must still see only a single `work` invocation.
    for _ in range(5):
        window.frameSwapped.emit()
    _drain_events()

    assert calls == [1]


def test_helper_disconnects_after_first_callback(qtbot):
    """After the first emission, the helper's slot is released.

    `QObject.receivers(signal)` returns the live slot count for a
    given signal. After the first `frameSwapped` emission the helper
    should have disconnected itself, dropping the count back to 0
    (or 1 if a sibling connection exists — we add one to confirm
    only the helper's slot is gone).
    """
    window = _FakeQuickWindow()

    # A sibling slot stays connected throughout — its presence confirms
    # we're not just measuring "all slots gone" by accident.
    sibling_calls: list[int] = []
    window.frameSwapped.connect(lambda: sibling_calls.append(1))

    helper_calls: list[int] = []
    schedule_after_first_frame(window, lambda: helper_calls.append(1))

    # Both slots connected: helper + sibling.
    assert window.receivers("2frameSwapped()") == 2

    window.frameSwapped.emit()
    _drain_events()

    # Helper ran once and self-disconnected; sibling stays.
    assert helper_calls == [1]
    assert sibling_calls == [1]
    assert window.receivers("2frameSwapped()") == 1, (
        "helper did not disconnect after first callback"
    )

    # Subsequent emissions only reach the sibling.
    window.frameSwapped.emit()
    _drain_events()
    assert helper_calls == [1]
    assert sibling_calls == [1, 1]


def test_helper_falls_back_to_singleshot_when_window_is_none(qtbot):
    """`None` window → fall back to `QTimer.singleShot(0, work)`.

    The fallback runs on the next event-loop tick, not after a frame
    swap (there's no window to swap), but it must still execute.
    """
    calls: list[int] = []

    schedule_after_first_frame(None, lambda: calls.append(1))

    # The `singleShot(0, ...)` fires on the next event-loop turn.
    _drain_events(rounds=10)

    assert calls == [1]


def test_helper_falls_back_when_object_has_no_frame_swapped(qtbot):
    """Window-shaped object without `frameSwapped` → fallback path.

    Defensive: if `engine.rootObjects()[0]` ever isn't a
    `QQuickWindow` (e.g., during shutdown the QML engine clears, or
    a future refactor swaps the root type), the helper must still
    run the work — silently dropping it would mean the LLM
    autoconnect or voice catalog refresh never happens.
    """
    plain_object = QObject()  # no `frameSwapped` attribute
    calls: list[int] = []

    schedule_after_first_frame(plain_object, lambda: calls.append(1))
    _drain_events(rounds=10)

    assert calls == [1]


def test_helper_swallows_exceptions_from_work(qtbot):
    """A raising `work` must not propagate into Qt's signal dispatch.

    PySide6's signal machinery generally swallows slot exceptions, but
    we wrap explicitly so the failure mode is logged at exception
    level and the connection still self-disconnects on entry.
    """
    window = _FakeQuickWindow()

    def _boom() -> None:
        raise RuntimeError("simulated work failure")

    schedule_after_first_frame(window, _boom)

    # Emitting must not raise from the test's perspective.
    window.frameSwapped.emit()
    _drain_events()

    # Subsequent emissions must still be no-ops (slot disconnected
    # before `work` ran).
    follow_up: list[int] = []
    schedule_after_first_frame(window, lambda: follow_up.append(1))
    window.frameSwapped.emit()
    _drain_events()

    assert follow_up == [1]
