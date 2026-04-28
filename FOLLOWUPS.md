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
PRs #11, #12, #13. Download verification layers 2 (size vs manifest)
and 3 (md5 / sha256 vs manifest) landed in v0.7.0 (PR #14). What
remains is UI / design judgment work.

## Future feature work

### Implement true inertial scrolling

`MainWindow.qml:83 scrollList()` currently does direct `contentY`
assignment with bounds-checking. CHANGELOG was corrected accordingly
in PR #5. Designing a real inertial implementation that preserves
sticky-bottom behavior is non-trivial (per AGENTS.md, native
`Flickable.flick()` can detach the sticky-bottom state machine).
Worth scoping if user feedback asks for it.

## Future feature work

### Pin Piper download + verify manifest to the same revision

`_download_voice` and `artifact_manifest` both resolve against the
`main` branch of `rhasspy/piper-voices`. If upstream pushes a voice
update in the few-second window between aria2 fetching the file
bytes and the manifest refresh, layer 2 / 3 verification will
fail-close against a healthy download. Layer 4 (smoke-load) catches
real corruption so the user can retry, but the false-positive is
visible.

Proper fix needs design judgment between three viable shapes:

- **Pin to a SHA.** Capture upstream commit SHA at download start;
  rewrite URLs to `https://huggingface.co/rhasspy/piper-voices/
  resolve/<sha>/<filepath>`; refetch manifest from the same SHA.
  Most robust, biggest URL refactor.
- **Capture-at-start.** Snapshot manifest payload before aria2
  fetches files; thread the snapshot through the loader to
  `_verify_download`. Avoids second HTTP call entirely. Touches
  the verifier interface.
- **Piper layers 2/3 advisory.** Make Piper override convert
  size/md5 mismatch to warning logs; let layer 4 be the
  authoritative gate. Smallest change but loses defense-in-depth
  for the (rare) bytes-look-fine-but-wrong-version case.

Tracked from PR #14 round-2 review (CodeRabbit P2).

## Deferred review items still open

Items that survived the 0.6.3 / 0.6.4 hardening sweeps. Each needs a
design or UI judgment, so they sit until the matching cycle picks
them up.

- **`model_loader.py:50` — `_emit_initial_state` third branch.**
  TTS has `_status_idle_prompt` for the unselected case; Whisper does
  not. Today the Whisper backend's `selected_item` is always
  populated by config defaults, so adding the branch would be dead
  code — defer until/unless the Whisper backend can actually surface
  a no-selection state.

- **`MicButton.qml` — `required property var voiceAgent`.** Tried
  CodeRabbit's suggestion in PR #5 but reverted: it triggers
  first-paint TypeErrors because internal `text:`/`enabled:`
  bindings evaluate before the parent's `voiceAgent: voiceAgent`
  binding lands. A correct fix needs lazy binding evaluation (wrap
  in `voiceAgent ? ... : null`, or move under
  `Component.onCompleted`) before declaring the property `required`.
  Needs UI verification post-change.

- **`voiceagent-compiletest.sh` programmatic stub.** PR #12 added
  standalone qmllint of `MicButton.qml` / `ConversationPane.qml`,
  but the in-script `voiceAgent` stub still drifts from the real
  `MainWindow` slot/property surface. Generate the stub
  programmatically from the real Q_PROPERTY surface to remove the
  silent-drift risk entirely.

## Forward-looking refactors (not bugs)

Lower-priority code-organization improvements — not load-bearing,
not user-visible.

- Extract a reusable `qml/CatalogList.qml` to replace the duplicated
  STT/TTS catalog ListView delegates inside the Model Manager.
- Extract the repeated pulsing mic frame (`Item { id: ...MicButtonFrame }`
  with its `SequentialAnimation`) into a sibling component alongside
  `MicButton.qml`. Currently the frame still lives inline at each
  callsite.
- Add MainWindow-level integration tests covering: conversation
  ordering across STT→LLM→TTS, draft-to-final user-bubble promotion,
  and the "thinking is status, not bubble" invariant from AGENTS.md.

## Deferred from PR #7 round-2 review

External round-2 review of PR #7 (the v0.4.0 ui-shaping branch)
surfaced these architectural items. Each is its own cycle.

- **`ConversationLogController` as the only writer to
  `ConversationModel`.** Today `window.py` writes to the model from
  `_apply_state`, `_set_status_message` (just unwound in this PR),
  `_set_error_message`, `_apply_model_status`, `_apply_tts_status`,
  `_on_llm_status_message`, `_append_user_message`, and
  `_append_assistant_message`. Centralizing through a controller
  would make simple/verbose mode policy testable in isolation and
  remove the risk of the next caller re-introducing the same
  duplicate-append regression we just fixed.

- **Extract QML components.** `MainWindow.qml` is ~800 lines and
  `window.py` is ~860 lines. Pull `CatalogList.qml` (replaces the
  duplicated STT/TTS catalog ListViews), `SessionSetupPane.qml`,
  `MicButtonFrame.qml` (the inline pulsing-frame Item), and consider
  a Kirigami dialog/page for model management. Kirigami's
  `FormLayout` is the documented pattern for the settings/control
  groups inside `sessionSetupGrid`.

- **Tighten QML component dependencies.** `MicButton.qml` and
  `ConversationPane.qml` rely on ambient `voiceAgent` and
  `ApplicationWindow.window`. Pass them as `required property` so
  the components are testable in isolation (and the compiletest stub
  doesn't have to be kept in sync separately).

- **MainWindow-level integration tests.** Cover simple-vs-verbose
  transcript content per mode, draft-to-final user-bubble ordering
  with and without a draft, replay error handling
  (`replayMessage` raise paths), custom STT path selection, and the
  connect spam-click guard below.

- **`replayMessage` defensive layer.** Round-2 added a try/except
  and an `is_available()` readiness check; a deeper pass should
  surface synthesis errors via a transient toast or status rather
  than silently logging.

- **KDE polish.** Migrate the session-setup grid to
  `Kirigami.FormLayout` (Kirigami's documented pattern for
  settings/control groups), wire user-facing strings through
  `KLocalizedContext` / `i18n()` for i18n readiness, and convert
  more of the inline button bindings to `Kirigami.Action`-based
  command surfaces. Each is small but they should land together so
  the QML reads consistently.

- **True post-first-frame deferral helper.** `app.py:118` documents
  why a 0 ms `QTimer.singleShot(0, ...)` can fire on the next
  event-loop tick before the first frame swap completes — that
  deferral was rewritten to a daemon thread for the sounddevice
  pre-warm. But `window.py:182` still uses the same pattern for
  LLM autoconnect and the TTS catalog refresh. Both are
  lightweight / off-thread today so it is not currently a P1 bug,
  but it is the same anti-pattern. Build a small helper that
  schedules work after the first frame swap (Qt has
  `QQuickWindow::frameSwapped` for exactly this) or off-thread,
  and route both call sites through it. KDE startup-best-practice
  alignment.
