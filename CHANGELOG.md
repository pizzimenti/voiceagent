# Changelog

All notable changes to VoiceAgent are documented here. Dates in YYYY-MM-DD.

## 0.8.1 — 2026-04-28

**QA automation infrastructure.** Adds a headless QML/UI verification
suite that closes the gap left by the v0.8.0 fast-path subagent
workflow (where visual / interaction sanity was deferred to a manual
post-merge smoke test). Patch bump: pure test tooling, no user-facing
capability change. Per the project's version-bump policy, capability
additions are minor; this is purely test infrastructure.

The suite uncovered one real bug in the v0.8.0 release that the
manual smoke test had been missing — see "Fixed" below.

### Added

- **`tests/qml/`** — Qt Quick Test (qmltestrunner) suite. Three
  `tst_*.qml` files covering form-layout shape, page-header
  actions, and the inertial wheel-scroll branching:
  - `tst_session_setup_pane.qml` — verifies `SessionSetupPane.qml`
    is a `Kirigami.FormLayout` (not the pre-PR-#22 GridLayout),
    that each row carries the expected `Kirigami.FormData.label`,
    and that `wideMode` flips correctly when `compactMode`
    toggles.
  - `tst_main_window.qml` — structural sanity: page-header
    actions exist and the verbose-log toggle is reachable.
  - `tst_scroll_mode.qml` — locks in PR #23's two-mode scroll
    branching (direct `contentY` assignment when `stickToBottom`,
    `flick(0, velocity)` when detached) against a mock
    listView.
- **`tests/qml/qmltest_main.py`** — Python driver that calls
  `QtQuickTest.QUICK_TEST_MAIN_WITH_SETUP` with a setup object
  that registers the `i18nCtx` translator on the test engine
  before any `tst_*.qml` parses, mirroring the production wiring
  in `MainWindow.__init__`. Without this, every `tst_*.qml`
  loading a production QML file would fail with
  `ReferenceError: i18nCtx is not defined`.
- **`tests/qml/StubVoiceAgent.qml`** — minimal QML stub of
  the `voiceAgent` surface for components that bind to it. Drift
  between this stub and the real `MainWindow` class is caught by
  `voiceagent-compiletest.sh` (which runs the *real* MainWindow
  against the *real* engine), so the stub stays small and
  test-scope-only.
- **`tests/test_replay_toast.py`** — pytest verification of PR
  #24's QML-side replay-failure toast wiring. Connects the
  Python `replay_failed` emit through the real
  `Kirigami.ApplicationWindow` to the passive-notifications
  overlay model and reads the message back via
  `QAbstractItemModel.data(index, role)`. This is the test that
  caught the broken `Connections { onReplayFailed }` wiring (see
  "Fixed" below).
- **`voiceagent-qatest.sh`** — single-command headless QA gate.
  Runs `pytest tests/` then `tests/qml/qmltest_main.py` with the
  same QML-import-path setup as `voiceagent-compiletest.sh`.
  Companion to compiletest:
    - `voiceagent-compiletest.sh` = "does the QML load?" gate
      (qmllint + real-engine load via fakes).
    - `voiceagent-qatest.sh` = "does the QML behave correctly?"
      gate (Quick Tests + pytest-qt interaction tests).
- **QML-import-path fallback in `tests/conftest.py`.** The
  shell scripts already prepend `/usr/lib/qt6/qml`; conftest now
  mirrors that for direct `pytest` invocations so tests that
  load real QML (e.g., `test_replay_toast`) work without first
  sourcing one of the gate scripts.

### Fixed

- **Replay-failure toast wiring (PR #24).** The QML side used a
  `Connections { target: voiceAgent; function onReplayFailed(...) }`
  block. Qt's QML signal-handler resolution does *not*
  auto-camelCase a snake_case Python signal name (`replay_failed`
  in `window.py`) at runtime, so `onReplayFailed` silently never
  fired and the toast never surfaced — `qmllint` warned at parse
  time ("no signal of the target matches the name"), but the
  warning did not fail compiletest. Switched to the
  `Component.onCompleted: voiceAgent.replay_failed.connect(...)`
  form, which binds at runtime regardless of casing convention.
  The new `tests/test_replay_toast.py` locks the wiring in.

## 0.8.0 — 2026-04-28

**Capability sweep.** Eight PRs land together: SHA-pinned download
verification (closes the layer 2/3 TOCTOU window), MainWindow internals
reorganized via `ConversationTurnCoordinator` and three new QML
components, KDE polish (FormLayout / i18n / Kirigami.Action), inertial
wheel-scroll mode-switch, and replay-failure toast. Minor bump per the
version policy: multiple capability additions, no public-API break.

### SHA-pinned download verification (PR #20)

`_download_voice` and `artifact_manifest` both used to resolve URLs
against `main` of `rhasspy/piper-voices`, so an upstream republish in
the few-second window between aria2 fetching the bytes and the manifest
refresh would make the verifier fail-close on a healthy install. Layer
4 (smoke-load) caught real corruption so the user could retry, but the
false-positive was visible. Now, every install captures the upstream
commit SHA at download start and pins both the file fetches AND the
verifier's manifest fetch to that same revision. Bytes-perfect manifest
comparison; no more self-healing false-positives from upstream churn.
The SHA is plumbed via a per-instance `_current_download_sha` field
that `_download_voice` sets and clears.

### Added

- **`PiperTtsService._capture_repo_sha()`** calls
  `HfApi.repo_info(VOICE_REPOSITORY).sha` to resolve the current
  commit of `rhasspy/piper-voices` at download start. Fails-closed:
  any error (network blip, HF 5xx, malformed response, missing
  `sha` attribute) raises so the download never proceeds against an
  unpinned `main` — the whole point of pinning is consistency. The
  existing download-error UI surfaces the raise to the user the
  same as any network failure.
- **`PiperTtsService._voices_json_url_for_sha(sha)`** classmethod
  constructs the SHA-pinned manifest URL.
- **`PiperTtsService._fetch_voices_json_at_sha(sha)`** classmethod
  fetches `voices.json` at a specific upstream commit, returns the
  parsed dict on success, `None` on any failure. Does NOT write to
  the on-disk `voices.json` cache — that cache is anchored to
  `main` for the eager catalog path, and writing a SHA-pinned
  snapshot over it could rewind the user's view of the catalog.
- **`PiperTtsService._current_download_sha`** per-instance field
  set by `_download_voice` before any URL is constructed,
  consumed by `artifact_manifest` during the post-download
  verification pass, and cleared on download failure.

### Changed

- **`_download_voice` threads SHA into `hf_hub_url`.** Both the
  `.onnx` and `.onnx.json` URLs now resolve to
  `https://huggingface.co/rhasspy/piper-voices/resolve/<sha>/...`
  via `hf_hub_url(..., revision=sha)`.
- **`artifact_manifest` reads from the pinned SHA.** When
  `_current_download_sha` is set, the verifier fetches the manifest
  at that revision via `_fetch_voices_json_at_sha`. When unset
  (defensive callers / tests outside an active download), the
  method skips layers 2/3 with a warning instead of falling back
  to `main` — falling back would re-open the TOCTOU window pinning
  is meant to close. Layers 1 (sidecar) and 4 (smoke-load) still
  apply.
- **The catalog-refresh path is unchanged.** `VOICES_JSON_URL` and
  `_fetch_and_cache_voice_names` continue to resolve against
  `main` because the eager catalog wants the latest browsable list
  of voices, not a download-pinned snapshot.

### SHA-pinning tests

- 11 new tests in `tests/test_tts_sha_pinning.py` covering SHA
  capture, URL construction, `_download_voice` threading, error
  fall-through (no SHA → empty manifest, capture failure → raise,
  download failure → SHA cleared), and the end-to-end
  download/manifest agreement.
- `tests/test_artifact_manifest.py` Piper tests updated to stub
  `_fetch_voices_json_at_sha` and set `_current_download_sha` on
  the fixture (replaces the previous `_fetch_and_cache_voice_names`
  monkeypatching). One new test asserts the no-SHA branch returns
  empty without falling back to `main`.

### Internal architecture (PRs #16, #17, #18, #19, #21)

- **#17 — Eliminate compiletest `StubVoiceAgent` heredoc.**
  `voiceagent-compiletest.sh` now loads a real `MainWindow` against a
  real `QQmlApplicationEngine`. Drift between the stub and the actual
  property/slot surface becomes structurally impossible. New
  `tests/fakes.py` extracts the test fakes for shared use.
- **#16 — `schedule_after_first_frame` deferral helper.**
  `MainWindow.show()`'s LLM autoconnect and TTS catalog refresh now
  defer through `QQuickWindow.frameSwapped` (one-shot, self-
  disconnecting) instead of `QTimer.singleShot(0, ...)`. Parallels the
  daemon-thread sounddevice pre-warm in `app.py:142`.
- **#18 — `required property var voiceAgent`.** `MicButton.qml` and
  `ConversationPane.qml` declare `voiceAgent` required. All internal
  `voiceAgent.*` bindings use the null-safe ternary pattern
  (`component.voiceAgent ? component.voiceAgent.foo : fallback`) to
  tolerate nested-component instantiation order. Closes the first-paint
  TypeError class that PR #5 hit and reverted.
- **#19 — `ConversationTurnCoordinator(QObject)`.** New module owns
  the per-turn flag, pending status-row queue, dedupe state, and
  verbose-mode gate. MainWindow forwards events to it via thin slot
  bodies; coordinator is the sole writer to `ConversationModel`. ~140
  lines moved out of `window.py`. Fold-in for the
  `ConversationLogController` design from the v0.4.0 round-2 review.
- **#21 — QML component extraction.** Three new components:
  `CatalogList.qml` (replaces duplicated STT/TTS catalog ListView
  delegates), `MicButtonFrame.qml` (unified across compact/medium
  modes via `animatePulse: bool`), `SessionSetupPane.qml`.
  `MainWindow.qml` shrinks 810 → 362 lines (-448).

### KDE polish + UX (PRs #22, #23, #24)

- **#22 — KDE polish bundle.** `SessionSetupPane.qml` migrated from
  `GridLayout` to `Kirigami.FormLayout` (`wideMode: !compactMode`
  drives compact-mode collapse). User-facing strings wrapped in
  `i18nCtx.i18n(...)` via a tiny `TranslatorContext` Python shim
  (`src/voiceagent/i18n.py`) — swappable for `KLocalizedContext` once
  PyKF6 is available. New verbose-log `Kirigami.Action` lifts to the
  page header.
- **#23 — Inertial wheel-scroll mode-switch.** `scrollList()` branches
  on `listView.stickToBottom`: at-bottom uses direct `contentY`
  assignment (preserves sticky-bottom state machine); scrolled-up
  uses `listView.flick(0, velocity)` for momentum scrolling. Velocity
  computed at `pixelDelta * 40` or `(angleDelta / 120) * gridUnit *
  60`. Auto-restores sticky-bottom via the existing `onMovementEnded`
  in `ConversationPane.qml`.
- **#24 — Replay failure toast.** New `replay_failed = Signal(str)`
  on `MainWindow` fires when `replayMessage` hits a not-available or
  synthesis-exception path. QML `Connections` invokes
  `applicationWindow().showPassiveNotification(reason, "short")` for
  a transient ~4s toast. The static readiness reason routes through
  `i18nCtx.i18n(...)`; the dynamic exception payload stays
  untranslated.

### Tests

- 217 passing total (was 182 at v0.7.0). New suites:
  `tests/test_tts_sha_pinning.py` (11), `tests/test_startup_deferral.py`
  (6), `tests/test_conversation_turn_coordinator.py` (21), plus
  additions to `test_artifact_manifest.py`, `test_mainwindow_integration.py`,
  and the existing TTS / catalog suites.

## 0.7.0 — 2026-04-27

**Download verification capability completion.** Closes the layers 2
and 3 gaps that have been parked since the v0.3.2 verifier shipped
with only layers 1 (aria2 sidecar) and 4 (Piper smoke-load). Both
backends now publish a per-file manifest with authoritative size +
checksum; the base verifier checks both before declaring an install
healthy.

Minor bump per the project's version policy: capability addition
visible to backends and observable via a new failure mode (a
size/checksum mismatch now routes through `load_failed` + cleanup).
No public-API break.

### Added

- **`ParallelItemLoader._verify_download` runs layers 2 + 3.**
  - Layer 2 — file size vs manifest. Fail-closed on mismatch.
  - Layer 3 — checksum vs manifest. Streams the file in 1 MiB chunks
    through `hashlib.new(algorithm)` (md5 or sha256). Fail-closed on
    mismatch; unsupported algorithm names skip layer 3 (treated as
    a backend-config bug, not corruption).
  - Generic helpers: `_verify_size(path, entry, name)` and
    `_verify_checksum(path, entry, name)` are public-on-the-class
    static methods so subclasses can compose differently if they
    need to.
  - New module-level dataclass
    `voiceagent.parallel_item_loader.ArtifactManifestEntry` with
    `expected_size`, `expected_checksum_hex`, `checksum_algorithm`
    fields. All optional — partial entries (e.g. HF non-LFS files
    that carry size but no sha256) skip the layer they don't cover
    instead of failing closed.
- **`PiperTtsService.artifact_manifest(name)`** reads the on-disk
  `voices.json` cache and returns size + md5 per file (basename →
  local path mapping). Empty dict on missing/malformed cache.
- **`WhisperTranscriber.artifact_manifest(name)`** calls
  `HfApi.repo_info(repo_id, files_metadata=True)` and returns size
  for every file plus sha256 for LFS files. Reads `HF_TOKEN` from
  the environment for parity with `_prepare_model_source`. Empty
  dict on network failure or unmanaged custom path.

### Changed

- **Manifest fetch is fail-soft.** A backend whose
  `artifact_manifest` raises (e.g. HF API timeout mid-install) gets
  a warning log and the verifier degrades to layer 1 only. The user
  just successfully downloaded gigabytes; aborting the install on a
  metadata blip would be terrible UX. Layer 1 already passed.
- **Backends without `artifact_manifest` degrade gracefully.** The
  base verifier reads the method via `getattr` so the `_ItemBackend`
  protocol stays unchanged and third-party / test fakes continue to
  work without modification.

### Tests

- 19 new tests (158 passing total, was 139):
  - 9 in `tests/test_parallel_item_loader.py` covering the base
    verifier extension: passing case (size + checksum match), size
    mismatch fail, checksum mismatch fail, sha256 path mirrors md5
    path, partial entries skip only the missing layer, no manifest
    method skips both, manifest-getter raise is fail-soft, paths
    not in manifest are silently skipped, unsupported algorithm
    names don't fail closed.
  - 10 in `tests/test_artifact_manifest.py` covering per-backend
    construction: Piper basename mapping, missing cache, missing
    voice in cache, malformed JSON, partial metadata fields;
    Whisper LFS-vs-blob distinction, nested-path skip parity with
    `_prepare_model_source`, network-failure fail-soft, custom-path
    short-circuit, `HF_TOKEN` propagation.

## 0.6.4 — 2026-04-27

**Cleanup-path correctness fix.** Single bug from the PR #11 review
queue (P2, two rounds of evidence). No new surface, no behavior change
on the happy path.

### Fixed

- **`ParallelItemLoader._cleanup_failed_download` no longer rmdirs the
  shared model root** when its basename happens to match the item name
  being installed. The 0.6.3 guard (`parent.name == name`) covered the
  default flat-vs-nested layout split but failed when
  `VOICEAGENT_TTS_MODEL_ROOT` / `VOICEAGENT_STT_MODEL_ROOT` was set to
  a path whose basename collided with an item name (e.g.
  `/srv/voices/en_US-ryan-high/` for item `en_US-ryan-high`). A failed
  verification then deleted the entire root and the next
  `voices.json` refresh failed inside `tempfile.mkstemp(dir=model_root)`
  until a human recreated the directory by hand. The new guard also
  requires `parent != backend.model_root`, read defensively via
  `getattr` so the `_ItemBackend` protocol stays unchanged.

### Tests

- Three new regression tests in `tests/test_parallel_item_loader.py`:
  the bug case (shared root with matching basename survives), the
  Whisper-style nested layout (per-item subdir is still cleaned up),
  and the Piper-style flat layout (no basename collision still
  preserved). `pytest -q`: 139 passing (was 136).

## 0.6.3 — 2026-04-26

**Infra hardening release.** Seven deferred items drained from the
PR #5 / #6 / #7 round-2 review queues. Test-side cleanup, dev-dep
plumbing, compiletest hardening, and one small loader-side
robustness improvement. No user-visible behavior change.

### Added
- **`TtsVoiceLoader.catalog_refresh_settled` signal** fires once per
  refresh cycle regardless of outcome (delta, no-change, exception).
  Replaces the broken `_catalog_refresh_scheduled` polling pattern
  in tests; UI code typically wants `catalog_changed` instead — the
  delta-only emit is unchanged.
- **`ruff` dev extra** in `pyproject.toml` plus a minimal
  `[tool.ruff]` block (target py311, line-length 100, exclude
  `src/vendor`). CodeRabbit references Ruff rule codes in PR reviews;
  contributors can now enforce locally via
  `pip install -e .[dev] && ruff check .`.

### Changed
- **`ParallelItemLoader.shutdown(timeout=2.0)`** now does a bounded
  join on in-flight workers via per-instance future tracking + a
  `concurrent.futures.wait(timeout=...)` after
  `executor.shutdown(wait=False, cancel_futures=True)`. Workers
  that complete within the timeout are awaited cleanly; over-runs
  are left to run in the background, relying on Qt's queued-
  connection safety net. Tracking is wired into both
  `download_item` / `delete_item` and
  `tts_loader.refresh_catalog_async` so a refresh mid-flight at
  shutdown gets a chance to settle.
- **Compiletest now lints components standalone.**
  `voiceagent-compiletest.sh` runs `qmllint` on `MicButton.qml` and
  `ConversationPane.qml` directly (in addition to `MainWindow.qml`).
  Catches errors local to the components without depending on the
  in-script `StubVoiceAgent`.
- **`tests/conftest.py` pins `qapp_cls=QApplication`** and
  materializes the `qapp` fixture at session start via an
  `autouse` fixture. Removes the recurring `RuntimeWarning:
  Existing QApplication ... is not an instance of qapp_cls` noise.
- **`tests/test_parallel_item_loader.py`** now uses a `make_loader`
  pytest fixture for unconditional shutdown — an assertion failure
  mid-test no longer leaks the executor into the next test.
- **`tests/test_tts_catalog_first_paint.py`** hoists the duplicated
  `_FakeResponse` class (3x) and `_boom` (2x) into module-level
  helpers. Third `_boom` re-definition is intentionally kept inline
  because that test additionally tracks call attempts.

## 0.6.2 — 2026-04-26

**Loader hardening release.** Five backend-only fixes drained from
the PR #5 (CodeRabbit) and PR #6 review queues. Defensive,
structural, and a perf flag — no user-visible behavior changes.

### Changed
- **Failed-install cleanup rmdirs per-item nested directories.**
  `parallel_item_loader._cleanup_failed_download` now best-effort
  removes the empty per-item subdirectory after artifact unlinks
  for nested-layout backends (Whisper's
  `<model_root>/<item_name>/`). Gated on `parent.name == name` so
  flat-layout backends (Piper, where artifacts share `model_root`
  directly) cannot have their shared root removed — that would
  break the next `voices.json` refresh.
- **Subclass status hooks enforced at class-build time.**
  `ParallelItemLoader.__init_subclass__` walks every subclass's
  MRO at definition time and refuses to construct a class that
  hasn't overridden every required `_status_*` hook. Surfaces
  forgotten overrides as a clean `TypeError` at import rather
  than as a lazy `NotImplementedError` whenever the state machine
  reaches that specific transition. `@abstractmethod` would be
  canonical but `QObject`'s metaclass conflicts with `ABCMeta`.
- **`_verify_download` docstring** clarifies up front that this
  is a *post-download one-shot check*, not a generic readiness
  probe. Subclass overrides may run heavy work (Piper's
  `onnxruntime.InferenceSession` is ~30-50 ms) and must not be
  invoked from per-row availability paths like
  `is_item_available` or `_CatalogStateAdapter.is_installed`.
- **Piper smoke-load skips graph optimization.** `tts_loader`
  passes `SessionOptions(graph_optimization_level=ORT_DISABLE_ALL)`
  to the verification `InferenceSession`. Verification only
  needs protobuf parse + graph build; the optimizer pass adds
  ~20-50 ms per voice install with no signal.
- **`single_instance` per-connection state moves to closures.**
  Replaces the `id(connection)`-keyed `self._buffers` dict with
  closure-local `bytearray` + `finalized` bool. Lifetimes match
  the connection's exactly; eliminates the (theoretical) id-reuse
  hazard and the manual cleanup paths.

## 0.6.1 — 2026-04-26

**Review-cleanup release.** Drains five small deferred fixes from the
PR #5 / #6 / #7 round-2 / #8 review queues; no new capabilities, no
user-visible behavior changes except the Connect button now stays
disabled during an in-flight refresh.

### Changed
- **Atomic `voices.json` write.** `_fetch_and_cache_voice_names`
  writes to a per-call unique tempfile in `model_root` then
  `os.replace`s onto the cache. A process kill mid-write now leaves
  the previous (valid) cache intact instead of a truncated file
  that the JSON parser silently treated as "empty cache". The
  try/except cleans up partial tempfiles.
- **Read-only role mapping.** `roleNames()` returns a fresh `dict`
  copy in both `CatalogModel` and `ConversationModel`. Qt can mutate
  the copy but not the canonical class-level dict. (PySide6 strictly
  type-checks the return — `MappingProxyType` triggered a
  `RuntimeWarning` and Qt fell back to an empty role map.)
- **`update_message` O(1) role lookup.** `ConversationModel` now
  resolves per-key role via a class-build-time `_KEY_TO_ROLE`
  inverse map, dropping the per-update O(R) linear scan over
  `_ROLE_KEYS`.
- **Connect button spam-click guard.** The QML `enabled:` clause
  now always requires `!llmConnectionBusy`. The previous
  `(!llmServerConnected || !llmConnectionBusy)` let repeat clicks
  through during the initial connect path while a refresh was
  already in flight, queueing extra `_start_refresh()` calls.
- **`PiperTtsService.known_voice_names` caching.** The union of
  on-disk voices + `voices.json` cache is memoized per-instance
  with a `threading.Lock` so worker-thread invalidations from
  `refresh_catalog` are serialized against GUI-thread reads. The
  cache invalidates on `refresh_catalog`, successful `_download_voice`,
  and `remove_item`. Eliminates the per-row disk-glob /
  JSON-parse cost when QML reads `model.managed` /
  `model.downloadable` for every visible row.

## 0.6.0 — 2026-04-26

**Custom STT path support in CatalogModel.** A `WHISPER_MODEL=/path`
entry now appears as a `Custom path` row in the Model Manager
alongside the managed Whisper models. The role infrastructure landed
in v0.5.0 was designed for exactly this — `managed=False`,
`downloadable=False`, `installed` reflects whether the path resolves
to a faster-whisper-shaped layout on disk.

### Added
- **`WhisperTranscriber._custom_path`** tracks a path-shaped
  selection (set in `__init__` and `set_model_name` when the value
  is not in `MODEL_REPOSITORIES` and reads as a path — absolute,
  contains `/`, or starts with `~`). `available_items()` appends
  the custom path so it surfaces in the catalog.
- **QML `modelStatusSummary` returns `"Custom path"`** for
  `installed && !managed` rows; new `modelActionVisible(item)`
  gate hides the Install / Remove button on unmanaged installed
  rows (Voice Agent does not own that file's lifecycle) and on
  un-installed rows that aren't downloadable.

### Changed
- **`MainWindow._handle_inventory_change` now refreshes the STT
  catalog model** when `transcriber.available_items()` shape
  shifts — same hook the TTS catalog already used for its deferred
  remote refresh. Custom rows appear / disappear without an app
  restart. `selection_changed` is routed through this handler too.
- **`PiperTtsService.is_item_downloadable`** also accepts
  voice-name-shaped strings via `_looks_like_voice_name`. A
  configured `TTS_MODEL=en_US-lessac-medium` keeps its Install
  button on first run before the deferred `voices.json` fetch
  populates the cache.
- **Persist only managed STT selections to QSettings.** A persisted
  custom path would resolve to a fallback on next launch when the
  env var is unset (the path no longer in the catalog), making
  selection state silently drift.

## 0.5.0 — 2026-04-26

**CatalogModel role extension.** The Model Manager's per-row state
(installed flag, in-flight loading flag, download progress) now flows
through proper Qt roles on the catalog model instead of through
parallel `QVariantMap` / `QVariantList` snapshots rebuilt on every
event. Same UX, lighter signal traffic on the hot path during model
installs, and the foundation for the deferred custom-STT-path
catalog work.

### Added
- **`loading`, `progress`, `downloadable`, `managed` roles on
  `CatalogModel`** alongside `name` and `installed`. QML delegates
  bind directly to `model.loading` / `model.progress`; the previous
  `voiceAgent.sttProgressMap[model.name]` lookups are gone.
- **`CatalogStateProvider` Protocol** decouples `CatalogModel` from
  loaders and backends. The window-side `_CatalogStateAdapter`
  pulls live state from the loader + service so `MainWindow` no
  longer carries duplicate `_active_items` / per-item progress
  dicts.
- **`refresh_row(name)` and `refresh_progress(name)`** on
  `CatalogModel`. The narrow-role variant emits `dataChanged` with
  only `[ProgressRole]`, keeping `installed`/`loading`-driven
  sibling bindings asleep between sub-second aria2 ticks.
- **`is_item_managed` and `is_item_downloadable`** on
  `WhisperTranscriber` and `PiperTtsService`. `PiperTtsService`
  also gains a new `known_voice_names()` classmethod that
  consolidates installed + cached unions as a single source of
  truth.
- **`sttInstalledCount` / `ttsInstalledCount` Q_PROPERTYs** on
  `MainWindow`. Replaces the QML-side `countInstalled()` JS helper
  that iterated `sttCatalog`/`ttsCatalog` on every `ui_changed`.

### Changed
- **`ParallelItemLoader._finish_success` no longer emits a synthetic
  terminal `item_progress_changed`.** That frame existed only to
  keep the dropped `*ProgressMap` "busy" predicate honest; the
  `refresh_row` triggered by `item_loading_changed(name, False)`
  now drives the row back to idle through the adapter.

### Removed
- **`MainWindow.downloads_changed` / `downloads_progress_changed`
  signals** and the matching `sttCatalog`, `ttsCatalog`,
  `sttProgressMap`, `ttsProgressMap`, `sttDownloadingList`,
  `ttsDownloadingList` properties. Per-row state now flows through
  `CatalogModel`'s roles. The `*DownloadingList` properties had no
  remaining QML consumers.

## 0.4.0 — 2026-04-26

**UI shaping release.** Toolbar tightens up with symbolic icons, the
conversation log gains an opt-in verbose mode that surfaces pipeline
activity inline, and the window now caps below the previous large
two-column dashboard width. First-click mic latency drops because
`sounddevice` is pre-warmed at startup instead of being lazy-imported
on the click path.

### Added
- **Verbose conversation log.** New eye-icon toggle in the
  Conversation pane header switches the log between simple (final
  user/assistant turns only) and verbose (also shows pipeline status:
  Transcribing, Thinking, Generating voice, Speaking). Verbose
  entries render as plain italic purple text inline with the
  transcript — no bubble — so they read as background activity
  rather than conversation. Persists across sessions via the
  `log_verbose_mode` QSettings key. Filter is applied at capture, so
  toggling on records only forward; existing entries remain. RECORDING
  is intentionally excluded from the log because the draft user
  bubble already signals it.
- **`StateNameRole` on `ConversationModel`** — exposes the raw
  `AppState.value` for `role="status"` rows so QML can style or
  filter on the underlying state without parsing the label text. The
  new `"status"` role is reserved for verbose-mode pipeline activity;
  existing `"system"` rows for model/voice loading and operational
  errors are unaffected.
- **`VOICEAGENT_VERBOSE_UI` environment variable** — when set
  (`1`/`true`/`yes`/`on`), bumps the rotating file log to DEBUG so
  monotonic-timing entries from the click chain land in
  `voiceagent.log`. Format is `ui-timing label=<name> ms=<float>`.
  Useful for diagnosing UI lag without spamming the console (the
  stream handler stays at WARNING regardless).

### Changed
- **Toolbar uses symbolic icons.** Voice Models action uses
  `folder-cloud-symbolic`; the theme menu loses its visible text
  label and renders icon-only via `Kirigami.DisplayHint.IconOnly`
  with `preferences-desktop-theme-symbolic`; mute toggles between
  `audio-volume-muted-symbolic` and `audio-volume-high-symbolic`.
  Symbolic icons pick up the palette so the toolbar reads cleanly
  in both Breeze Light and Dark. Auto/Light/Dark sub-actions stay
  text-and-checkable as before.
- **Window caps at `Kirigami.Units.gridUnit * 49` and the maximize
  button is disabled.** Voice Agent is a small-window app; the
  large two-column dashboard layout (`largeMode`,
  `dashboardColumns`, `largeMicPaneComponent`, `largeDashboardRow`,
  `largeMicPriorityMode`) has been removed entirely. Two responsive
  views remain: medium (stacked, up to the cap) and compact (under
  250 px). The session-setup grid simplifies from a three-way
  column count to a binary one. `MainWindow.qml` shrank from 882 to
  778 lines.

### Fixed
- **First mic-button click no longer blocks on `sounddevice`
  import.** `MicrophoneRecorder.start()` lazy-imported `sounddevice`
  on first call, loading the PortAudio C library on the main Qt
  thread between the click and the next paint (100-500 ms). The
  import is now pre-warmed after `window.show()` on a daemon thread,
  so the GUI keeps painting while PortAudio loads.
- **Simple log mode actually hides pipeline activity.**
  `_set_status_message` previously appended every controller status
  transition to the conversation log as a `role="system"` row,
  bypassing the new `logVerboseMode` gate added by `_apply_state`.
  Simple mode looked identical to verbose, and verbose mode rendered
  duplicate rows for the same state. The status text still drives
  the mic-button label; the conversation-log status path now flows
  exclusively through `_apply_state`'s `role="status"` write, gated
  by `logVerboseMode`.
- **`replayMessage` no longer raises into QML.** The replay slot
  now uses `tts_service.is_available` (which requires both `.onnx`
  and `.onnx.json` for the selected voice, matching
  `is_item_available`) instead of `tts_service.enabled` (which
  passes for half-installed voices), and wraps the synthesize call
  in `try/except` that surfaces failures via the existing error
  channel.

### Documentation
- `AGENTS.md` updated. Review-guideline line that listed three
  responsive layouts now lists two (the large layout was removed).
  The "no statuses in bubbles" rule is softened to acknowledge that
  verbose log mode surfaces pipeline statuses as plain styled text
  in the transcript; bubbles remain reserved for final user and
  assistant content.

## 0.3.3 — 2026-04-26

**Local-repo packaging helper.** No app changes. New script that
publishes VoiceAgent into a personal pacman repo so AUR helpers
stop probing the AUR for a package that lives only on disk.

### Added
- **`packaging/release-local.sh`** — runs `makepkg -f`, moves the
  resulting `.pkg.tar.zst` into `~/.local/share/pacman-localrepo/`
  (override via `REPO_DIR` / `REPO_NAME`), and updates the local
  repo database via `repo-add -R`. Once VoiceAgent is installed via
  `pacman -S` from that repo instead of `pacman -U` against a loose
  artifact, it stops appearing in `pacman -Qm`, and `yay` / `paru`
  no longer include it in the per-startup AUR `arg[]=...` info
  request.

### Notes
- The script intentionally does **not** pass `--cleanbuild` to
  `makepkg`. In this repo `${srcdir}` resolves to `${startdir}/src`,
  which is the actual source tree; `--cleanbuild` would `rm -rf`
  it before `build()` runs and yield an empty wheel. The trap is
  documented in a comment in the script.

## 0.3.2 — 2026-04-25

**Concurrency and download-integrity release.** No new user-facing
features. Five real bugs that could bite the running app, plus the
verification layer that catches the corrupt-download mode that hit
v0.3.1's build test.

### Fixed
- **First paint no longer blocks on the network.** The Piper
  `voices.json` fetch (5 s timeout when the local cache is missing)
  used to run synchronously in `MainWindow.__init__`. On a cold-cache
  + offline launch the QML window wouldn't paint until that timeout
  resolved. The catalog now starts with the union of installed +
  configured + cached voices, lets QML paint, and refreshes
  asynchronously after first paint via `QTimer.singleShot(0, …)`. A
  catalog-changed signal makes the dropdown rebind when the network
  result lands. Refresh failure (HTTP 503, offline, malformed
  response) is non-fatal and logged.
- **VoiceController state mutations from worker callbacks now route
  through queued signals** so only the owner thread writes
  `_active_pipeline_count` and `_partial_inflight`. Same class of
  bug `ParallelItemLoader` fixed in v0.3.1, but in a different file.
  AutoConnection (default) is used so same-thread emits run inline —
  important because `Future.add_done_callback` invokes its callback
  synchronously on the caller's thread when the future is already
  done at registration time, and an explicit `QueuedConnection` would
  defer the count decrement until *after* the result-handler had
  already read a stale count and skipped the resume/state-transition
  logic.
- **AudioPlayer is safe under back-to-back `play_file` calls.** Each
  invocation now mints a fresh generation ID and per-worker
  `threading.Event`. Stale workers exit cleanly, can't write into
  the live playback's audio device, can't emit `playback_finished`
  for the wrong path, and unlink only their own temp WAV (a stale
  worker that didn't unlink its own file accumulated orphans on
  rapid replay/supersede). Bounded 0.25 s join replaces the prior
  1 s wait so the calling thread doesn't stall on a stuck worker.
- **LlmController's `connection_busy` flag clears only on the live
  refresh.** Previously the busy-clear ran *before* the
  stale-discard check, so an earlier completion could clear busy
  while a newer refresh was still in flight; the newer completion's
  clear was a no-op. Symptom: a brief "connected, idle" UI flash
  during rapid Connect-then-switch-URL sequences.
- **Downloads are verified before the loader marks them ready.** A
  partial Piper voice download in v0.3.1's build test left an
  `.onnx` plus a `.aria2` aria2 control sidecar on disk; the loader
  emitted `load_completed`, the user selected the voice, and first
  TTS attempt crashed with `onnxruntime InvalidProtobuf` →
  `wave.Error: # channels not specified`. New `_verify_download(name)`
  hook on `ParallelItemLoader` runs after `download_item` returns,
  before `load_completed`. Base layer 1 walks
  `backend.artifact_paths(name)` and rejects any artifact with a
  surviving `.aria2` sidecar; verification failure deletes the
  partial files and emits `load_failed` instead. Piper backend
  overrides with a layer-4 smoke-load
  (`onnxruntime.InferenceSession`) that catches the corrupt-bytes
  case the sidecar check misses. Layer 4 fails closed if the
  `onnxruntime` import itself is broken. Whisper backend uses base
  layer 1 only (smoke-load is too slow for it). Whisper's
  `artifact_paths` includes vocabulary candidates so partial vocab
  downloads are also caught.
- **`PiperTtsService.is_available` matches `is_item_available`** —
  it now requires both `.onnx` and `.onnx.json` to exist for the
  selected voice. Previously `is_available` reported True with only
  `.onnx`, contradicting the predicate `is_item_available` used at
  every other callsite.
- **TTS catalog refresh recovers from transient failures.** The
  `_catalog_refresh_scheduled` flag now resets on worker resolution
  (success, exception, or no-op delta), so a one-time HTTP 503 on
  first paint no longer locks out future re-fetches for the rest of
  the session. The flag is now strictly a do-not-stack guard.
- **`refresh_catalog_async` survives MainWindow shutdown.** A 0 ms
  `QTimer.singleShot` posted by `MainWindow.show()` could fire after
  `MainWindow.shutdown()` had torn down the loader executor, raising
  `RuntimeError` on the unguarded `executor.submit`. Now caught and
  logged at debug level.

### Documentation
- `FOLLOWUPS.md` curated to remove items that landed in this release;
  remaining deferrals from PR #6 review (CodeRabbit nitpicks: empty
  cleanup-subdir best-effort `rmdir`, `_verify_download` doc-clarity,
  `voices.json` atomic-rename guard, ONNX `SessionOptions` perf tweak,
  test-helper duplication, `_wait_for` test-flow improvement) are
  now tracked in the deferred-review section.

## 0.3.1 — 2026-04-25

**Internal cleanup release.** No new user-facing features. Substantial
restructuring under the hood, several real concurrency fixes that
parallel-install users could trigger, and the project now ships with a
test suite. Same install path, same UI.

### Fixed
- **Parallel-install thread safety.** STT and TTS download progress
  was mutated from worker threads while the UI thread read it
  concurrently — a real race that could corrupt the per-row progress
  map under multi-download load. Progress now routes through a
  queued-connection signal so only the owner thread writes the dict.
- **Idempotent download finalization.** A download that emitted
  `load_failed` from inside its worker *and* from the
  `add_done_callback` path could double-finalize and re-enter the row
  busy state. The finalization slots are now early-return no-ops if
  the item is no longer active.
- **`DownloadProgress` is immutable.** Promoted to
  `@dataclass(frozen=True, slots=True)`. Required so the value type
  can be safely shared across thread boundaries via the queued
  connection.
- **Catalog rows no longer stick on "Installing…" after a successful
  install.** The QML `downloading` predicate is now consistent with
  the loader's terminal-tick behavior (post-finalization ticks are
  guarded out, and `delete_item` now emits the symmetric initial
  empty-progress tick).
- **Delete failures route to `delete_failed`, not `load_failed`.**
  `_handle_done` is the done-callback for both download and delete
  futures; a delete-future raising no longer surfaces a misleading
  "load failed" message.
- **Single-instance activation requires `b"activate"` payload.** The
  Qt `QLocalServer` now reads the payload before emitting
  `activated`, instead of activating on any TCP-style connection.
  Bounded payload accumulation guards against unbounded reads.
- **Subprocess output pre-initialized.** `aria2c` invocation in
  `downloaders.py` initializes `stdout, stderr = "", ""` before the
  `try:` so an unexpected `proc.communicate()` failure can't shadow
  the real error with `UnboundLocalError`.

### Changed
- **`MainWindow` is split.** `ConversationModel` (`conversation_model.py`),
  `CatalogModel` (`catalog_model.py`), and the LLM connect/refresh/
  load orchestration (`services/llm_controller.py`) now live in
  dedicated modules. `MainWindow` shrank from ~1156 lines to ~600 and
  delegates the LLM surface to a controller object. The QML-facing
  slot/property surface is unchanged.
- **STT and TTS loaders share a `ParallelItemLoader` base.** The
  duplicated state-machine code in `model_loader.py` and `tts_loader.py`
  is now in `parallel_item_loader.py`. Subclasses override only the
  status-string formatters and a couple of backend-specific hooks.
- **QML extracted into reusable components.** New `qml/MicButton.qml`
  replaces three near-identical Button blocks across the medium /
  compact / large layouts. New `qml/ConversationPane.qml` factors the
  inline `conversationPaneComponent` into its own file.
  `MainWindow.qml` shrank from 1187 → ~880 lines.
- **`micStatusLabel` is a priority table** instead of a 14-branch
  if/elif. Adding a new pipeline state means inserting one tuple at
  the right priority.
- **Local PKGBUILD and AUR PKGBUILD share one packaging policy.**
  Both vendor speech dependencies under `/usr/lib/voiceagent/vendor`
  and ship the launcher script. The launcher discovers the system
  site-packages path via `sysconfig.get_paths()["purelib"]` at run
  time, so a Python rollover (3.14 → 3.15) no longer breaks
  installed packages.
- **Catalog delegate downloading-set lookup is O(1)** via a
  `QVariantMap` instead of `Array.indexOf`.

### Added
- **Pytest infrastructure.** `pytest>=8` + `pytest-qt>=4`, headless Qt
  via `QT_QPA_PLATFORM=offscreen` in `tests/conftest.py`. 57 tests
  cover the format helpers, both list models, the
  `ParallelItemLoader` state machine (including idempotent
  finalization, queued-connection thread-safety, post-finalization
  tick handling, and delete-failure routing), and the
  `LlmController`.
- **`FOLLOWUPS.md`** — forward-only worklist of items deferred from
  this release's review and from future-feature scoping.

### Removed
- **`audio_check.py` and `replay_widgets.py`**, which were leftover
  Qt Widgets debug code unreferenced by any live module after the
  Kirigami/QML migration.
- **Stale `TODO.md`** (its single line referred to replacing the
  Widgets shell, which already happened in v0.2.0).

### Documentation
- **CHANGELOG correction**: v0.3.0 claimed "inertial scrolling" but
  the implementation is direct `contentY` assignment with
  bounds-checking. Reworded to describe what shipped.
- **README**: `Tests` section, AUR section reflects the new vendor
  policy, AUR release checklist updated.

## 0.3.0 — 2026-04-24

**Install multiple voices at once, watch each one's progress in the list, and
keep your scroll position while they finish.**

### Added
- **Parallel model installs.** Start up to three downloads simultaneously from
  the Voice Models dialog. Each row shows its own thin progress bar; only the
  currently-downloading row's Install button disables. Other rows stay clickable
  so you can queue more work without waiting.
- **Sticky-to-bottom conversation view.** If you're at the bottom when a new
  turn arrives, the view follows; if you've scrolled up to read history, the
  view stays put and a small "↓" button appears to jump back.
- **Smooth pixel-based wheel scrolling** in both the conversation and the
  Voice Models lists — wheel / trackpad input drives `contentY` directly
  with bounds-checking, replacing the prior page-jump behavior. (True
  inertial coast-and-decelerate remains future work.)
- **Microphone button shows live status** inside the button itself — "Ready —
  tap to talk", "Listening…", "Transcribing…", "Thinking…", etc. The status
  replaces the old separate indicator text.
- **Single-instance guard.** Launching VoiceAgent while it's already running
  raises the existing window instead of spawning a second process.
- **QML runtime logs routed to the Python log file** for easier debugging.

### Changed
- **Conversation model migrated to `QAbstractListModel`.** New turns append in
  place without rebuilding the full list, so the view no longer flashes or
  snaps to the top on every message.
- **Catalog models migrated to `QAbstractListModel`.** When an install/delete
  completes, only the affected row updates — the list doesn't scroll back to
  the top.
- **Catalog rows are non-interactive** except for the Install/Remove and
  Use/Current buttons. No more misleading hover highlight that looked like
  selection cycling during fast scroll.
- **Assistant pipeline status** (Thinking, Transcribing, Playing, etc.) moved
  to the microphone status label. Assistant bubbles only appear once the
  response is final, and they land chronologically below the in-flight status
  events.
- **Default LM Studio timeout** raised from 5 s to 10 s so short reasoning-model
  turns don't cut off.
- **Empty Whisper transcripts** (partial-transcription pass on silence) log at
  INFO instead of WARNING, killing the stdout flood during idle listening.

### Fixed
- **aria2c lifecycle.** `--enable-rpc` made aria2c a daemon that never exited
  on its own, so completed downloads spun the polling loop forever. Now we
  send `aria2.shutdown` RPC on completion, with a 5-second grace period and
  a terminate/kill cascade if the RPC silently fails.
- **aria2 port race under parallel installs.** The reserve-port-then-spawn
  sequence is now serialized with a `threading.Lock` held until aria2c has
  actually bound its RPC port, preventing two workers from being handed the
  same ephemeral port.
- **Stuck-loading regression.** After a download completed, the loader's
  single `_loading` bool could get stuck True, disabling every Install button.
  Replaced with a per-model `_active_items` set so completion of one download
  doesn't touch other rows' state.
- **Download progress storms** no longer invalidate the catalog list.
  Progress ticks emit on a dedicated signal that doesn't notify the model
  list, so the UI stays stable under a running download.
- **Conversation list flicker** during live partial transcription (cause was
  per-tick QVariantList replacement resetting `contentY`).
- **Launch freeze with LM Studio unreachable.** LLM refresh is deferred off
  first-paint via `QTimer.singleShot(0, ...)` and runs in a `ThreadPoolExecutor`
  so a blocking `urlopen` can't stall the UI thread.
- **PKGBUILD version drift.** The install-path version now matches
  `pyproject.toml` so `makepkg` produces a package with the right semver.
- **SingleInstance cleanup.** `SingleInstance.release()` is wired to
  `QApplication.aboutToQuit` so the lock file and QLocalServer are torn down
  deterministically on graceful shutdown.

### Known follow-ups

See `FOLLOWUPS.md` on the `review-followups` branch for deferred review
findings: thread-safety of per-model progress state, PKGBUILD / PKGBUILD.aur
drift, Python-version-hardcoded launcher, and a set of structural refactors
(loader deduplication, `MicButton.qml` extraction, god-object split).

## 0.2.0 — 2026-04-02

First Kirigami/QML release. Baseline STT (Whisper) + TTS (Piper) + LM Studio
pipeline, session-setup pane, per-turn conversation bubbles, model manager
dialog.
