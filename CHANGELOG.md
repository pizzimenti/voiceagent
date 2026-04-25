# Changelog

All notable changes to VoiceAgent are documented here. Dates in YYYY-MM-DD.

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
  import is now pre-warmed during `app.main()` so the splash period
  absorbs the cost.

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
