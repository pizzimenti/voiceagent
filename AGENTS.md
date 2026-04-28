# AGENTS.md

## Review guidelines

- Treat startup hangs, blank windows, or network/model refresh work that blocks first paint as P1.
- Treat regressions in the two supported layouts as P1: compact under 250px and stacked medium mode up to the window's maximum width (`Kirigami.Units.gridUnit * 49`). The window is non-resizable above that cap; the prior large horizontal-tiling layout has been removed.
- Treat clipping of the session controls, conversation pane, or microphone control at window edges as P1.
- Treat conversation-turn lifecycle regressions as P1. A user turn should keep one bubble from draft transcription through sent/final text. Bubbles are reserved for final user and assistant content; assistant thinking/progress belongs in status text and the microphone control. The opt-in verbose log mode may surface pipeline status (Transcribing, Thinking, Generating voice, Speaking) inline as plain styled text — these are not bubbles and do not weaken the rule.
- Treat message ordering regressions as P1. The finalized user bubble must remain above its corresponding assistant bubble.
- For QML/UI changes, verify `./voiceagent-compiletest.sh` passes. For startup-flow changes, verify the app window appears without freezing on launch.

## Test gates and what they cover

Three headless gates layer up; run them in order from cheapest to
most thorough:

1. **`pytest tests/`** — Python-side logic + pytest-qt interaction
   tests against a stubbed QML engine. Catches Python-side
   regressions and Python-to-QML signal/slot drift at the property
   surface. ~219 tests, ~3s.
2. **`./voiceagent-compiletest.sh`** — "does the QML load?" gate.
   qmllint each component standalone, then load `MainWindow.qml`
   into a *real* `QQmlApplicationEngine` against a *real*
   `MainWindow` instance via `tests.fakes.build_compiletest_window`.
   Catches QML/property surface drift, missing imports, malformed
   bindings.
3. **`./voiceagent-qatest.sh`** — "does the QML behave correctly?"
   gate. Runs `pytest tests/` first (so a Python regression fails
   fast), then runs the Qt Quick Test suite under `tests/qml/`
   via `tests/qml/qmltest_main.py` (a `QtQuickTest.QUICK_TEST_MAIN_WITH_SETUP`
   driver that registers `i18nCtx` on the test engine before any
   `tst_*.qml` loads). Covers form-layout shape, page-header
   actions, scroll-mode branching, and (via
   `tests/test_replay_toast.py`) the replay-failure toast wiring
   round-trip into the Kirigami passive-notifications overlay.
4. **`./voiceagent-visualtest.sh`** — "does the QML LOOK right?"
   gate. Renders `MainWindow` against `QT_QPA_PLATFORM=offscreen`
   via `QQuickWindow.grabWindow()` at multiple combinations of
   `QT_SCALE_FACTOR` (1.0 / 1.25 / 1.5 — Plasma's common HiDPI
   settings) and logical width (400 / 600 / 800 / 1000 / 1200 px),
   saving 15 PNGs under `screenshots/` (gitignored). Catches layout
   regressions that only surface at specific scale × width
   combinations. The driver is `tests/visual/visual_smoke.py`. Use
   when changing responsive thresholds, FormLayout structure, or
   anything that touches `Kirigami.Units.gridUnit`-dependent
   sizing.

The user's manual smoke test reduces to subjective items only:
animation feel (does the mic pulse breathe at the right tempo) and
visual rhythm judgments that humans see better than diff tools.
Behavioral and structural checks the manual smoke previously
covered are now in the automation suite.

## KDE/QML implementation memory

- Prefer stable `QAbstractListModel` objects for live QML lists. Avoid replacing `QVariantList` values for frequently changing views because delegate rebuilds can reset `contentY`, disturb current index, and cause visible jumps.
- For sticky conversation scrolling, keep one owner for scroll state. Track whether the user is attached to the tail, compute bounds with `originY`, and snap with explicit `contentY` only when sticky mode is active.
- Do not route wheel scrolling through `Flickable.flick()` unless the sticky state machine is designed around Flickable movement events. Native flick movement can emit `movementStarted` / `movementEnded` and accidentally detach bottom-stick behavior.
- Keep status/progress separate from conversation bubbles. Operational states such as `Thinking`, transcription, model loading, and playback belong in status rows, the microphone control, or — when the user has enabled verbose log mode — as inline plain-text status entries in the transcript. Bubbles remain reserved for final user and assistant content.
- Treat `voiceagent-buildtest.sh` as a long-running app launcher. If another Voice Agent instance or lock exists, assume it may be intentional; do not kill the process or remove the runtime lock without explicit user approval.
- Keep network/model refreshes off the first paint path. Launch the QML window first, then defer autoconnect, model refresh, and heavy backend work.
