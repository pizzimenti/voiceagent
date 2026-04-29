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

## Responsive layout policy

VoiceAgent has three responsive modes driven by `MainWindow.qml`'s
`compactMode` and `ultraCompactMode` properties (declared on the root
`Kirigami.ApplicationWindow`). Both are gridUnit-based so they scale
correctly with Plasma 100% / 125% / 150% / 200%.

### Modes

| Mode | Trigger | Layout |
| :--- | :--- | :--- |
| **medium** | `width >= gridUnit * 40` | Side-by-side: `SessionSetupPane.qml` (Kirigami.FormLayout: Speech / Voice / LLM URL / Loaded Model) on the left, mic button (~gridUnit × 10 wide) on the right. `ConversationPane.qml` below. Page header: Mute, Voice Models, Theme, Verbose Log actions. |
| **compact** | `width < gridUnit * 40 && height >= gridUnit * 10` | SessionSetupPane hidden. `ConversationPane.qml` fills the page: conversation feed on top + mic button (gridUnit × 5 tall) at the bottom. Page header: drops Voice Models action; rest visible. |
| **ultraCompact** | `compactMode && height < gridUnit * 10` | Conversation feed entirely hidden via `Layout.maximumHeight: 0` collapse. Mic button fills the window with zero padding (hemmed against the window edges). Page header may cramp at the smallest sizes — by design. |

### Window dimensions

- **`minimumWidth: gridUnit * 6`** (~108 px @1.0x). The user explicitly opted into the postage-stamp mic-only-widget extreme. Title bar text may collapse to ellipsis at the narrow end — by design at this size.
- **`minimumHeight: gridUnit * 8`** (~144 px @1.0x). The floor at which the mic button has room for icon + wrapped status text without its bottom curve clipping at the window edge.
- **No upper cap.** Maximize, fullscreen, tiling all work as the compositor permits. v0.8.4 dropped the prior `maximumWidth` / `maximumHeight` caps after they proved more annoying than the alternative.

### Mode transition animation

- 250 ms `Easing.OutCubic` `Behavior on Layout.maximumHeight` on the conversation-feed Item in `ConversationPane.qml`. Animates 100000 ↔ 0 when `ultraCompactMode` flips. The mic button anchor in `ConversationPane.qml` has `Layout.fillHeight: ultraCompactMode`, so as the conversation collapses, the mic *slides up* into the freed vertical space. No dissolve.
- **No opacity fades anywhere** — the user explicitly preferred motion over dissolves.
- Compact ↔ medium animates via a single page-level `MicButtonFrame` declared in `MainWindow.qml`. Each pane (`SessionSetupPane.qml`, `ConversationPane.qml`) exposes a `micAnchor` alias — an empty Item that reserves the layout slot the mic occupies in that pane. The page-level mic binds `x` / `y` / `width` / `height` to whichever pane's `micAnchor` is active for the current mode, mapped via `mapToItem(parent, …)`. `Behavior on x` / `y` / `width` / `height` (250 ms `Easing.OutCubic`) smooths the transition when the breakpoint flips, producing the slide-between-positions animation.

### Mic button sizing

- **Icon size:** `Math.max(18, Math.min(48, height * 0.32))` — scales with frame height so short windows shrink the icon instead of overflowing.
- **Font size:** `Math.max(10, Math.min(14, height * 0.10))` — same scaling logic.
- **Status label wraps** via `wrapMode: Text.WordWrap` (custom `contentItem` in `MicButton.qml`). Long strings line-break ("No model loaded" → two lines) instead of truncating to "No model lo...".

### Form layout (medium mode)

- `SessionSetupPane.qml` uses `Kirigami.FormLayout` with `wideMode: !compactMode` for the responsive collapse.
- Form ComboBoxes have `Layout.preferredWidth` (gu × 14 / gu × 16) but **no `Layout.fillWidth`** — preventing them from starving the right-aligned label column.
- The inner `RowLayout` uses `anchors.fill: parent` (respects Pane padding), not `width: parent.width` (which overshoots the padding region).

### Verifying responsive layout

- **Headless:** `./voiceagent-visualtest.sh` captures 15 screenshots per scale × 5 widths plus 5 short-window captures per scale (3 scales × 10 = 30 PNGs). Output under `screenshots/` (gitignored). Driver: `tests/visual/visual_smoke.py`.
- **Manual smoke:** launch `.venv/bin/voiceagent`, resize through the breakpoints. Watch for:
  1. Form labels fully visible at the gridUnit × 40 medium-mode floor (no left-clipping).
  2. Mic button bottom not clipped at minimum-height window.
  3. Conversation feed collapses smoothly (~250 ms) at the ultraCompact threshold; mic button slides up into the freed space.
  4. Title bar shows "Voice Agent" + Mute action down to gridUnit × 18 width or so. Below that the WM title bar may overflow to "..." — that's fine.
  5. Mic status text wraps at narrow widths ("No model loaded" on two lines) instead of eliding.

## KDE/QML implementation memory

- Prefer stable `QAbstractListModel` objects for live QML lists. Avoid replacing `QVariantList` values for frequently changing views because delegate rebuilds can reset `contentY`, disturb current index, and cause visible jumps.
- For sticky conversation scrolling, keep one owner for scroll state. Track whether the user is attached to the tail, compute bounds with `originY`, and snap with explicit `contentY` only when sticky mode is active.
- Do not route wheel scrolling through `Flickable.flick()` unless the sticky state machine is designed around Flickable movement events. Native flick movement can emit `movementStarted` / `movementEnded` and accidentally detach bottom-stick behavior.
- Keep status/progress separate from conversation bubbles. Operational states such as `Thinking`, transcription, model loading, and playback belong in status rows, the microphone control, or — when the user has enabled verbose log mode — as inline plain-text status entries in the transcript. Bubbles remain reserved for final user and assistant content.
- Treat `voiceagent-buildtest.sh` as a long-running app launcher. If another Voice Agent instance or lock exists, assume it may be intentional; do not kill the process or remove the runtime lock without explicit user approval.
- Keep network/model refreshes off the first paint path. Launch the QML window first, then defer autoconnect, model refresh, and heavy backend work.

## Subjective smoke (user-only, no automation)

These are the items the user judges by eye when validating a release; automation can confirm the animation runs but not whether it *feels* right. Worth a manual pass after any change in the relevant area.

- **Mic-button pulse breathing tempo** — the `SequentialAnimation` pulse rate / amplitude. Look + feel call.
- **Compact-vs-medium layout shaping** — does the form-stack collapse at the responsive breakpoint feel natural; do controls have breathing room; does no widget visually clip at the smallest supported width.
- **Conversation-pane visual rhythm** — bubble spacing, font weights, color contrast for the assistant-vs-user distinction under both Breeze Light and Dark.
- **Slide animation on compact ↔ medium flip** (v0.9.0+) — the page-level mic should glide between bottom-of-conversation and right-of-form over ~250 ms.
- **Theme palette across modes** — switch Auto / Light / Dark via the toolbar toggle and confirm bubbles, mic button, toolbar icons, and status purple all repaint cleanly in each mode.
