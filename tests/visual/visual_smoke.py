"""Visual smoke runner — capture MainWindow renders at multiple widths.

Renders MainWindow against `QT_QPA_PLATFORM=offscreen` so it works
headlessly (no display needed). Saves a PNG per width under
`screenshots/` for visual review.

Use via the wrapper:

    bash voiceagent-visualtest.sh

The wrapper sets `QML_IMPORT_PATH` so Kirigami resolves AND iterates
the capture loop across `QT_SCALE_FACTOR` values so HiDPI / fractional-
scaling regressions surface (Plasma desktops commonly run 125%, 150%,
or 200%; on Wayland these are the user's reality, not a 1.0x baseline).
Scale factor at runtime is read from `VOICEAGENT_VISUAL_SCALE` (set by
the wrapper). Output filenames embed both width and scale.

Output is gitignored — diagnostic artefacts, not committed fixtures.

Why offscreen rendering catches real bugs: layout, font metrics,
`Kirigami.Units.gridUnit` (which scales with `QT_SCALE_FACTOR`),
FormLayout column widths — all evaluate the same under offscreen as
on a real display. What it does NOT match is GPU-accelerated effects
and pixel-perfect font hinting; for layout regressions specifically
it's reliable.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtQuick import QQuickWindow  # noqa: E402  -- registers the type so grabWindow() resolves on engine.rootObjects()[0]
from PySide6.QtWidgets import QApplication  # noqa: E402

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent.parent
sys.path.insert(0, str(_REPO))
sys.path.insert(0, str(_REPO / "src"))

from tests.fakes import build_compiletest_window  # noqa: E402

# Bracket the compactMode breakpoint (25 grid units ≈ 600 px on standard
# Kirigami density) plus typical desktop/laptop widths above it.
WIDTHS = [400, 600, 800, 1000, 1200]
HEIGHT = 700

# Time to let layout + render settle after a resize. Two render frames
# at 60 Hz is ~33 ms; 250 ms is generous to absorb any deferred
# bindings or animations.
SETTLE_MS = 250


def pump(app: QApplication, ms: int) -> None:
    """Pump the event loop for `ms` milliseconds."""
    deadline = time.monotonic() + (ms / 1000.0)
    while time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.005)


def grab(qquick, width: int, height: int, out_path: Path) -> None:
    qquick.setWidth(width)
    qquick.setHeight(height)
    pump(QApplication.instance(), SETTLE_MS)
    img = qquick.grabWindow()
    img.save(str(out_path))
    print(f"saved {width}x{height} -> {out_path.relative_to(_REPO)}")


def main() -> int:
    out_dir = _REPO / "screenshots"
    out_dir.mkdir(exist_ok=True)
    scale_label = os.environ.get("VOICEAGENT_VISUAL_SCALE", "1.0")

    app = QApplication([])
    window = build_compiletest_window()
    qquick = window._window
    qquick.show()

    # Initial render pump so the first capture isn't a half-painted frame.
    pump(app, 400)

    for w in WIDTHS:
        out_path = out_dir / f"scale-{scale_label}_width-{w}.png"
        grab(qquick, w, HEIGHT, out_path)

    # Final settle + clean shutdown.
    pump(app, 100)
    if hasattr(window, "shutdown"):
        try:
            window.shutdown()
        except Exception as exc:
            # Shutdown may complain about cleanup of fakes; not a render concern.
            print(f"shutdown warning (non-fatal): {exc}", file=sys.stderr)

    QTimer.singleShot(0, app.quit)
    app.exec()
    return 0


if __name__ == "__main__":
    sys.exit(main())
