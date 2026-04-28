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
