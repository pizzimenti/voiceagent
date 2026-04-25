# Voice Agent — forward worklist

Tracked, forward-looking worklist for VoiceAgent. This file replaces the
PR #4 review tracker that lived here previously — every P2/P3 item from
that tracker landed in PR #5 (the cleanup PR). What remains is split by
priority. **Delete entries as they ship.**

## Higher priority

These are real bugs surfaced during PR #5 review, not in scope for that
PR but worth tackling early in the next cycle.

### P1 — TTS catalog fetch blocks first paint

`src/voiceagent/window.py` builds the TTS catalog before QML loads, and
`src/voiceagent/services/tts.py` (around line 279) fetches `voices.json`
over the network with a 5 s timeout when the local cache is missing. On
a cold launch with no cache, the QML window doesn't paint until that
timeout resolves.

**Fix shape:** start with whatever's already on disk (cached / installed
/ configured), let QML paint, then refresh the catalog asynchronously
and emit a catalog-changed signal when the network fetch lands. This
also satisfies AGENTS.md's "keep network/model refreshes off the first
paint path" rule.

### P2 — VoiceController mutates owner-thread state from worker callbacks

Same class of bug `ParallelItemLoader` just fixed in PR #5, but in a
different file. `src/voiceagent/controller.py:190` mutates
`_active_pipeline_count` from an executor callback;
`src/voiceagent/controller.py:403` mutates `_partial_inflight`
similarly. Wrap both in the queued-signal bridge pattern that
`LlmController` (also landed in PR #5) already uses: an internal
`Signal` connected with `Qt.QueuedConnection`, sole writer on the
owner thread.

### P2 — AudioPlayer can race old/new playback

`src/voiceagent/services/playback.py:47` stops then reuses one
`_stop_event` between playbacks; `:93` only joins for 1 s. A slow
worker from a prior playback can overlap a new one or emit stale
`finished` signals that confuse the controller.

**Fix shape:** per-playback generation IDs and per-worker stop events.
Each `play_file` call mints a new generation; the old worker's
`finished` signal handler checks generation before mutating state.

### P2 — Verify model/voice downloads before marking ready

Observed during the PR #5 build test: a partial Piper voice download
left an `.onnx` on disk alongside an aria2 `.aria2` control sidecar.
The loader emitted `load_completed`, the user selected the voice, and
the first TTS attempt blew up at runtime with
`onnxruntime InvalidProtobuf` → `wave.Error: # channels not specified`.

The loader currently trusts whatever the backend's `download_item`
returns. Add layered verification in the loader's success path,
ordered cheap → expensive:

1. **Reject `.aria2` leftovers.** If `<file>.aria2` exists next to the
   target after `download_item` returns, the download did not finish.
   Treat as failure; clean up partials before any retry. This alone
   would have caught the observed bug.
2. **Compare file size to manifest.** Piper voices ship a `voice.json`
   sidecar; Whisper/CT2 models have a `config.json` and HF metadata.
   Where an authoritative expected size is available, mismatch is a
   hard fail.
3. **SHA-256 verification** when the source publishes checksums (HF
   model files have `sha256` in the LFS pointer; piper-tts repo has
   hashes per voice). Compute on disk, compare, fail closed.
4. **Smoke-load the artifact** before marking ready: for Piper,
   `onnxruntime.InferenceSession(path)` in a try/except; for Whisper,
   instantiating `WhisperModel(path)` briefly. Most expensive layer
   but the most authoritative.

Where to wire it in:

- `src/voiceagent/parallel_item_loader.py` — add a `_verify_download(name)`
  hook called between `download_item` returning and `load_completed`
  being emitted. Default implementation runs layer 1 only.
- `src/voiceagent/services/tts.py` (Piper backend) — override the hook
  to add layers 2–4.
- The Whisper backend — same.

On verification failure: delete the partial files (model + `.aria2`),
emit `load_failed(name, message)` with a useful message, and refresh
the catalog row so the UI shows it as not installed.

Tests:

- Synthetic test in `tests/test_parallel_item_loader.py`: backend
  stub's `download_item` "succeeds" but leaves a `.aria2` sidecar in
  a tmpdir → loader emits `load_failed`, not `load_completed`.
- Backend-level test for the smoke-load layer: write a 100-byte
  garbage-bytes file to a tmpdir and confirm the verifier rejects it.

### P2 — LlmController stale refresh prematurely clears `connection_busy`

`src/voiceagent/services/llm_controller.py:332` calls
`_set_connection_busy(False)` *before* the stale-refresh return at
:333–336. With two rapid connect/refresh attempts, the earlier
(stale) completion's `clear → False` emission lands while the newer
refresh is still running, and the newer completion's clear is a
no-op. UI sees a brief "connected, idle" state during the gap.

**Fix shape:** gate the busy-clear on whether the resolving request
ID is still the active one. Move the busy-clear into the same branch
as the actual update (after the stale-discard return).

### P3 — TTS `is_available` inconsistent with `is_item_available`

`src/voiceagent/services/tts.py:40` reports `is_available=True` when
only `.onnx` exists; `:79` correctly requires both `.onnx` and
`.onnx.json`. Pairs naturally with the corrupt-download verification
work above — fix both in one pass.

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
assignment with bounds-checking. CHANGELOG previously claimed
"inertial scrolling" — that claim was corrected in PR #5. Designing a
real inertial implementation that preserves sticky-bottom behavior is
non-trivial (per AGENTS.md, native `Flickable.flick()` can detach the
sticky-bottom state machine). Worth scoping if user feedback asks for
it.

## Deferred review findings (PR #5)

Lower-severity items from the PR #5 review that were out of scope for
that PR. Apply opportunistically when touching the surrounding code.

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
- **Defer Piper remote voice-catalog refresh** until after QML first
  paint — cached/installed/configured first, network fetch
  asynchronous (overlaps with the P1 above).

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

Lower-priority code-organization improvements that would have been
nice to bundle into PR #5 but aren't load-bearing.

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
