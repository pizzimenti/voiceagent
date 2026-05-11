# Changelog

All notable changes to VoiceAgent are documented here. Dates in YYYY-MM-DD.

## 0.12.1 — 2026-05-11

**Auto-migrate v0.11.x flat data dirs to the engine-scoped tree.**
v0.12.0 introduced the new layout (`stt/whisper/`, `tts/piper/`,
`tts/chatterbox/references/`) but the legacy aliases were redirected
to the new paths, leaving an upgrading user's existing models
orphaned at the old flat locations (`stt-models/`, `tts-models/`,
`chatterbox-references/`). The app appeared to lose all installed
content — STT/TTS catalogs went empty, the user re-downloaded
several GB of models that were already on disk a directory away.

`voiceagent.paths.migrate_legacy_data_dirs()` now runs at startup
(before service construction) and moves each legacy dir into its
engine-scoped equivalent when the new dir is empty. Idempotent
(no-op once migrated). Logs the migration to both the file log and
the console line on first post-upgrade launch.

Skip rules:

- **Both old and new dirs have content.** The migration logs a
  warning and skips that pair — merge manually if the old data
  should take precedence. Avoids clobbering anything the user
  accumulated post-upgrade.
- **`VOICEAGENT_*_ROOT` env var is set.** The user has an explicit
  custom path; auto-migrating to the *default* would put the data
  somewhere they're not reading from. Manual `mv` recovers prior
  state in that case.
- **`os.rename` fails (cross-device, permissions).** Logs a warning
  with the OSError; the app proceeds with empty new dirs. Manual
  `mv` recovers.

### Manual fallback

Users on custom env-var paths or with both-populated dirs can run
the migration by hand:

```sh
mv ~/.local/share/voiceagent/stt-models           ~/.local/share/voiceagent/stt/whisper
mv ~/.local/share/voiceagent/tts-models           ~/.local/share/voiceagent/tts/piper
mv ~/.local/share/voiceagent/chatterbox-references ~/.local/share/voiceagent/tts/chatterbox/references
```

Adjust the destination paths for any custom `VOICEAGENT_*_ROOT`
overrides.

## 0.12.0 — 2026-05-10

**Chatterbox TTS engine + live engine selector.** Voiceagent ships
Piper (default) and now also Chatterbox (Resemble AI's zero-shot
voice cloning model) as an optional second engine. Install via
`pip install voiceagent[chatterbox]`. Both engines coexist behind a
runtime engine selector in the main window — switch without
restarting; per-engine voice selection is remembered across swaps.

Chatterbox is voice-cloning only — there are no built-in voices.
Two ways to add one:

- **Mic-record** (`startChatterboxRecording`): in-app fixed-duration
  capture (up to 60 s) writes a 24 kHz mono WAV directly into the
  references catalog. The always-on STT mic is suspended for the
  duration so the recording stays clean.
- **File import** (`importChatterboxReference`): pick any audio
  file via the system FileDialog; non-WAV is resampled to 24 kHz
  mono on import. Percent-encoded paths and filenames with spaces
  / non-ASCII characters are decoded correctly.

Reference clips live at `$VOICEAGENT_CHATTERBOX_REFERENCES_ROOT`
(default `~/.local/share/voiceagent/tts/chatterbox/references/`).

**Per-dtype model variants.** A dropdown in the Chatterbox config
pane selects between four precision tiers — `q4` (default, ~700 MB,
fastest), `q4f16`, `fp16`, and `fp32` (~2.5 GB, highest fidelity,
preserves the high-frequency tail of speaker embeddings where pitch
lives). Switching dtypes does not delete previously-downloaded
variants — flipping back is free.

**Offline-readiness.** `download_engine_model` pins a single HF
revision SHA across every component fetch (graphs + sidecars +
tokenizer files), records the SHA + sidecar inventory in a per-
dtype install manifest, and routes every download through the
engine-scoped `model_root` rather than the global HF cache.
`AutoTokenizer.from_pretrained` uses the pinned revision with
`local_files_only=True` so first synth never touches the network
when the install is complete. `_model_present` re-verifies the
manifest's sidecar inventory still exists, so a post-install cache
eviction surfaces as "needs download" instead of failing later at
`InferenceSession`.

**Engine swap safety.** Live engine swaps tear down the previous
loader's signal connections before tearing down the loader itself,
so late progress/status emits from in-flight downloads can't update
the new engine's UI state. `controller.set_tts_service` rejects
mid-pipeline swaps with a logged warning rather than risking a torn
state in `_run_pipeline`. Re-selecting the current engine cancels
any deferred swap that was queued behind a busy pipeline.
`VOICEAGENT_TTS_ENGINE` is honored on first launch (persisted to
QSettings so the desync recovery path doesn't silently revert it
to piper).

**LM Studio bug workaround.** When the native `/api/v1/models`
endpoint returns `loaded_instances: []` for every model (a known
LM Studio bug as of May 2026), voiceagent falls back to the
OpenAI-compatible `/v1/models` list — but only when there is
exactly one LLM-typed candidate AND the native API confirmed at
least one model is `type: llm`. Ambiguous fallbacks now surface
"no model loaded" honestly instead of guessing.

The Chatterbox engine targets `ResembleAI/chatterbox-turbo-ONNX`
(pure ONNX runtime, no PyTorch dependency). The default `q4` variant
runs at ~1.35× realtime on a Ryzen 5 8640HS CPU; ~6.77 s wall time
for a 5 s utterance. 24 kHz mono output.

### Notes
- Chatterbox extras require Python ≥ 3.11 (matches voiceagent's own pin).
- GPU acceleration is not available on AMD integrated GPUs (Phoenix /
  RDNA3 iGPU) due to ONNX Runtime not having a Vulkan execution
  provider; CPU inference is the only path on those machines today.
- Watermarking (Resemble's PerthImplicitWatermarker) is disabled by
  default to avoid an extra dep; can be enabled by setting
  `VOICEAGENT_CHATTERBOX_WATERMARK=1`.
- The `huggingface_hub` upper pin is bumped from `<1` to `<2` to keep
  the venv coherent with `transformers` 5.x's hub requirements.
- `VOICEAGENT_*_ROOT` env vars are now treated as unset when blank —
  a stray empty assignment in a shell rc no longer diverts caches
  into the launch directory.
- Session Setup ComboBoxes (engine, voice, speech, LLM model)
  highlight the currently-selected entry on dropdown open. The
  default Qt Quick Controls delegate only highlights keyboard hover
  under the KDE/Breeze style, leaving the actual selection visually
  indistinguishable from the other rows.

## 0.11.1 — 2026-05-06

**Qt 6.11 compatibility + Kokoro engine scaffolding (shelved).** Manjaro
and Arch pushed Qt 6.11 system-wide; voiceagent's `PySide6>=6.10,<6.11`
pin meant the venv-side PySide refused to load the system-side
6.11-built Kirigami plugin, blocking the app from launching. Bump the
upper bound to `<6.12` so the venv can pull a Qt-6.11-compatible
PySide.

A second strand of work for v0.12 — adding **Kokoro** as a second TTS
engine — landed in this branch as a draft and was then shelved without
being merged into the user-visible UI. Upstream `kokoro-onnx` (`<3.14`)
and `misaki` (`<3.13`) don't yet support Python 3.14, which is the
current Manjaro / Arch default. Shipping the engine selector with a
"Kokoro option that doesn't install on your Python" was rejected as a
half-working feature. The `KokoroTtsService` skeleton (single-bundle
download, sidecar trap, lazy import, default voice catalog, `synthesize`
plumbing) is preserved as a reference comment in
`src/voiceagent/services/kokoro_tts.py` for future use; nothing imports
it. The engine-selector architecture (live runtime swap, factory
branch, `set_tts_service`, per-engine voice memory) will land alongside
v0.13's second-engine work, when the second engine is one that actually
installs on a current Manjaro Python.

## 0.11.0 — 2026-04-29

**Multi-turn conversation history.** Voiceagent up to v0.10.x was
single-turn — `LmStudioClient.complete()` rebuilt `messages` from
scratch on every call (system + user only), so the model saw each turn
as a brand-new conversation. v0.11 ships history serialization and a
turn-count cap that trims the visible transcript in place — so
follow-ups like "what about *that* one?" resolve correctly, and what
the user sees is exactly what the LLM sees on the next call. Roadmap
entry retired; v0.12 (MCP tool calling) now unblocked.

### Design choice — visible transcript == LLM payload

The cap is enforced by trimming the `ConversationModel` itself, not
just the serialized payload. When a new assistant response lands and
the conversation has more than `max_history_turns` finalized
user/assistant entries, the oldest pair (and any per-turn status /
system breadcrumbs that fall before the new kept-window head) is
dropped from the model. The user sees the same transcript the model
will see on the next turn — no "phantom history" that's visible but
not in scope.

### Design choice — conversation persists across model swaps

Earlier iterations of this milestone wiped the transcript on
`selected_model_changed`. That was an over-correction. Modern
instruction-tuned local models handle each other's transcripts well,
and a surprise wipe (especially when comparing models on the same
question) is the bigger UX cost than theoretical incoherence. The
context-token bar still resets on swap and the new model's
`loaded_context_length` is re-fetched, so the visual warning stays
accurate under the new ceiling. If the existing transcript exceeds
the new model's context window, LM Studio silently truncates from
the front — the token-aware trim follow-up (roadmap.md v0.11.x)
addresses that automatically; for v0.11 the user can shrink
`VOICEAGENT_MAX_HISTORY_TURNS`.

### How conversation memory works (newcomer note)

When voiceagent talks to LM Studio over the OpenAI-compatible
`/chat/completions` endpoint, the server is **stateless** — every
request stands alone, and the server forgets everything as soon as it
sends the reply. The only way to give the model "memory" is for the
**caller** to include the full prior conversation in every new request.
That conversation is sent as a structured `messages` array:
`[{role: "system", content: "…"}, {role: "user", content: "…"},
{role: "assistant", content: "…"}, …]`.

v0.11 fixes the previous single-turn behavior by serializing the
visible transcript into the `messages` array on each new turn. The
model now sees prior turns and can resolve follow-ups. Token economics
matter: every turn you keep makes the next request longer, and every
model has a hard limit on how many *tokens* (chunks of ~0.75 words
each) it can read in one call — its **context window**. v0.11 ships a
simple turn-count cap (default: last 20 user/assistant entries) to
keep the prompt bounded, configurable via
`VOICEAGENT_MAX_HISTORY_TURNS`. **The cap trims the visible transcript
in place** — when an oldest pair scrolls out of the LLM's context, it
also disappears from the chat pane, so what you see and what the
model sees stay in lockstep. A token-aware trim that adapts
automatically to the loaded model's `loaded_context_length` is on the
v0.11.x roadmap; turn-count is good enough to unblock the multi-turn
experience and ship.

The chosen design matches LangChain's `ConversationBufferWindowMemory`
and Semantic Kernel's `ChatHistoryTruncationReducer`. Most local-LLM
desktop apps (LM Studio's own UI, Open WebUI, GPT4All) ship hard
truncation with no built-in summarization, so v0.11's explicit
turn-count cap is more conservative — i.e. more correct — than what
those apps default to internally. Fancier alternatives surveyed and
deliberately rejected: summary-buffer hybrid (LLM summarization mid-
pipeline = unacceptable latency for voice), RAG-as-memory (embedding
+ vector store = overkill for a single-user assistant),
MemGPT-style tiered memory (designed for weeks-of-continuity agents).

### Added — `LmStudioClient.complete()` signature change

- **Signature: `user_text: str` → `messages: list[dict[str, str]]`**.
  The caller now owns the full message list — no auto-injection of
  the system prompt. `messages=[]` raises `RuntimeError` early.
  Streaming, usage callback, thinking-channel routing all unchanged.
  This is a public-API break for direct callers; voiceagent's
  in-tree use site is the only known one.

### Added — `ConversationModel`

- **`to_openai_messages(system_prompt, max_turns=None)`** —
  serializes finalized user/assistant turns to the OpenAI message
  format. Skips `role="system"` operational notices,
  `role="status"` pipeline rows, drafts (`bubbleState="draft"`),
  in-flight turns (`turnPending=True`), and empty-text rows. The
  assistant's `thinkingText` (`reasoning_content` channel) is NOT
  replayed as content — DeepSeek and OpenAI both explicitly document
  that intermediate reasoning tokens should not round-trip into the
  next turn unless the turn includes tool calls (out of scope until
  v0.12 MCP).
- **Pair-integrity guard.** If the cap lands on an odd cut, the
  slice can lead with `assistant` (no preceding `user`), which the
  model interprets as unfinished. The guard drops one more entry so
  history always starts on `user` (or empty) — mirrors LangChain
  `ConversationBufferWindowMemory`'s "drop a complete pair"
  semantics.
- **`clear()`** — `beginResetModel` / `endResetModel` wipe; no-op +
  signal-silent on an already-empty model.

### Added — `ConversationTurnCoordinator.clear()`

Wraps `model.clear()` and resets the four internal per-turn fields
the coordinator owns: `_streaming_assistant_index`,
`_current_turn_user_bubble_present`, `_pending_status_log_states`,
`_last_logged_status_state`. Emits `conversation_changed` only when
rows actually existed pre-clear.

### Added — `AppConfig.max_history_turns` + env var

- New `AppConfig.max_history_turns: int = 20`.
- Env override: `VOICEAGENT_MAX_HISTORY_TURNS`. Invalid values fall
  back to default; negative values clamp to 0 (= unbounded).
- README env-var reference left for a follow-up touch-up; the
  config field carries the documentation in source.

### Added — `VoiceController` history hook

- **`chat_history_provider: Callable[[], list[dict]] | None`** —
  attribute set by callers (window) to a closure that returns the
  current conversation history as an OpenAI messages list.
- **`max_history_turns: int = 20`** — set by `app.py` from
  `AppConfig.max_history_turns`.
- **`_handle_segment_ready`** captures the snapshot via the provider
  on the GUI thread *before* `executor.submit(...)` — the
  `ConversationModel` is read while the GUI thread is still the sole
  mutator. Provider exceptions log + fall through to the
  v0.10-equivalent single-turn payload.
- **`_run_pipeline(audio_path, history_snapshot)`** appends
  `{"role": "user", "content": <transcript>}` to the snapshot and
  posts the full list. Empty-transcript path (v0.9.14) skips the
  chat call entirely so a no-speech turn does not land in history.

### Added — Cap-driven trim of the visible transcript

`ConversationTurnCoordinator.set_max_history_turns(n)` plus a
`_trim_to_history_cap` helper invoked at the end of every
`on_assistant_response`. After a complete user/assistant pair lands,
the helper counts finalized rows and drops oldest entries (rounded
to a complete pair via the same pair-integrity guard) until the
total fits the cap. The streaming-draft assistant index is adjusted
to track its new row position when the front of the model is
trimmed; status / system rows belonging to dropped turns disappear
along with their parent pair.

`MainWindow` wires `controller.max_history_turns` →
`coordinator.set_max_history_turns(...)` once at construction.

### Tests (363 → 442, +79)

- `tests/test_chat.py` (+3) — `test_complete_posts_messages_verbatim_no_injection`
  locks the v0.11 contract that the client never injects the system
  prompt on top of the caller's list. `test_complete_raises_when_messages_empty`
  guards the empty-list shape. All existing `complete("...")` callers
  migrated to messages-list shape.
- `tests/test_conversation_model.py` (+10) — `clear` (with/without
  reset-signal observation), `to_openai_messages` round-trip, skip
  thinking, skip drafts/pending/system/status, skip empty text, omit
  empty system prompt, max-turns cap behavior, cap=0/None unbounded,
  pair-integrity guard on odd cut.
- `tests/test_conversation_turn_coordinator.py` (+10) — coordinator
  `clear` resets all internal state, emits signal only when
  non-empty. Trim-on-assistant-response: drops oldest pair when
  cap exceeded, no-ops below cap, treats `cap=0` as unbounded,
  drops per-turn status breadcrumbs along with their parent pair,
  pair-integrity rounds excess to even, single
  `conversation_changed` per landing despite the trim, negative
  cap clamps to 0.
- `tests/test_controller_history.py` (NEW, +10) — `_run_pipeline`
  appends user turn after history; falls back when no provider; no
  system message when chat client has empty prompt; provider list
  not mutated; empty transcript skips chat call. Plus
  `AppConfig.max_history_turns` env-var parsing (default, override,
  invalid, negative-clamp).
- `tests/test_mainwindow_integration.py` (+4) — provider wired to
  window; serializes visible turns; model-switch preserves
  conversation (per-model context counters reset only); cap
  propagated from `AppConfig` → coordinator; visible transcript
  trims when cap fires (with snapshot read confirming the model
  and provider agree).
- `tests/fakes.py` — `FakeChatClient` gained `system_prompt`
  attribute + `complete()` keyword-args matching the real shape.

### Replaced — Mute toolbar action → per-bubble ▶/🤫 toggle

The toolbar Mute action and its underlying machinery (`audioMuted`
Q_PROPERTY, `setAudioMuted` slot, `AudioPlayer.set_muted`,
`muted_changed` signal, the worker's mute branch + counter) were
retired. Muting mid-utterance is genuinely useless once the
audio's been generated — the user wanted a "stop reading this turn
NOW, leave the mic hot for the next thing I'm about to say"
button.

Each assistant bubble now carries an inline button that toggles
between ▶ (when nothing is playing this row's audio) and 🤫 (when
this row IS being read aloud — by the in-pipeline auto-play OR by
a user-triggered replay). Click ▶ → `replayMessage(index)`. Click
🤫 → `stopSpeaking()`, which calls `stop()` on both
`controller.player` (in-pipeline) and `replay_player` (user
replays) and resets a new `speakingRow: int` Q_PROPERTY back to
-1. The voice connection is deliberately untouched so the next
user turn can begin immediately.

`speakingRow` tracks which row owns the currently-playing audio:

- in-pipeline auto-play → most recent finalized assistant row
  (resolved via `find_message_index("assistant", bubble_state="sent",
  turn_pending=False)` on `playback_started`).
- replay → the row index the user clicked, stashed in
  `_pending_replay_row` before `play_file` so the
  `playback_started` signal can pick it up.
- both reset to -1 on `playback_finished` / `playback_failed`,
  and on the explicit `stopSpeaking()` slot.

### Fixed — Model-switch timeout (pre-v0.11 bug, hit during testing)

`AppConfig.lm_studio_timeout_seconds` (default 10 s) was applied
to every HTTP call including `POST /api/v1/models/load`, which
LM Studio frequently takes 30-90+ seconds to complete (longer on
first-load with cold disk). Result: model switches initiated from
within voiceagent timed out with "timed out after 10 seconds"
even though the same swap from LM Studio's own UI worked. Added
`AppConfig.lm_studio_load_timeout_seconds` (default 300, env
`LM_STUDIO_LOAD_TIMEOUT_SECONDS`), wired through to a per-call
override on `_json_request` used only by the `/models/load`
POST. Fast-path queries (list models, fetch context length,
list-loaded, unload) keep the 10 s budget so unreachable-server
errors still surface promptly.

### Fixed — PortAudio teardown error surfacing as playback failure

User stopped a long TTS playback mid-utterance. The worker was
blocked inside `stream.write()`; `stop()` set `stop_event` then
abandoned the still-blocked worker after 250 ms. ~21 s later the
abandoned worker finally unblocked with `PortAudioError -9999`
(host audio API teardown side-effect) and the catch-all
exception handler emitted `playback_failed` → red error in the
conversation pane for a turn that had already finished cleanly.
Fix: in the playback worker's exception handler, suppress
exceptions that fire after `stop_event.is_set()` OR after the
worker has been superseded (`not _is_current_generation(gen)`).
Real failures with a live, current worker still surface
unchanged so genuine device errors aren't silently swallowed.

### Added — session-rotated conversation log

New `~/.local/state/voiceagent/logs/conversation.log` captures
the full LLM context per turn (transcript, every entry of the
`messages` list, assistant response, token usage, trim events,
model swaps). Rotates by SESSION (each launch shifts the prior
to `.1`, drops the oldest beyond `.5`), separate from the main
`voiceagent.log` size-rotation. Useful when debugging multi-turn
behavior — "what context did the model actually see for that
turn?" — without bloating the main app log.

### Note — visualtest gate

`./voiceagent-visualtest.sh` continues to fail with
`AttributeError: 'PySide6.QtGui.QWindow' object has no attribute
'grabWindow'` on this branch. **Pre-existing on main** — confirmed
by stashing v0.11 changes and running on a clean tree → identical
failure. Caused by a PySide6 API drift (`grabWindow()` is on
`QQuickWindow`, not `QWindow`); not addressed in this PR. The other
three gates (pytest, compiletest, qatest) pass.

## 0.10.2 — 2026-04-29

**First-party-surface review-feedback cleanup.** User did a thorough
audit of the v0.9.0–v0.10.1 surface (mostly merged without bot
review per the per-cycle subagent-PR convention) and surfaced 10
findings: 1 P1, 5 P2, 4 P3. All ten addressed.

### Fixed (P1)

- **Opacity-fade policy violation** — v0.9.0's page-mic refactor
  added `Behavior on opacity` blocks to the dashboardModes
  ColumnLayout and the compactLoader, against AGENTS.md "Mode
  transition animation" → "No opacity fades anywhere". Removed;
  `visible: root.{medium,compact}Mode` drives the panes directly.
  The page-level mic's `Behavior on x/y/width/height` carries the
  visual transition via geometry slide.

### Fixed (P2)

- **Piper SHA pinning shared state** — `_current_download_sha` was
  one shared scalar across `tts_loader`'s `max_workers=3`
  executor, so two parallel voice installs could overwrite each
  other's verifier context. Replaced with `_download_sha_by_name:
  dict[str, str]` keyed by voice name, guarded by a dedicated
  lock. Consume-on-read pop prevents unbounded growth. New
  concurrency tests in `test_tts_sha_pinning.py` (+2).
- **LLM model switch ordering** — `LmStudioClient.load_model()`
  unloaded the previously-loaded model BEFORE confirming the new
  load. A failed load left the user with no working LLM. Reordered:
  load first, unload others only after `status == "loaded"`. Peak
  memory briefly 2x; the trade keeps the working model alive on
  load failures. New tests in `test_chat.py` (+2).
- **Release metadata drift** — `PKGBUILD` was on 0.8.7 and
  `packaging/PKGBUILD.aur` + `.SRCINFO` were on 0.4.0 while runtime
  was on 0.10.1. All three bumped to 0.10.2. New
  `tests/test_release_metadata.py` (+4) asserts the four release
  files agree with `__version__` so future drift fails CI.
- **Executor shutdown guards** — three executors
  (`MainWindow._context_length_executor`,
  `LlmController._llm_executor`, `ParallelItemLoader._executor`)
  could accept `submit()` calls AFTER `shutdown()` returned.
  Added `_shutdown_started` flags + `RuntimeError` catches at all
  submit sites; `ParallelItemLoader` additionally rolls back
  optimistic state mutations so cancelled installs don't leave
  rows stuck "loading". New tests in
  `test_controller_thread_safety.py` (+3).
- **Layout policy not locked in tests** — `tests/visual/visual_smoke.py`
  saves PNGs but doesn't assert. New `tests/qml/tst_layout_policy.qml`
  string-greps production QML for `Behavior on opacity` and asserts
  zero matches, so future re-introduction of the v0.9.0 slip fails
  CI. Three additional structural tests (mic-anchor geometry,
  ultraCompact feed collapse, mediumMode floor) ship as `skip()`
  pending a richer `StubVoiceAgent`; the policy-grep test (the
  highest-value invariant from the review) runs and passes.

### Changed (P3)

- **AGENTS.md stale numbers** — "Three" → "Four" gates, "~219
  tests" → "~363", "saving 15 PNGs" → "33". `tests/visual/visual_smoke.py`
  breakpoint comments refreshed to current thresholds (gu*6 floor,
  gu*10 ultraCompact).
- **QML stub signal name drift** — `StubVoiceAgent.qml` declared
  `replayFailed` (camelCase) while production connects to
  `voiceAgent.replay_failed` (snake_case, matching the Python
  signal). Renamed stub so QML tests exercise production binding
  shape.
- **Ruff baseline** — `ruff check .` had 65 errors. 12 unused
  imports auto-fixed; 1 unused local variable removed; 41 E402
  + 9 E731 cases (legitimate Qt-bootstrap patterns + lambda stubs
  in tests) configured as `per-file-ignores` for `tests/**` only.
  Production source still enforces both rules. Final state:
  `All checks passed!`.
- **`roadmap.md` tracked** — moved from untracked to tracked at
  lowercase `roadmap.md`. Private machine path
  (`~/.claude/plans/...`) replaced with placeholder. Roadmap
  fleshed out with v0.11 conversation-history plan and v0.12 MCP
  internet-access plan.

### Added — class docstrings

Three core classes that lacked module-level documentation:
`LmStudioClient`, `VoiceController`, `MainWindow`. Stale
`root.scrollList(...)` reference in `ConversationPane.qml` module
comment also cleaned up (orchestrator-level function deleted in
v0.9.12).

### Tests

Net pytest delta: 363 → 374 (+11). qmltestrunner: 9 → 16
(+1 layout-policy passing; SessionSetupPane / MainWindow /
ScrollMode totals unchanged from prior release).

## 0.10.1 — 2026-04-29

**v0.10.0 cleanup pass.**

### Fixed

- **Conversation bubble alignment regression** — v0.10.0's
  Layer-7 `assistantColumn` ColumnLayout wrapper around
  `messageRow` broke `RowLayout.layoutDirection` ordering
  under Qt6 Quick Layouts. User bubbles rendered left-aligned
  instead of right-aligned, and the assistant bubble lost its
  Replay button placement. Replaced `layoutDirection` with
  explicit `Layout.alignment` + fillWidth spacers — bubbles now
  align right (user) / left (assistant) regardless of
  surrounding layout context.
- **Whisper empty-transcript log spam** — v0.9.14 bumped the
  empty-transcript log line from `INFO` to `WARNING`. The
  partial-probe loop fires Whisper every ~350 ms while voice
  is connected, so silent buffers produce a stderr WARNING per
  probe. Walked back to `INFO` since empty results are common,
  not exceptional. `tests/test_stt.py` assertion updated to
  match.

### Removed

- **`FOLLOWUPS.md`** — file deleted. The deferred-indefinitely
  note about `_emit_initial_state` moved to a one-line code
  comment at `model_loader.py:50`. The user-smoke checklist
  moved to a new "Subjective smoke" section in `AGENTS.md`.

## 0.10.0 — 2026-04-29

**Streaming chat completions, thinking expander, context-token
bar.** Three intersecting capability additions shipped as one
bundle. Closes the v0.9.x 60-second timeout class, makes
reasoning-model thoughts visible per turn, and surfaces token
usage against the loaded model's context window.

### Added — streaming chat (v0.9.x timeout class fixed)

- **`services/chat.py:LmStudioClient.complete()`** now sets
  `stream: True` + `stream_options.include_usage: True`, parses
  SSE chunks, accumulates `delta.content` into the returned
  answer string. Optional callbacks fire from the worker thread:
  - `on_content_chunk(text)` — incremental answer text
  - `on_thinking_chunk(text)` — incremental
    `delta.reasoning_content` (LM Studio's R1-style thinking
    surface)
  - `on_usage(usage_dict)` — fires once on the final chunk
- **Per-read timeout** instead of total-response cap.
  `timeout_seconds` now bounds per-chunk gaps, so multi-minute
  generations are fine as long as tokens keep arriving.
- **`fetch_loaded_context_length()`** queries LM Studio's native
  `/api/v1/models` for the loaded model's context window
  (`loaded_context_length` with `context_length` fallback).

### Added — thinking expander (UX C)

- **`ConversationModel`** — `ThinkingTextRole`,
  `ThinkingExpandedRole` per row.
- **`ConversationTurnCoordinator`** — streams chunks into a
  draft assistant row created on first chunk. Default
  `thinkingExpanded` snapshots `verbose_mode` at row insertion
  (per-item state, never overwritten by chunks).
  `set_thinking_expanded(row, bool)` slot for QML toggling.
- **`ConversationPane.qml`** delegate — collapsible "Thoughts"
  expander attached above each assistant bubble that has
  thinking content. Chevron rotates 0° → 90° on expand. Body
  italic, theme-aware purple (matches existing `statusTextColor`
  pair). Maps cleanly to `<details><summary>Thoughts</summary>
  *body*</details>` in markdown export.

### Added — context-token progress bar

- **`MainWindow.contextTokensUsed`** /
  **`contextTokensCeiling`** Q_PROPERTYs, notified by
  `ui_changed`.
- **`setThinkingExpanded(int, bool)`** Slot bridging QML to
  the coordinator.
- **MainWindow worker** for the context-length probe — private
  `ThreadPoolExecutor` triggered on
  `LlmController.selected_model_changed`. Calls
  `chat_client.fetch_loaded_context_length()` off the GUI
  thread, posts the result back via a private signal. Stale-
  result guard against fast load-then-switch.
- **`MainWindow.qml`** — thin progress strip at the bottom of
  the page. Bar fill `highlightColor` < 75 %,
  `neutralTextColor` 75–90 %, `negativeTextColor` > 90 %. Hidden
  when ceiling is 0. Count label hidden in `ultraCompactMode`.

### Tests

- `tests/test_chat.py` (43 → 52, +9): SSE streaming, chunk
  callbacks, `fetch_loaded_context_length` happy/fallback/empty
  paths.
- `tests/test_conversation_model.py` (19 → 25, +6): new role
  round-trips + `dataChanged` emission.
- `tests/test_conversation_turn_coordinator.py` (+15): streaming
  lifecycle, draft row promotion, `thinkingExpanded` snapshot,
  `set_thinking_expanded` no-ops.
- `tests/test_mainwindow_integration.py` (+8): chunk
  forwarders, context-token state, ceiling probe round-trip,
  stale-result guard.
- Net (vs v0.9.14): 325 → 363 (+38).

## 0.9.14 — 2026-04-29

**Empty Whisper transcript no longer crashes the pipeline.** When
the user records silence or non-speech audio, Whisper returns no
segments. Previously `services/stt.py` raised
`RuntimeError("Whisper did not return any transcript ...")` and
the pipeline failed with a red error row. Empty transcripts are
now treated as a no-op turn.

### Fixed

- **`services/stt.py:transcribe()`** returns `""` instead of
  raising when no segments are produced. Existing log entry
  retained at `WARNING` level so verbose-log mode still shows
  the no-speech event.
- **`controller.py:_run_pipeline()`** short-circuits when
  `transcript.strip()` is empty: returns a no-op
  `PipelineResult`, the chat client is never called, audio
  cleanup still fires.
- **`conversation_turn_coordinator.py:on_user_transcript("")`**
  drops any dangling `turnPending=True` user bubble that was
  created during transcription, so silence after a draft doesn't
  leave a stuck row.

### Tests

- `tests/test_stt.py`, `tests/test_controller_thread_safety.py`
  (+3), `tests/test_conversation_turn_coordinator.py` (+2).
  Net: 320 → 325 (+5).

## 0.9.13 — 2026-04-28

**Two reverts from v0.9.12 user testing.**

### Reverted

- **Model Manager back to a modal `Window`** (undoes v0.9.3).
  The Kirigami.Page approach pushed onto pageStack hid the
  Voice Models toolbar action in a "More Actions" hamburger and
  left no obvious close affordance. User explicitly preferred
  the original separate-window dialog. `ModelManagerPage.qml`
  deleted; `Window { id: modelManagerWindow ... }` restored
  inline in `MainWindow.qml`.
- **Conversation scroll back to direct-only** (undoes v0.9.12,
  invalidates the v0.9.9–v0.9.11 multiplier rounds). The
  Kirigami.WheelHandler experiment shipped at "1/5 native speed
  and no inertial" per user. CatalogList — which scrolls fast —
  uses the original PR #23 multipliers (`pdy * 8` /
  `gridUnit * 12`), a `MouseArea` overlay, and no inertial
  flick branch. Conversation view now matches that pattern
  verbatim. `ListView.flickDeceleration: 1800` restored.

The multiplier-tuning rounds were chasing a problem that didn't
actually exist at PR #23 multipliers — the perceived slowness
was specific to the conversation view's wiring (Kirigami.Wheel-
Handler in v0.9.12, plus the inertial-flick branch that
overrode each wheel event in v0.9.x). CatalogList has been
scrolling fast with unchanged multipliers throughout.

## 0.9.12 — 2026-04-28

**Replace custom scroll curve with `Kirigami.WheelHandler`.** Three
multiplier-tuning rounds (v0.9.9 / .10 / .11) didn't close the gap
— user reported scrolling was still ~1/5 the speed of other apps
and producing no inertia. The hand-tuned `scrollList()` formula was
the wrong abstraction for Plasma 6 / Wayland's high-resolution
scroll event semantics. Drop the custom curve entirely and use the
framework primitive.

### Removed

- `MainWindow.scrollList()` (~60 lines).
- `MouseArea` overlay in `ConversationPane.qml`'s ListView that
  routed wheel events to scrollList.
- `tests/qml/tst_scroll_mode.qml` (~155 lines) — tested the
  removed function. qmltest total drops 16 → 9.
- Hand-tuned `ListView.flickDeceleration: 900` from v0.9.11.

### Added

- `Kirigami.WheelHandler { target: conversationView;
  filterMouseEvents: true }` inside the ListView. Handles mouse
  wheel, hi-res mouse, and touchpad continuous events with
  proper source-aware velocity and Flickable-native inertia.
  The existing `onMovementStarted` / `onMovementEnded` sticky-to-
  bottom logic continues to work unchanged.

## 0.9.11 — 2026-04-28

**Inertial scroll glide retuned ~8x.** v0.9.9 doubled the wheel
multipliers but scroll was still sluggish. The inertial branch's
glide distance is `velocity² / (2 × flickDeceleration)` —
quadratic in velocity, not linear. Two-axis retune:

### Changed

- **Inertial velocity multipliers** (`MainWindow.scrollList`)
  doubled again: `pdy * 80` → `pdy * 160`,
  `gridUnit * 120` → `gridUnit * 240`. The squared term gives
  4x glide per notch.
- **`ListView.flickDeceleration`** halved in
  `ConversationPane.qml`: `1800` → `900`. Linear contribution
  doubles glide on top.
- Combined: ~8x glide vs v0.9.9. Direct path multipliers
  unchanged.
- **`tests/qml/tst_scroll_mode.qml`** magnitude assertion
  updated.

## 0.9.10 — 2026-04-28

**Verbose toggle hides existing status rows + theme-aware status
purple.**

### Fixed

- **Verbose toggle didn't hide existing status rows.** The
  coordinator already gates *new* status rows on
  `logVerboseMode`, but rows already in the model stayed
  visible until restart. The delegate now reads
  `voiceAgent.logVerboseMode` live and collapses status-row
  `implicitHeight` + `visible` when verbose is off, so toggling
  reflows the layout immediately.

### Changed

- **Status text purple now theme-aware.** Single hardcoded
  `#9b6bcc` was washed out on Light and muddy on Dark. Same
  hue (~270°), shifted lightness per theme:
  - Light: `#7b4ab8` (deeper purple)
  - Dark : `#bf95e8` (softer lavender)

## 0.9.9 — 2026-04-28

**Light-mode polish bundle.** Four user-reported issues from
v0.9.8.

### Fixed

- **Mic button text white-on-white in Light mode** —
  `MicButton.qml` hardcoded `icon.color: "white"` and
  `palette.buttonText: "white"`. Worked when `buttonColor` was
  always saturated (Plasma highlight blue/teal), broke in Light
  mode when `buttonColor` falls back to
  `Kirigami.Theme.alternateBackgroundColor` (a near-white
  surface). Auto-derive icon + text color from button-bg
  luminance (BT.601), same luminance-fork pattern the bubbles
  use.
- **Theme toggle silently collapsed to overflow** — even with
  `IconOnly` + `visible: !compactMode`, Kirigami's
  `ActionToolBar` pushed the action to a hidden overflow bucket
  when two text+icon actions consumed the available width. Add
  `KeepVisible` so the action never collapses.
- **Wheel-scroll regression on Plasma 6 / Wayland** — the
  `scrollList()` body has been byte-identical to v0.8.0 PR #23
  but the multipliers now feel sluggish under Plasma 6 / Wayland
  wheel-event semantics on the user's hardware. Doubled:
  - direct path: `pdy * 8` → `pdy * 16`,
    `(ady/120) * gridUnit * 12` → `gridUnit * 24`
  - inertial path: `pdy * 40` → `pdy * 80`,
    `gridUnit * 60` → `gridUnit * 120`

### Changed

- **Toolbar action labels** — per user spec "each button should
  have a label", drop `IconOnly` from `themeAction` and
  `verboseLogAction`. Theme labels simplify to Auto / Light /
  Dark; verbose label is a constant "Verbose" (the open-eye /
  slashed-eye icon carries on/off state).
- **`tests/qml/tst_scroll_mode.qml`** magnitude assertions
  updated to match the doubled multipliers.

## 0.9.8 — 2026-04-28

**Cycling 3-way theme toggle.** The theme action was a submenu
(`Kirigami.Action` with three checkable children inside an
`ActionGroup`) — two clicks per mode change. Now a single
icon-only action that cycles `Auto → Light → Dark → Auto` on each
click. Icon and tooltip reflect current state + next state.

### Changed

- **`themeAction` in `MainWindow.qml`** — single cycling action
  replacing the submenu. Icon: `preferences-desktop-theme-symbolic`
  (Auto), `weather-clear-symbolic` (Light), `weather-clear-night-
  symbolic` (Dark). Tooltip carries current+next state.
- **`themeActionGroup` and three child actions** — dropped in
  the same pass.

## 0.9.7 — 2026-04-28

**Violet AI-app bubble palette.** Replaces v0.9.6's warm coffee
assistant palette and the Kirigami `Selection` user-sent
inheritance with a branded violet identity. Stops tracking the
user's Plasma accent — deliberate, the voice agent now has its
own visual signature.

### Changed

- **Sent (user) bubble**:
  - Light: bg `#7C3AED`, text `#FFFFFF`, no border
  - Dark : bg `#A78BFA`, text `#140A2B`, no border
- **Recv (assistant) bubble**:
  - Light: bg `#EDE9FE`, text `#1F1736`, border `#DDD6FE`
  - Dark : bg `#374151`, text `#F9FAFB`, border `#475569`

Draft (transcribing) bubble keeps the pink `#ff5c8a` hold-over
as a transient state marker.

## 0.9.6 — 2026-04-28

**Warm complementary assistant bubble + replayable bool coerce.**
v0.9.5's `View` colorSet rendered the assistant bubble ~5 % off
the page bg on Breeze Dark — read as flat black. Kirigami doesn't
expose a "complementary panel" set, so the assistant bubble is
now an explicit hand-picked warm coffee / cream palette per
theme.

### Changed

- **Assistant bubble palette** — hand-tuned hue ~25° complementary
  to the teal/green Plasma highlight (~170°):
  - Dark theme: bg `#3d3027`, text `#f4e3d0`
  - Light theme: bg `#f4e3d0`, text `#3d3027`
- **Theme detection** — ITU-R BT.601 luminance check on
  `Kirigami.Theme.backgroundColor` (bg < 0.5 → dark theme).
  Stand-alone fork is the only way to get a real complementary
  panel — Kirigami's color sets all converge near page bg in dark.

### Fixed

- **Replay-button QML warning flood** — `visible: !compactMode &&
  model.replayable` produced `Unable to assign [undefined] to
  bool` warnings per row per resize. Rows that aren't replayable
  (system, status, draft) never set `replayable`, and the
  conversation model returns `None` → JS `undefined`. Coerced
  with `!!` so the binding lands a real bool.

## 0.9.5 — 2026-04-28

**Bubble colorSet correction + explicit toolbar icon tint.**
v0.9.4 placed assistant bubbles in `alternateBackgroundColor`,
Kirigami's zebra-stripe slot, not its panel slot — bubbles ended
up ~5 % off Window in both Breeze themes (effectively invisible).
Plus Plasma's symbolic-icon auto-tint dropping out in IconOnly
toolbar slots left the four header icons reading as background.

### Changed

- **Assistant bubbles**: `Kirigami.Theme.colorSet = Theme.View`
  (the canonical "elevated panel" set — what NeoChat / Tokodon
  use for incoming bubbles).
- **User-sent bubbles**: `colorSet = Theme.Selection` (same
  pixels as the v0.9.4 highlight pair, but the colorSet form
  stays consistent with the assistant side and inherits naturally
  for child Labels).
- **Draft bubbles**: `colorSet = Theme.Window` so the explicit
  pink override doesn't fight an inherited set.
- **1-px `separatorColor` border** on non-draft bubbles for edge
  definition on Breeze Light (View bg only ~5 % brighter than
  Window there).
- **`icon.color: Kirigami.Theme.textColor`** on the four
  `Kirigami.Action` instances in `MainWindow.qml` (mute / theme /
  model-manager / verbose-log) — belt-and-suspenders for Plasma's
  symbolic-icon auto-tint dropouts.

## 0.9.4 — 2026-04-28

**Conversation bubbles use Kirigami theme roles.** Hardcoded
`#34c759` assistant green plus white text lacked contrast on
Breeze Light and didn't track theme flips. Switched to Kirigami's
documented color pairs so the palette adapts to Breeze Light /
Dark and to the user's accent color automatically.

### Changed

- **Assistant bubbles**: `alternateBackgroundColor` +
  `textColor` — quiet, like a system panel.
- **User-sent bubbles**: `highlightColor` +
  `highlightedTextColor` — Plasma's "selection" pair, naturally
  accented.
- **User-draft (transcribing) bubbles**: pink (`#ff5c8a`)
  preserved as a transient state marker; flips to the highlight
  pair on finalize.
- **Bubble timestamp**: tracks `bubbleTextColor` at 0.72 opacity
  instead of hardcoded white at 0.72 — reads on both bubble
  styles.

## 0.9.3 — 2026-04-28

**Model Manager → `Kirigami.Page` on `pageStack`.** The Manager
was a top-level modal `Window` opened over the main page; it now
lives in the same QML tree as the rest of the UI, integrated via
Kirigami's standard pageStack navigation conventions (header
title, page actions, padding). Closes the lone "Future feature
work" entry in `FOLLOWUPS.md`.

### Changed

- **`src/voiceagent/qml/ModelManagerPage.qml` (new, 163 LOC)** —
  `Kirigami.Page` hosting the existing TabBar + StackLayout +
  CatalogList structure. `Close` action pops via
  `applicationWindow().pageStack.pop()`.
- **`src/voiceagent/qml/MainWindow.qml`** drops the 145-line inline
  `Window { id: modelManagerWindow ... }` block and the manual
  centering math in `modelManagerAction.onTriggered`. The action
  now calls `pageStack.push(modelManagerPageComponent)`. An
  `objectName: "modelManagerPage"` guard against double-push
  handles repeated action triggers.
- **`FOLLOWUPS.md`** "Future feature work" section pruned (only
  item shipped).

### Tests

- 320 pytest passes (unchanged), 16 qatest passes (unchanged),
  compiletest green, 30 visualtest captures unchanged.

## 0.9.2 — 2026-04-28

**Decompose `services/audio.py:_handle_audio_chunk` into named
state-machine phases.** The audio-stream callback was a 138-line
monolith spanning four overlapping state machines (idle / pre-roll
buffering, speech-candidate detection, active turn tracking,
silence finalization). Behavior is preserved exactly — locking,
signal emissions, callback ordering, numeric thresholds, and
exception handling all match the prior monolith. Net structural
win: orchestrator drops from 138 → 32 lines.

### Refactored

- **`services/audio.py:_handle_audio_chunk`** is now an orchestrator
  that holds the lock, snapshots state, and dispatches to five
  named `_locked` helpers:
  - `_handle_suspended_input_locked(frames, rms)` — admin-suspended
    drop path.
  - `_handle_ignore_window_locked(chunk, frames, rms, remaining)` —
    post-finalize ignore window.
  - `_check_speech_candidate_locked(chunk, frames, rms)` — entire
    not-yet-started branch (candidate accrual, candidate→active
    transition, idle pre-roll).
  - `_track_active_segment_locked(chunk, frames, rms)` —
    speech / silence accrual + chunk append.
  - `_finalize_on_silence_or_max_locked(frames, callback)` —
    silence-timeout / max-turn dispatch.

### Tests

- **`tests/test_audio_chunk_handler.py` (new, +360 LOC, 14 tests)**
  drives the state machine through every transition with synthesized
  chunks and asserts the same observable outputs (signals, callbacks,
  `take_pending_segment`) the original code produced. Tests pass
  against both the unmodified anchor and the refactored source.
- Pre-existing `tests/test_audio_player_race.py` (which covers
  `services/playback.py`, not `services/audio.py`) still passes.

Net suite: 320 passed (was 306 before PR #27).

### Future work flagged in PR #27

Two preserved-but-suspicious patterns spotted while reading
`_handle_audio_chunk`, intentionally left for a separate cycle:

- Max-turn check uses `total_turn_frames + frames` (pre-accrual)
  while silence-timeout check uses post-accrual `_silence_frames`.
  Mathematically equivalent under the accrual invariant, but the
  asymmetric phrasing is a readability tax.
- `_finalize_segment_locked` discards a "short segment" using
  `total_turn_frames < min_speech_frames` — but `total_turn_frames`
  includes silence frames, and silence-timeout finalization always
  carries `~silence_timeout_frames` worth of silence. The natural-
  callback discard branch is therefore essentially unreachable at
  default tunings; only `force_finalize_active_segment` from outside
  hits it. Worth a design check.

## 0.9.1 — 2026-04-28

**Direct test coverage for `services/chat.py` and `services/stt.py`.**
Both modules previously had zero dedicated test files despite TTS
having five. The two new test files mirror the `test_tts_*.py`
pattern and bring the python suite from 222 to 306 tests.

### Tests

- **`tests/test_chat.py` (new, +581 LOC, 43 tests)** — covers
  `LmStudioClient` URL normalization, error formatting (connect /
  timeout / HTTP-status / generic exception classes), request
  composition + response parsing happy paths, and timeout/connection-
  error edge cases. `chat.py` uses `urllib.request.urlopen`, so the
  fakes monkeypatch `voiceagent.services.chat.request.urlopen`
  consistent with `test_tts_catalog_first_paint.py`.
- **`tests/test_stt.py` (new, +675 LOC, 41 tests)** — covers
  Whisper backend lifecycle (start / stop / restart), model-path
  fallback when configured path is missing, compute-type inference
  by hardware probe, and `AppState` / config hydration paths.
  Mirrors the TTS fixture style.

Net suite: 306 passed (was 222 before PR #26).

## 0.9.0 — 2026-04-28

**Single page-level mic with animated cross-mode geometry.** Closes
the three UI rough edges deferred from the v0.8.x responsive sweep.
The previous design used two `MicButtonFrame` instances (one per
pane) and could not animate geometry between the medium-mode
right-of-form position and the compact-mode bottom-of-conversation
position; it also crowded the form's `RowLayout` at the
`gridUnit × 40` floor, causing overlap between the mic and the
URL row's Connect button plus left-clipping on the "Loaded Model:"
label.

### Changed

- **`MainWindow.qml`** now hosts a single `MicButtonFrame` at the
  `dashboardModes` level. Its `x` / `y` / `width` / `height` bind
  via `mapToItem(parent, …)` to whichever pane's `micAnchor` alias
  is active for the current mode. `Behavior on …` 250 ms
  `Easing.OutCubic` smooths the geometry transition when
  `compactMode` flips, producing the slide-between-positions
  animation the user requested.
- **`SessionSetupPane.qml`** and **`ConversationPane.qml`** each
  expose a `micAnchor` alias — an empty `Item` reserving the
  layout slot the mic occupies — instead of instantiating their
  own `MicButtonFrame`. The unused `micPulseActive` /
  `micButtonColor` / `micPulseColor` required properties on
  `SessionSetupPane` drop in the same pass; `MainWindow` now
  feeds those colors directly to its page-level mic.

### Tests

- `tests/qml/tst_session_setup_pane.qml` updated to drop the
  removed mic-color literal props from the inline component
  definition. Full QA suite: 16 passed.
- `pytest tests/`: 222 passed.
- `./voiceagent-visualtest.sh`: 30 PNGs across scale × width.
  Labels fully visible at width 800 / scale 1.0 (the
  `gridUnit × 40` medium-mode floor); mic correctly positioned
  in both modes; `ultraCompactMode` mic-fills-window intact.

### Documentation

- **`AGENTS.md`** "Mode transition animation" section now
  documents the page-level mic + `micAnchor` alias pattern.
- **`FOLLOWUPS.md`** UI rough edges section pruned (all three
  items shipped).

## 0.8.7 — 2026-04-28

**Detect partial-download voices via stale `.aria2` sidecars.** Closes
a class of TTS failures that pre-dates the v0.3.2 layer-4 smoke-load
verifier — voices downloaded before that protection shipped never
went through ONNX validation, and an interrupted download leaves a
truncated `.onnx` next to a `<onnx>.aria2` aria2 control file. The
loader treated those as available, synthesis crashed inside Piper's
`synthesize_wav` without writing WAV headers, and the user saw
`wave.Error: # channels not specified` (the WAV close raises because
`setnchannels` was never called).

### Fixed

- **`PiperTtsService.is_voice_available`** now returns False when a
  `<onnx>.aria2` sidecar exists alongside the `.onnx`. Aria2 leaves
  that file behind only when the transfer didn't complete; its
  presence is a definitive marker for a partial install.
- **`PiperTtsService._resolve_existing_model_path`** now skips
  candidates with a `.aria2` sidecar, so `synthesize()` correctly
  raises its missing-model RuntimeError instead of proceeding to
  `wave.open` and crashing later.

### Tests

- 3 new tests in `tests/test_tts_is_available_consistency.py`:
  `is_available_false_when_aria2_sidecar_present`,
  `is_available_recovers_after_aria2_sidecar_removed`,
  `resolve_path_skips_partial_download`. The existing 4 tests in
  the file still pass — total 7.

### Migration note

If you have voices in your TTS model directory with `.aria2` files
sitting next to them, those voices are corrupt and the v0.8.7 loader
will correctly hide them from the UI. To replace them, delete the
`.onnx` + `.onnx.aria2` + `.onnx.json` triple by hand (or via the
Voice Models UI's Remove action) and re-download fresh. The new
download goes through the v0.7.0 SHA-pinning + layer 2/3 size+md5
verification + layer 4 smoke-load, so it can't end up in the same
state.

## 0.8.6 — 2026-04-28

**Sliding height-collapse instead of opacity fade + smaller
ultraCompact threshold + mic button sizing fixes.** The v0.8.5 fades
were the wrong direction — the user wants elements to *move* between
positions, not dissolve. Plus several rough edges with the
ultra-compact rendering.

### Changed — animation: height collapse, not opacity fade

- Removed the opacity Behaviors on the conversation Item, the
  ConversationPane header row, and the medium/compact mode wrappers
  in MainWindow.qml.
- The conversation-feed Item now uses
  `Layout.maximumHeight: ultraCompactMode ? 0 : 100000` with a
  `Behavior on Layout.maximumHeight` (250 ms `Easing.OutCubic`). When
  ultraCompactMode triggers, the conversation collapses smoothly to
  zero height; the mic button below slides up to fill the freed
  vertical space. No more dissolve transition.

### Changed — `ultraCompactMode` threshold lowered

- `compactMode && height < gridUnit * 28` → `compactMode && height < gridUnit * 10`.
- The conversation now stays visible until it's down to roughly one
  line of content. Below that, ultraCompact takes over and the mic
  fills.

### Changed — minimum window dimensions

- `minimumWidth`: gridUnit × 12 → **gridUnit × 6** (~108 px @1.0x).
- `minimumHeight`: gridUnit × 10 → **gridUnit × 8** (~144 px @1.0x).
- The width floor lets the user shrink to a postage-stamp mic-only
  window (title bar may cramp at this size — that's by design). The
  height floor at gridUnit × 8 ensures the mic button has room for
  icon + (wrapped) status text without bottom-clipping.

### Fixed — mic button bottom-clipping in ultraCompact

- `MicButtonFrame` in `ConversationPane.qml` now has
  `Layout.fillHeight: ultraCompactMode` so the mic claims the
  vertical space the conversation just gave up. Without this it
  stayed at `preferredHeight: gridUnit * 5` and the inner Button's
  `contentItem` (taller from the Cycle 8 wrapping label) would
  overflow the frame and clip at the window edge.
- `iconSize` and `fontPixel` now scale with the actual frame height
  (`Math.max(18, Math.min(48, height * 0.32))` and
  `Math.max(10, Math.min(14, height * 0.10))` respectively) so very
  short windows shrink the icon/font instead of overflowing.

### Fixed — orphaned scroll-to-bottom button in ultraCompact

- The conversation-feed scroll-to-bottom button is anchored to its
  parent's bottom; when the conversation Item collapsed to 0 height
  in ultraCompact, the button still rendered floating at the top of
  the (zero-height) collapsed area. Now also gated on
  `!ultraCompactMode`.

### Deferred — compact ↔ medium "slide up and right" animation

- The user requested that the mic button slide between positions
  during a `compactMode` flip (bottom-of-conversation in compact →
  right-of-form in medium). This requires a single page-level mic
  button instance with animatable x/y/width/height, replacing the
  current two-instance approach (one mic in `ConversationPane.qml`,
  one in `SessionSetupPane.qml`). That's a non-trivial refactor;
  deferred to a follow-up.

## 0.8.5 — 2026-04-28

**ultraCompactMode + 250 ms reorientation animations + smaller minimum
window.** When the user has shrunk the window to "I just need the
mic" mode, the conversation pane is dead space. Hide it; let the mic
button fill the window. Plus: lower the minimum-window floor so the
window can shrink to a small mic-only widget.

### Added — `ultraCompactMode`

- New responsive mode in `MainWindow.qml`:
  ```
  readonly property bool ultraCompactMode:
      compactMode && height < Kirigami.Units.gridUnit * 28
  ```
- When true, `ConversationPane.qml` hides the conversation feed
  (header row + ListView + scroll-to-bottom button) and the
  MicButtonFrame fills the available space.
- `pageContentMargin` and `ConversationPane.padding` drop to 0 in
  ultraCompactMode so the mic is hemmed against the window edges.

### Changed — minimum window dimensions

- `minimumWidth` lowered `gridUnit * 22` → `gridUnit * 12` (~216 px
  at 1.0x).
- `minimumHeight` lowered `gridUnit * 18` → `gridUnit * 10` (~180 px
  at 1.0x).
- The user can now shrink to a tiny mic-only window. Title bar still
  shows "Voice Agent" + Mute action; mic button + status text fit.

### Changed — MicButton.qml status text wraps instead of eliding

- Custom `contentItem: ColumnLayout { Kirigami.Icon; Label }` with
  `wrapMode: Text.WordWrap` and `elide: Text.ElideNone` so longer
  status strings ("No model loaded", "Whisper transcribing…") line-
  break at narrow widths instead of truncating to "No model lo...".
- Default `AbstractButton`'s `IconLabel` content elides with "..." at
  narrow widths, which was exactly what the user reported on the
  pre-fix small-window screenshot.

### Added — 250 ms cross-fade between responsive modes

- `ConversationPane.qml`: header `RowLayout` and conversation Item
  fade out over 250 ms when `ultraCompactMode` flips on.
- `MainWindow.qml`: medium-mode `ColumnLayout` and compact-mode
  `Loader` cross-fade over 250 ms when `compactMode` flips. Loader
  `active:` still gates instantiation by mode (smooth fade-IN, instant
  unload on hide is an acceptable simplification — the new mode's
  content fades in over the disappearing one).
- Easing: `Easing.InOutQuad`. Duration matches Kirigami's roughly-
  standard transition feel.

### Changed — visual smoke runner

- `tests/visual/visual_smoke.py` adds a `SHORT_CAPTURES` list to
  exercise the ultraCompactMode threshold:
  `(220, 200)`, `(280, 280)`, `(340, 360)`, `(400, 460)` per scale.
  Verifies the mic-fills-window layout at very small dimensions.

## 0.8.4 — 2026-04-28

**Window-sizing rework: drop max caps, raise min width, fix mediumMode
form column squeeze.** The visual smoke at 1.0x didn't reproduce the
actual broken layout the user saw on Plasma at higher scale. Three
coordinated fixes in MainWindow.qml + SessionSetupPane.qml.

### Changed — window flags + sizing policy

- **Removed `maximumWidth` / `maximumHeight` caps.** v0.7.0 capped
  width at `gridUnit * 49` and height at `Screen.desktopAvailableHeight`
  to suppress maximize. The caps were more annoying than helpful — fullscreen
  blocking is worse UX than a layout that scales naturally. Window can
  now be maximized / fullscreened freely; the layout adapts.
- **Restored `Qt.WindowMaximizeButtonHint`** in the window flags.
- **Removed the `onVisibilityChanged` snap-to-Windowed handler** that
  forced any maximize / fullscreen request back to Windowed.
- **Added `minimumWidth: gridUnit * 22`** floor. Below this, the title
  bar collapses actions into "..." overflow and the compact mic button's
  "LLM disconnected" status truncates. gridUnit-scaled keeps the floor
  scale-aware across Plasma 100% / 125% / 150% / 200%.
- **Added `minimumHeight: gridUnit * 18`** floor for the same reason.

### Changed — compactMode threshold

- **`compactMode` threshold raised 35 → 40 grid units.** v0.8.2's gu*35
  fixed offscreen 1.0x captures but the user's actual Plasma desktop
  at higher Wayland scale showed the same form-column squeeze + mic
  overlap. At 1.5x scale the gridUnit-multiplied control widths don't
  fit at typical 1000 px windows. gu*40 gives enough headroom that the
  form fits at common Plasma scales without requiring an oversized
  window.

### Fixed — SessionSetupPane label clipping + mic overlap

- **Inner `RowLayout` now uses `anchors.fill: parent`** instead of
  `width: parent.width`. The latter overshoots into the Pane's padding,
  letting children render flush against the window edge. That manifested
  as form labels being left-clipped because the Pane's left padding
  wasn't honored.
- **Form ComboBoxes drop `Layout.fillWidth: true`.** With fillWidth on
  every form child plus aggressive `Layout.preferredWidth: gu*14` /
  `gu*16` hints, controls hogged horizontal space and starved the
  Kirigami.FormLayout label column to ~50 px — labels left-clipped to
  "eech:", "URL:", "odel:". Removing fillWidth lets controls size to
  preferredWidth and gives the label column its natural width.

## 0.8.3 — 2026-04-28

**Filter Kirigami `ToolBarLayout` incubation warnings.** Every launch
was logging ~18 lines of:

```
WARNING: Could not create delegate for ToolBarLayout
WARNING: ...ActionToolBar.qml: Object or context destroyed during incubation
```

Upstream Kirigami chatter — `Kirigami.ApplicationWindow`'s page-header
`ActionToolBar` does async delegate incubation and the layout context
gets re-evaluated mid-incubation during initial paint and at every
responsive-mode transition. The toolbar renders correctly and every
Kirigami app emits these; they're cosmetic noise.

`app.py:_silence_upstream_qml_chatter()` adds
`kf.kirigami.layouts.warning=false` to `QT_LOGGING_RULES` before
`QApplication()` is constructed. Scoped category filter — real Qt /
Kirigami warnings outside this category still surface. Respects any
user-supplied `QT_LOGGING_RULES` so a developer setting them for
diagnostics isn't clobbered.

## 0.8.2 — 2026-04-28

**Visual-smoke framework + compactMode breakpoint fix.** The first
real-world launch of v0.8.0 surfaced a layout regression that the
v0.8.1 QA infrastructure didn't cover: at small-but-not-tiny window
widths (~600 px), `SessionSetupPane.qml`'s FormLayout column squeezed
hard, labels left-clipped to "RL:" / "el:", and the medium-mode mic
frame visually overlapped the dropdown arrows. Two parts:

### Added — visual smoke runner

- **`tests/visual/visual_smoke.py`** + **`voiceagent-visualtest.sh`** —
  headless `QQuickWindow.grabWindow()` capture pipeline. Renders
  `MainWindow` against `QT_QPA_PLATFORM=offscreen` at multiple
  combinations of `QT_SCALE_FACTOR` (1.0x / 1.25x / 1.5x — Plasma's
  common HiDPI settings) and logical width (400 / 600 / 800 / 1000 /
  1200 px), saving 15 PNGs under `screenshots/` (gitignored). Closes
  the gap between "QML loads" (compiletest) / "QML behaves"
  (qatest) / "QML LOOKS right" (visualtest). Layout regressions that
  only appear at specific scale × width combinations are now
  diff-detectable.
- Visual-test gate documented alongside the other test gates.

### Changed — compactMode breakpoint

- **`MainWindow.qml` `compactMode` threshold raised 25 → 35
  `gridUnit`s.** Below ~35 grid units there isn't enough horizontal
  room for the side-by-side form + mic frame without column squeeze;
  the visual smoke run confirmed the regression at 600 px logical
  width (1.0x), and the fix lands cleanly across 1.0x / 1.25x / 1.5x
  scales because `gridUnit` is itself scale-aware. Below the
  threshold the existing compactMode layout (mic-only, conversation
  pane full-width) takes over, which is already known good.

### Fixed (companion to 0.8.0)

- **FormLayout label crowding (PR companion to v0.8.0 KDE polish).**
  `SessionSetupPane.qml` previously concatenated `i18n("Speech:") + " " + voiceAgent.modelStatus`
  into the FormData label, which blew out the right-aligned label
  column to the width of the longest dynamic status string. Trimmed
  to static labels — the dynamic status is already surfaced via the
  progress label area below the form. Bumped the form/mic gap from
  `mediumSpacing` to `largeSpacing`.
- **Bare entry point's missing QML import path.** `.venv/bin/voiceagent`
  failed to load `org.kde.kirigami` because no wrapper had set
  `QML_IMPORT_PATH` (only `voiceagent-buildtest.sh` / -compiletest /
  -qatest / -visualtest do that). `app.py:_ensure_qml_import_path()`
  auto-prepends the standard Qt 6 QML directories
  (`/usr/lib/qt6/qml`) at startup if missing, so both the bare
  entry point and the wrapper scripts work.
- **Stale runtime version string.** v0.8.0's PR #20 bumped
  `pyproject.toml` and `PKGBUILD` but missed `src/voiceagent/__init__.py`,
  so app startup was logging "Voice Agent 0.7.0" against a v0.8.0
  install. Mirrored.

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
