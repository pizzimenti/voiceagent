# Voice Agent — forward worklist

Tracked, forward-looking worklist for VoiceAgent. **Delete entries as
they ship.**

The high-priority bug list that previously lived here — TTS first-paint
blocking, VoiceController thread-safety, AudioPlayer race, LlmController
stale-refresh ordering, download verification (layers 1 + 4), TTS
`is_available` inconsistency — all landed in PR #6 (v0.3.2). The
infrastructure-hardening sweep (clean-shutdown bounded join,
`__init_subclass__` enforcement, closure-local `single_instance`
buffers, hoisted test helpers, `ruff` dev extra, `QApplication`
fixture, standalone qmllint, `catalog_refresh_settled` signal, ORT
`DISABLE_ALL` for verifier, atomic `voices.json` writes,
`known_voice_names` cache + lock, root-deletion guard) landed across
PRs #11, #12, #13. Download verification layers 2 (size) and 3
(md5 / sha256) landed in v0.7.0 (PR #14). v0.8.0 then drained the rest
of the FOLLOWUPS roadmap across PRs #16–#24: SHA-pinned download
verification, post-first-frame deferral helper, compiletest stub
elimination via real-MainWindow, `required property var voiceAgent`
migration, `ConversationTurnCoordinator` extraction (folds in the
former `ConversationLogController` design), three new QML components
(CatalogList / MicButtonFrame / SessionSetupPane), KDE polish bundle
(FormLayout / i18n / Kirigami.Action), inertial wheel-scroll mode-
switch, and replay-failure toast.

## QA infrastructure

- **v0.8.1 (2026-04-28) — QA automation infrastructure shipped.**
  `voiceagent-qatest.sh` runs the headless QML/UI suite (Quick Tests
  via `tests/qml/` + pytest-qt interaction tests). The user's manual
  smoke test reduces to the subjective items below.

### User smoke required (subjective only)

These behaviors don't have automation analogues — they're
visual/animation feel calls:

- **Mic-button pulse breathing tempo** — the SequentialAnimation
  pulse has the right rate and amplitude. Automation can verify the
  animation runs; humans must judge whether it feels right.
- **Compact-vs-medium layout shaping** — does the form-stack
  collapse at the responsive breakpoint feel natural, do controls
  have enough breathing room, does no widget visually clip at the
  smallest supported width.
- **Conversation-pane visual rhythm** — bubble spacing, font
  weights, color contrast for the assistant-vs-user distinction
  under both light and dark themes.

Everything else the v0.8.0 smoke checklist covered (form labels,
header actions, scroll mode behavior, replay-failure toast firing)
is now in the automated suite.

## UI rough edges (deferred from v0.8.x responsive sweep)

These shipped as-is across v0.8.0–v0.8.6 with the intent to refine in
a later cycle. The "Responsive layout policy" section of `AGENTS.md`
documents the shipped behavior; this is the punch list of what's
known to be off.

- **`SessionSetupPane.qml` "Loaded Model:" label can left-clip** at
  the narrow end of mediumMode (just above the gridUnit × 40
  breakpoint). Form has no headroom for the longest label at the
  smallest mediumMode width. Either tighten the breakpoint another
  notch or reduce control `Layout.preferredWidth` further.
- **Medium-mode mic button can visually overlap the URL row's
  Connect button** at the same narrow end of mediumMode. Same root
  cause — form crowding the mic frame at the breakpoint floor. The
  cleanest fix is the single-page-level mic-button refactor below
  (since the mic could then leave a guaranteed gap).
- **Compact ↔ medium mic-button "slide up and right" animation.**
  The user requested that the mic button slide between positions
  during a `compactMode` flip (bottom-of-conversation in compact →
  right-of-form in medium). This requires a single page-level mic
  button instance with animatable `x`/`y`/`width`/`height` bound to
  the current mode, replacing the current two-instance approach
  (one mic in `ConversationPane.qml`, one in `SessionSetupPane.qml`).
  Roughly 150-line refactor; it would also resolve the mic-overlap
  issue above.

## Future feature work (lower priority)

- **Kirigami dialog/page for model management.** The Model Manager is
  currently an inline `Window` inside `MainWindow.qml`. Kirigami's
  dialog/page conventions would integrate better with the rest of the
  QML tree.

## Deferred indefinitely

- **`model_loader.py:50` — `_emit_initial_state` third branch.**
  TTS has `_status_idle_prompt` for the unselected case; Whisper does
  not. Today the Whisper backend's `selected_item` is always populated
  by config defaults, so adding the branch would be dead code — defer
  until/unless the Whisper backend can actually surface a no-selection
  state.
