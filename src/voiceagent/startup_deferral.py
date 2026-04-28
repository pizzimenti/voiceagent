"""Schedule callables to run after the first QML frame swap.

`MainWindow.show()` historically used `QTimer.singleShot(0, ...)` to
defer work that shouldn't block first paint (LLM autoconnect, Piper
voices.json refresh). The trouble is the same one documented in
`app.py:142-158` for the sounddevice pre-warm: a 0 ms timer fires on
the next event-loop tick, which can land **before** the first frame
swap completes. The deferral was rewritten to a daemon thread for
sounddevice; for Qt-bound work that has to stay on the GUI thread we
need a different primitive.

`QQuickWindow.frameSwapped` is the Qt-blessed signal for this — it
emits after the GPU has finished the swap, so anything connected to
it runs strictly *after* the user has seen the first painted frame.
The helper here connects, runs the callable on the first emission,
and self-disconnects so subsequent frame swaps don't re-fire it.

Falls back to `QTimer.singleShot(0, work)` when the window is None or
doesn't expose `frameSwapped` (e.g., during teardown, or if the QML
root is somehow not a `QQuickWindow`). The fallback preserves the
prior behavior — work still runs, just without the first-frame
guarantee — so call sites are never silently dropped.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QTimer


_logger = logging.getLogger(__name__)


def schedule_after_first_frame(
    window: Any | None,
    work: Callable[[], None],
) -> None:
    """Run `work` once after the next `frameSwapped` emission.

    `frameSwapped` fires every frame, so the slot self-disconnects on
    the first callback to avoid re-firing on every subsequent frame.

    If `window` is None or doesn't expose a `frameSwapped` signal
    (e.g., shutdown teardown, non-Quick root window), fall back to
    `QTimer.singleShot(0, work)` so the call site still runs. The
    fallback is logged at debug level — production never hits it
    because `MainWindow.__init__` always assigns a `QQuickWindow` to
    `self._window`.
    """
    frame_swapped = getattr(window, "frameSwapped", None)
    if frame_swapped is None or not hasattr(frame_swapped, "connect"):
        _logger.debug(
            "schedule_after_first_frame: no frameSwapped on %r; "
            "falling back to QTimer.singleShot(0, work)",
            window,
        )
        QTimer.singleShot(0, work)
        return

    # Mutable container so the inner slot can both read and rewrite the
    # connection handle without `nonlocal` gymnastics across re-entry.
    state: dict[str, Any] = {"connection": None, "fired": False}

    def _on_first_frame() -> None:
        # Defensive: frameSwapped can fire more than once before Qt
        # processes our disconnect. Latch on first invocation.
        if state["fired"]:
            return
        state["fired"] = True
        connection = state["connection"]
        if connection is not None:
            try:
                frame_swapped.disconnect(connection)
            except (TypeError, RuntimeError):
                # Already disconnected, or the underlying QObject is
                # being torn down. Either way, the slot has run; drop
                # the stale handle and proceed.
                pass
        try:
            work()
        except Exception:
            _logger.exception(
                "schedule_after_first_frame: work callable raised"
            )

    state["connection"] = frame_swapped.connect(_on_first_frame)
