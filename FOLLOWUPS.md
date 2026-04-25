# Voice Agent — forward worklist

Tracked, forward-looking worklist for VoiceAgent. **Delete entries as
they ship.**

The high-priority bug list that previously lived here — TTS first-paint
blocking, VoiceController thread-safety, AudioPlayer race, LlmController
stale-refresh ordering, download verification (layers 1 + 4), TTS
`is_available` inconsistency — all landed in PR #6 (v0.3.2). What
remains is feature-shaped work plus lower-severity review nits.

## Future feature work

### Cap horizontal width and drop the large two-column layout

This is meant to be a small-window app. Cap
`Kirigami.ApplicationWindow`'s horizontal size at the medium-mode
upper bound so the window cannot be dragged or maximized into the
existing two-column dashboard layout. Two of the three responsive
views survive (medium, compact); the large two-column dashboard view
is removed entirely.

Concrete shape:

- `MainWindow.qml:7` — add `maximumWidth: Kirigami.Units.gridUnit * 49`
  (just below the current `largeMode` threshold at `gridUnit * 50`).
- Disable the maximize button via the window's `flags:` so the WM
  doesn't let the user expand past the cap.
- Delete `largeMode` (`MainWindow.qml:17`), `dashboardColumns:20`, the
  three-way ternary at `:512`, the entire `largeMicPaneComponent`
  block, the `largeDashboardRow` RowLayout and its three Loaders, and
  the `largeMicPriorityMode` predicate.
- **Coupled doc edit:** `AGENTS.md:6` currently treats regressions in
  the large layout as P1. When this feature lands, drop the
  large-layout clause from that line so AGENTS.md stays aligned with
  the actual product surface.
- Verify the medium and compact layouts still render — the QML should
  shrink meaningfully (~150 lines).

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

- **`conversation_model.py:60`** — `roleNames()` returns the
  class-level `_ROLE_NAMES` dict directly. PySide6 doesn't guarantee
  Qt treats the returned dict as read-only. Defensive: return a copy
  or wrap with `MappingProxyType`.
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
- Add `ruff` to a `dev` extra in `pyproject.toml` if linting is part
  of the project's expected workflow. (Several CodeRabbit nitpicks
  reference Ruff rules that aren't being enforced locally because
  ruff isn't in the venv.)
