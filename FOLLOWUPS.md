# Voice Agent — forward worklist

Tracked, forward-looking worklist for VoiceAgent. **Delete entries as
they ship.**

The high-priority bug list that previously lived here — TTS first-paint
blocking, VoiceController thread-safety, AudioPlayer race, LlmController
stale-refresh ordering, download verification (layers 1 + 4), TTS
`is_available` inconsistency — all landed in PR #6 (v0.3.2). What
remains is feature-shaped work plus lower-severity review nits.

## Future feature work

### Implement true inertial scrolling

`MainWindow.qml:83 scrollList()` currently does direct `contentY`
assignment with bounds-checking. CHANGELOG was corrected accordingly
in PR #5. Designing a real inertial implementation that preserves
sticky-bottom behavior is non-trivial (per AGENTS.md, native
`Flickable.flick()` can detach the sticky-bottom state machine).
Worth scoping if user feedback asks for it.

### Download verification — layers 2 and 3

PR #6 landed layer 1 (`.aria2` sidecar rejection) and layer 4
(Piper smoke-load via `onnxruntime.InferenceSession`). The middle
layers are still TODO:

- **Layer 2 — file size vs manifest.** Piper voices have a
  `voice.json` sidecar with expected size; HF model files have
  `.json` metadata. Where authoritative size is available, mismatch
  is a hard fail before the smoke-load even runs.
- **Layer 3 — SHA-256 verification.** HF model files have `sha256` in
  the LFS pointer; piper-tts repo has hashes per voice. Compute on
  disk, compare, fail closed.

Wire-in points: `ParallelItemLoader._verify_download` already exists
as the layered hook; subclasses would extend it. Keep the cheap
layers (1, 2) in the default impl and the expensive layers (3, 4) in
backend overrides.

## Deferred review findings (PR #6)

CodeRabbit's first pass on PR #6 surfaced 11 inline comments; five
landed in the PR itself (vocabulary in `artifact_paths`, fail-closed
onnxruntime smoke-load, refresh-flag reset on worker error,
flag-set ordering vs eager snapshot, Ruff E712). The rest are
deferred:

- **`parallel_item_loader.py:321`** — `_cleanup_failed_download`
  unlinks each artifact path, but for nested-layout backends (like
  Whisper, where artifacts live under
  `<model_root>/<item_name>/`) the now-empty directory is left
  behind. Best-effort `rmdir` on parents that became empty after
  unlink. Not catastrophic — the next pass treats the directory as
  empty — but cleaner.
- **`parallel_item_loader.py:360`** — Documentation: clarify in a
  comment near the `_verify_download` callsite that this hook is
  *post-download*, not a generic readiness check. Subclass
  overrides like Piper's smoke-load run a one-shot
  `onnxruntime.InferenceSession` (~50–100 ms) and shouldn't be
  invoked from any general "is this ready?" path.
- **`services/tts.py:94`** — `_fetch_and_cache_voice_names` writes
  `voices.json` directly via `cache_path.write_text(...)`. If the
  process is killed mid-write, the cache can end up truncated and
  the next `available_voice_names` call silently loses the entire
  cached catalog (the JSON-parse fallback returns an empty set).
  Tmp-write-then-rename for atomic replacement.
- **`tts_loader.py:210`** — Optional perf tweak: pass a
  pre-configured `onnxruntime.SessionOptions` with
  `graph_optimization_level=ORT_DISABLE_ALL` to the smoke-load
  `InferenceSession`. Verification only checks that the file
  parses and produces a valid graph; full optimization isn't
  needed and wastes 20–50 ms per voice install.
- **`tests/test_tts_catalog_first_paint.py:132`** — Helper
  duplication: `_FakeResponse` is redefined three times,
  `_boom` twice. Hoist into a module-level fixture.
- **`tests/test_tts_catalog_first_paint.py:232`** — `_wait_for(qtbot,
  lambda: loader.catalog_refresh_scheduled, ...)` is essentially a
  no-op because the flag is set synchronously before submit (the
  `processEvents()` loop below is what actually drains the work).
  Replace with a wait on a post-completion sentinel signal once
  one is exposed.

## Deferred review findings (PR #5)

Lower-severity items from the PR #5 review that were out of scope for
that PR. Still valid; apply opportunistically when touching the
surrounding code.

### From Codex

- **`MainWindow.qml:21`, `window.py:190`** — Catalog counts rebuild
  full `QVariantList`s and perform per-item availability checks on
  every `ui_changed`. At ~20 items it's immeasurable, but it's wasted
  work; cache and invalidate on actual catalog deltas.
- **`voiceagent-compiletest.sh:49,329`** — QML compile-test stub
  `voiceAgent` has slot/property signatures that drift from the real
  `MainWindow`, and the script doesn't lint the extracted QML
  components (`MicButton.qml`, `ConversationPane.qml`,
  `WaveformMeter.qml`) directly — only via `MainWindow.qml`. Make the
  stub generation programmatic or at minimum add explicit qmllint
  invocations.

### From CodeRabbit

- **`conversation_model.py:60`, `catalog_model.py:45`** —
  `roleNames()` returns the class-level `_ROLE_NAMES` dict directly.
  PySide6 doesn't guarantee Qt treats the returned dict as read-only.
  Same pattern in both files; same fix — return a copy or wrap with
  `MappingProxyType`.
- **`conversation_model.py:91`** — `update_message` does an O(R)
  reverse lookup over `_ROLE_KEYS` per updated key. Inverse map at
  class build time removes the inner loop.
- **`model_loader.py:57`** — `_emit_initial_state` only branches on
  ready/not-ready. TTS has the third branch (`_status_idle_prompt`)
  for the unselected case. If the Whisper backend ever surfaces a
  no-selection state, the user sees "Download …" instead of
  "Select a …". Wire the third branch symmetrically.
- **`parallel_item_loader.py:153`** — `shutdown(wait=False)` lets
  in-flight workers continue. PySide6's queued connections make
  deleted-receiver emissions safe (per CodeRabbit's own analysis on
  `llm_controller.py:217`), but a clean-shutdown story (per-worker
  cancel events, bounded join) is nicer than relying on the safety
  net.
- **`parallel_item_loader.py:235`** — `_status_*` hooks raise
  `NotImplementedError` lazily at call time. Consider an
  `__init_subclass__` check that asserts every override is present
  (standard `@abstractmethod` is awkward because `QObject`'s
  metaclass conflicts with `ABCMeta`).
- **`parallel_item_loader.py:319`** — `_finish_success` emits a
  terminal `item_progress_changed` carrying `DownloadProgress(1, 1, 0)`
  when the worker never reported real progress (the `or 1` fallback).
  Pairs with the round-1 fix landed in PR #5; a deeper design pass on
  whether the terminal tick should exist at all is worth doing.
- **`single_instance.py:65`** — `_buffers` keyed by `id(connection)`.
  Closure-local state would be cleaner: each connection's `readyRead`
  and `disconnected` handlers already capture `connection` by closure,
  so the buffer can live in the closure too.
- **`MicButton.qml`** — Tried CodeRabbit's `required property var
  voiceAgent` suggestion in PR #5 but reverted: it triggers
  first-paint TypeErrors because internal `text:`/`enabled:` bindings
  evaluate before the parent's `voiceAgent: voiceAgent` binding lands.
  A correct fix needs lazy binding evaluation (wrap affected bindings
  in `voiceAgent ? ... : null`, or move them under
  `Component.onCompleted`) before declaring the property `required`.
- **`tests/test_parallel_item_loader.py:354`** — Hoist loader
  construction + shutdown into a pytest fixture so cleanup is
  unconditional even if an assertion fails before `loader.shutdown()`.

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
- Normalize tests to use `QApplication` consistently. Test runs
  currently emit a Qt warning about `QCoreApplication` vs
  `QApplication` because some tests instantiate the bare core app
  while others (or QML-touching code under test) need the full GUI
  application. Pick one (`QApplication`, since QML/widgets are in
  scope) and route every test through a shared fixture.
- Add `ruff` to a `dev` extra in `pyproject.toml` if linting is part
  of the project's expected workflow. (Several CodeRabbit nitpicks
  reference Ruff rules that aren't being enforced locally because
  ruff isn't in the venv.)

## Deferred from PR #7 round-2 review

External round-2 review of PR #7 (the v0.4.0 ui-shaping branch)
surfaced these architectural items. Each is its own cycle.

- **`ConversationTurnCoordinator`.** A small object that owns the
  draft → final → status row ordering for a conversation turn. Today
  `_apply_state` only promotes an existing draft user bubble; if no
  draft exists yet (the user finalized via VAD silence rather than a
  click), pipeline status rows can land before the final user bubble.
  The coordinator would gate status appends on the user-bubble
  finalization being settled.

- **`ConversationLogController` as the only writer to
  `ConversationModel`.** Today `window.py` writes to the model from
  `_apply_state`, `_set_status_message` (just unwound in this PR),
  `_set_error_message`, `_apply_model_status`, `_apply_tts_status`,
  `_on_llm_status_message`, `_append_user_message`, and
  `_append_assistant_message`. Centralizing through a controller
  would make simple/verbose mode policy testable in isolation and
  remove the risk of the next caller re-introducing the same
  duplicate-append regression we just fixed.

- **Custom STT path support in `CatalogModel`.** Backend already
  supports `WHISPER_MODEL=/path/to/model`, but
  `WhisperTranscriber.available_items()`
  (`src/voiceagent/services/stt.py:60`) only lists managed names, so
  the UI catalog and `selectedSttModel`
  (`src/voiceagent/window.py:243`, `:553`) cannot select a direct
  custom path. The path either looks unavailable or gets silently
  replaced by a fallback.

- **`CatalogModel` role extension.** Add `installed`, `loading`,
  `progress`, `downloadable`, and `managed`/`custom` as proper Qt
  roles. Removes the QML-side `QVariantMap`/`Array.indexOf` lookup
  maps and lets the model invalidate per-row instead of rebuilding
  full `QVariantList`s on every `ui_changed`.

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

- **Connect spam-click guard.** `MainWindow.qml:681` allows the
  Connect action when `!llmServerConnected`, even if
  `llmConnectionBusy` is `true`. Each click queues another refresh
  via `_start_refresh()`
  (`src/voiceagent/services/llm_controller.py:271`). Gate the QML
  `enabled` on `!llmConnectionBusy` too.

- **`replayMessage` defensive layer.** Round-2 added a try/except
  and an `is_available()` readiness check; a deeper pass should
  surface synthesis errors via a transient toast or status rather
  than silently logging.

- **Test fixture normalization.** Existing pytest run emits a
  `QApplication is not an instance of qapp_cls` warning because some
  tests use bare `QCoreApplication` while others need
  `QApplication`. Pick `QApplication` in `tests/conftest.py` and
  route every test through a shared fixture.

- **Add `ruff` to the dev extra in `pyproject.toml`.** Several
  CodeRabbit nitpicks reference Ruff rules (BLE001, FBT001, FBT003)
  that aren't enforced locally because ruff isn't in the venv.
  Decide whether linting is part of the project workflow.

- **KDE polish.** Migrate the session-setup grid to
  `Kirigami.FormLayout` (Kirigami's documented pattern for
  settings/control groups), wire user-facing strings through
  `KLocalizedContext` / `i18n()` for i18n readiness, and convert
  more of the inline button bindings to `Kirigami.Action`-based
  command surfaces. Each is small but they should land together so
  the QML reads consistently.
