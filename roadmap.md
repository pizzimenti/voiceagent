# Roadmap

Scheduled work, in order. Each entry maps to a target minor version.
Items move out of here into `CHANGELOG.md` once they ship.

## v0.12.0 — Chatterbox TTS engine + live engine selector ✅ SHIPPED 2026-05-10

The original v0.12 plan was Kokoro, but upstream `kokoro-onnx` /
`misaki` pinned `<3.14` Python while Manjaro / Arch defaulted to
3.14, blocking the venv. Chatterbox (Resemble AI's zero-shot voice
cloning model, pure ONNX runtime, no PyTorch dep) had no such
constraint and was sequenced ahead — see CHANGELOG for the full
shipped scope: live engine selector, mic-record + file-import
voices, per-dtype model variants, offline-readiness via pinned HF
revision + install manifest, engine-swap safety guarantees.

Released as v0.12.0 (commit `4dcdf74`, tag `v0.12.0`).

## v0.13.0 — Remove v0.11.x migration helper ✅ SHIPPED 2026-05-13

Cleanup-only release. Removed `migrate_legacy_data_dirs()` and its
startup hook now that the single-user/single-machine app has fully
moved to the engine-scoped tree introduced in 0.12.0. The migration
helper had begun emitting a spurious "Skipping legacy data migration"
warning on every launch.

## v0.14 — Kokoro TTS engine ✅ SHIPPED 2026-06-07

Third TTS engine — Kokoro (hexgrad/kokoro-onnx, ~82M-param neural
model, Apache 2.0, pure ONNX, no PyTorch). Selectable in the runtime
engine selector alongside Piper (default) and Chatterbox. See
CHANGELOG for the full shipped scope. Released as v0.14.0.

**The Python 3.14 blocker turned out to be a paper cap.** The reason
Kokoro was deferred behind Chatterbox in v0.12 — `kokoro-onnx`
(`requires-python <3.14`) and `misaki` (`<3.13`) — was verified
(2026-06-06 spike) to be conservative metadata only: `kokoro-onnx`
0.5.0 ships native cp314 wheels (numpy, onnxruntime), imports, and
synthesizes correctly on 3.14.5. Shipped by force-installing with
`pip install --ignore-requires-python voiceagent[kokoro]`. `misaki`
(the higher-quality JP/ZH g2p, still `<3.13`) is NOT used — Kokoro
phonemizes via `phonemizer-fork` (espeak-ng, bundled through
`espeakng-loader`, no system package needed).

Notable deviations from the original plan (preserved below the line):

- **Single-bundle, not per-voice.** The Kokoro v1.0 release ships ONE
  bundle (`kokoro-v1.0.onnx` + `voices-v1.0.bin`) holding all 54
  voices, not a per-voice catalog like Piper. So the config pane is a
  straight mirror of the Piper pane (filter + CatalogList): the
  catalog lists all 54 up front, installing any one fetches the shared
  bundle, and `is_engine_ready` gates Talk until the bundle is on disk
  (same shape as Chatterbox's shared-model readiness). No per-voice
  download granularity, no `voices.json`-style catalog refresh.
- **54 voices, not ~10.** Nine languages (en-US/GB, es, fr, hi, it,
  ja, pt-BR, zh). Each voice's prefix selects its espeak language; all
  54 synthesize, English-strongest. Default stays Piper.
- **No `soundfile` dep.** WAV is written with the stdlib `wave` module,
  mirroring Piper.
- **Engine-scoped root** `<data>/tts/kokoro/model/`, isolated from
  Piper — no flag-day migration, matching the Chatterbox layout.

<details>
<summary>Original v0.14 plan (pre-implementation) — historical</summary>

### Why

Voiceagent ships with Piper today (`PiperTtsService`,
`src/voiceagent/services/tts.py:24`). Piper is fast and ARM-friendly
but its formant-coloured neural voices age poorly next to engines
released in 2024-2025. The `TextToSpeechBackend` Protocol in
`src/voiceagent/backends.py:42` already makes the surface pluggable —
adding a second engine is new code, not a refactor.

Kokoro (hexgrad/kokoro-onnx, 1.0 in early 2025) is the right second
backend: ~82M-parameter neural model, Apache 2.0 license, ONNX runtime
(no PyTorch dependency), runs at roughly realtime on a modern desktop
CPU, ~330 MB single-file model. Quality is dramatically above Piper
while keeping the local-only / no-GPU posture voiceagent depends on.

### Open questions and risks (as resolved)

- **espeak-ng dependency.** Resolved: bundled via `espeakng-loader`,
  no system package or PKGBUILD dep needed.
- **Voice catalog UX asymmetry.** Resolved: single-bundle means the
  Piper-style pane works unchanged; all 54 voices listed up front.
- **Multilingual coverage gap.** Kokoro is English-strongest; JP / ZH
  via espeak are functional but coarse (misaki unavailable on 3.14).
  Default stays `piper`.
- **First-run download size.** ~350 MB bundle, surfaced via an inline
  note in the config pane.
- **Disk layout.** Engine-scoped root, backwards-compatible — Piper
  voices untouched.

### Streaming TTS — still out of scope

Sentence-streamed playback (speech starts mid-LLM-response) remains a
separate effort. Kokoro chunk-streams natively, so when the streaming
overhaul happens Kokoro inherits it cleanly. Track post-v0.14.

</details>

## ~~v0.13 — Chatterbox Turbo TTS engine~~ — superseded by v0.12.0

The Chatterbox engine work originally scheduled here landed early as
v0.12.0 once upstream Kokoro got blocked. Notable deviations from
this plan:

- The shipped engine targets `ResembleAI/chatterbox-turbo-ONNX`
  (pure ONNX, no PyTorch dep) — the PyTorch-runtime concern
  documented below turned out to be moot.
- Voice-cloning UX shipped as mic-record + file-import (no
  expressivity sliders yet — upstream API doesn't expose them).
- Optional-extras packaging (`pip install voiceagent[chatterbox]`)
  did ship as planned.

The original v0.13 outline preserved below for historical reference.

### Why (historical)

### Outcome

User picks "Chatterbox Turbo" in the engine selector, optionally
provides a 6-30 s reference clip in Settings, and the next turn
speaks in either the cloned voice or a chosen built-in expressive
voice. Emotion / pace / expressivity sliders exposed in Settings if
the upstream API supports them.

### Scope

One PR (or two if voice cloning grows enough UI to deserve its own).
Surfaces:

**1. New `services/chatterbox_tts.py:ChatterboxTtsService`.**
Implements `TextToSpeechBackend`. Loads the Chatterbox Turbo model
from `$VOICEAGENT_TTS_MODEL_ROOT/chatterbox/`. Synthesizes to a temp
WAV like the others.

**2. `AppConfig.tts_engine`** extended to
`Literal["piper", "kokoro", "chatterbox"]`.

**3. Voice-cloning UX.** New Settings section: "Reference voice"
file picker with validation (length 6-30 s, mono / 16 kHz preferred,
clear-format error otherwise), persisted at
`$XDG_DATA_HOME/voiceagent/chatterbox-references/` so it survives
across sessions. The reference clip *is* the "voice" in the catalog
sense — the existing voice-dropdown becomes "reference clip" when
Chatterbox is selected.

**4. Expressivity controls (if supported).** Sliders for emotion /
pace / exaggeration if Chatterbox Turbo's API exposes them. Skip if
the API does not — don't fake controls that aren't real.

**5. Optional-extras packaging.** PyTorch is heavy (~1 GB). Gate
Chatterbox behind `pip install voiceagent[chatterbox]` and an
equivalent flag in PKGBUILD `optdepends`. Piper / Kokoro users
should not pay the dependency cost just because Chatterbox is
listed in the engine selector — the engine entry only enables when
the optional deps are present, and the UI shows an "install
chatterbox extras" prompt otherwise.

### Tests

- `tests/test_chatterbox_tts.py` — synthesize roundtrip with a
  fixture reference clip.
- Reference-clip validation: too short, too long, wrong format —
  all produce clear errors and don't crash the pipeline.
- Engine-selection tests extended to cover the third value.
- Optional-deps gating: with deps absent, engine entry is disabled
  and shows install prompt; with deps present, engine works.

### Open questions and risks

- **License re-verification.** Confirm Chatterbox Turbo's license
  is permissive (Apache / MIT) and doesn't carry NC restrictions
  before starting work. Base Chatterbox is MIT; "Turbo" may have
  its own terms. **Hard blocker if the license is non-commercial
  or attribution-restrictive — pivot to a different cloning engine
  in that case (re-evaluate XTTS successors if Coqui's lineage
  lands with permissive terms).**
- **PyTorch dependency.** ~1 GB of installed deps. Gated behind
  optional-extras packaging as above.
- **Model size.** ~500 MB-1 GB depending on packaging — surface in
  the engine-selector UI same as Kokoro.
- **Reference-clip privacy.** Cloned voices are sensitive — store
  reference clips locally only, never log them, document in the
  README that they don't leave the machine.
- **Quality vs. CPU speed tradeoff.** Chatterbox Turbo is the faster
  variant precisely because expressivity and quality are slightly
  reduced. If users complain about latency the non-Turbo variant is
  the next dial — Turbo is the right v1 default for this engine
  slot.
- **Streaming inheritance.** If the streaming-TTS overhaul has
  shipped by v0.13, verify Chatterbox's streaming path works the
  same way. If not, both can stream once the overhaul lands.

## v0.15 — Internet access via MCP (web search and beyond)

**Status:** scheduled after v0.14 (Kokoro).

**Detailed plan:** working draft below; refined when implementation starts.

### Why

Voiceagent today can't look anything up — `LmStudioClient` sends a single
system + user message to LM Studio's OpenAI-compatible endpoint and streams
the reply. LM Studio itself has no internet access; it's just a local
model server. Internet access has to live in voiceagent and be exposed to
the LLM as **tools the model can call**.

A one-shot `web_search` function would work, but the strategic bet is to
adopt **MCP (Model Context Protocol)** as the tool layer. MCP is becoming
the de-facto standard tool protocol (Anthropic, OpenAI, Microsoft Copilot
Studio), and the server ecosystem is growing fast — search, fetch,
GitHub, calendar, filesystem, code execution, memory, and more. Each new
MCP server added to voiceagent's config gives the agent a new capability
**with zero code changes**. The architecture is also backend-agnostic: it
works with LM Studio today and Ollama / Claude / OpenAI tomorrow.

### Outcome

The user speaks a question → the LLM emits a tool call → voiceagent
dispatches it to a configured MCP server → the result is fed back → the
LLM produces the final spoken answer. Tool calls happen invisibly between
user-visible turns; only the final text streams to the conversation pane
and gets spoken.

### Scope

Two PRs, in order. Each is independently reviewable and testable.

**PR 1 — OpenAI tool-calling support in `LmStudioClient`.** Pure
addition. Add a sibling `complete_with_tools(...)` method that accepts a
full `messages` list plus `tools` / `tool_choice`, returns a small
`CompletionResult` dataclass (`content`, `tool_calls`, `finish_reason`),
and accumulates streaming `tool_calls` deltas across SSE chunks. Existing
`complete()` path stays byte-identical when no tools are configured.

**PR 2 — MCP client + agent loop + bundled web-search config.**

- New `services/mcp_client.py` using the official Python `mcp` SDK
  (`mcp.client.stdio.stdio_client` + `ClientSession`). Spawns each
  configured stdio server, runs `initialize` + `tools/list`, dispatches
  calls, and shuts down cleanly. Asyncio loop runs in a dedicated daemon
  thread so the rest of voiceagent's sync-on-threads pattern is
  preserved.
- Tool-name prefix scheme `{server}__{tool}` to disambiguate across
  servers.
- Agent loop in `VoiceController._run_pipeline()`: call → if
  `tool_calls`, dispatch → append tool-result messages → call again,
  capped at N iterations (default 6). Only the terminal round's content
  streams to the user-visible channel; intermediate-round content
  (rare) routes to the thinking expander.
- Config: extend `AppConfig` with `McpServerConfig` list, read from a
  Claude-Desktop / VS-Code-shaped `mcp.json` at
  `~/.config/voiceagent/mcp.json` by default. API keys come from the
  parent process env via `${VAR}` interpolation — never stored in
  QSettings.
- Bundled example `packaging/mcp.json.example` with **Tavily** as the
  recommended v1 search provider (single API key, LLM-ready ranked
  snippets, generous free tier). The schema is provider-agnostic;
  swapping in Brave Search MCP, self-hosted SearXNG-MCP, or a `fetch`
  server is a config change, not a code change.

### Graceful degradation

- No MCP servers configured → no `tools` field sent, no agent loop,
  behaviour identical to today.
- Model offered tools but emits content-only → treated as a plain
  response, no loop.
- MCP server fails to start → log + non-fatal toast; voiceagent stays
  usable without that capability.
- Tool throws → error string fed back to the LLM, which recovers /
  apologises rather than crashing the pipeline.
- Iteration cap hit → surface to UI via existing `pipeline_failed`
  path.

### Open questions and risks

- **Model coverage on LM Studio.** Tool-calling is per-model.
  Llama 3.1 / 3.3-Instruct, Qwen 2.5, Mistral-Nemo-Instruct, and Hermes-3
  emit `tool_calls` reliably; Gemma and base R1 distills do not. v1
  mitigation: graceful-degradation path above + a one-line warning in
  `LlmController` when the loaded model is on a known-bad list.
- **Token-budget blowup.** A 4-iteration round with verbose search
  results can blow a small model's context window. v1 caps each tool
  result at 4000 chars (truncate with ellipsis). v1.1 could add smarter
  summarisation.
- **Subprocess lifecycle on shutdown.** `MainWindow.closeEvent` must
  call `mcp.aclose()` *before* `QCoreApplication.quit()` or stdio
  servers leak. Use `threading.Event` + 5s join timeout +
  SIGKILL survivors.
- **API-key UX.** v1 documents `TAVILY_API_KEY` env var + `mcp.json`
  location in README. A settings UI for MCP servers is a v1.1 follow-up.
- **Streaming dead-air during tool dispatch.** Buffering content until
  the terminal round means ~1–3 s of UI silence while a search runs.
  Mitigation: emit a `pipeline_state_changed` update (new
  `AppState.TOOL_CALLING` or piggyback on `THINKING`) so the UI shows
  "Searching the web…".

### Future capability multiplier

Once MCP is plumbed, adding more capabilities is config-only:

- `mcp-server-fetch` — fetch arbitrary URLs the user mentions.
- `mcp-server-filesystem` — read / summarise local files.
- `mcp-server-github` — issues, PRs, code search.
- `mcp-server-time`, `mcp-server-memory`, calendar / mail servers, etc.

This is the main reason MCP is the right shape rather than a one-off
`web_search` function.

### Dependency on v0.11 (satisfied)

The MCP agent loop assumes the caller owns a running `messages` list
so it can append tool-call / tool-result messages between iterations.
v0.11 (multi-turn conversation history) is what gives us that list —
without it every MCP iteration would lose the prior conversation and
the agent loop would be a single-turn-only feature, defeating the
point. v0.11 shipped in `63dc1cd`, so this dependency is already
satisfied; v0.14 picks up cleanly when its turn comes.

## v1.0 — Stable-release housekeeping

**Status:** scheduled before any 1.0 tag is cut.

### Conversation log default → OFF

The v0.11 conversation log
(`$XDG_STATE_HOME/voiceagent/logs/conversation.log`) is currently
default-ON for debug convenience: full transcript text + LLM
`messages` payload + assistant responses persist on every turn.
That was the right call for the v0.11 development cycle — being able
to grep `conversation.log` after a multi-turn session is exactly
what the file is for. CodeRabbit (PR #44 review, round 3) flagged
the privacy posture: persisting raw user content by default runs
counter to privacy-by-default principles, even on a local-only
single-user tool, and is the kind of thing a security audit would
ding before a stable release.

**v1.0 work:**

- Flip the conversation log default to **OFF**. Opt in with an
  explicit env flag (e.g. `VOICEAGENT_CONVERSATION_LOG=1`).
- Document the flag in `README.md` § Configuration.
- The session-rotation machinery + per-turn content capture stay
  exactly as-is — only the install gate in
  `logging_utils._install_conversation_logger` changes from "always
  install" to "install iff env flag set".
- One small migration concern: any existing log files from v0.11
  remain on disk; they don't auto-clear. Document in the v1.0
  CHANGELOG that users may want to remove
  `${XDG_STATE_HOME:-$HOME/.local/state}/voiceagent/logs/conversation.log*`
  if the content is sensitive.

### Other v1.0 items

(Reserved — add as the v0.12 → v0.14 cycle surfaces other
"good for a release-candidate" cleanups.)

### Borrow Story Viber's LLM call-logger pattern

Source: 2026-05-22 head-to-head comparison between Voice Agent and
Story Viber (`~/Code/Story_Viber`). Voice Agent's `LmStudioClient` is
ahead on connection management (model load/unload, context-window
discovery, the loaded-instance bug workaround), but Story Viber is
ahead on observability. Worth borrowing the latter back in.

Story Viber's pattern, at `src/story_viber/observability/`:

- **Typed events** (Pydantic): `RequestEvent`, `ChunkEvent`,
  `ResponseEvent`, `ProviderErrorEvent`, `CancelledEvent`,
  `GraderResultEvent`. Every event carries a required, non-empty
  `call_id` (enforced via `field_validator`). A discriminator field
  (`event: Literal["request"]`, etc.) + an `EVENT_REGISTRY` make
  JSONL post-mortems trivial: `jq 'select(.event=="response") | ...'`.
- **`CallContext`** — an `async with logger.call(call_id=...)`
  context manager that **guarantees exactly one terminal event** per
  call (response / error / cancelled). If the caller forgets, the
  context manager auto-emits on exit. `asyncio.CancelledError`
  produces a `CancelledEvent`; any other exception produces a
  `ProviderErrorEvent` and re-raises.
- **`LLMCallLogger`** — JSONL writer. Filesystem failure at
  construction OR mid-session degrades to no-op silently with a
  stdlib warning. Logging never aborts the host application — same
  diagnostic-only contract Voice Agent already implicitly follows.
- **`NullLogger`** — explicit no-op sibling, returned when logging
  is disabled. Caller code doesn't need to special-case "no logger".

**Why Voice Agent would benefit:**

1. Per-call traceability across `request → chunks → response` is
   currently impossible — `chat.py` uses stdlib `logging.warning()`
   with plain-text messages. Diagnosing a hung stream or partial
   completion means scrolling stderr.
2. Voice Agent already captures reasoning_content via a callback;
   the typed `ResponseEvent` would also persist it alongside content
   in the log, so post-hoc analysis ("why did the model think for 4
   seconds before answering?") works without re-running.
3. CodeRabbit's "raw user content in logs" privacy concern (see
   v1.0 conversation-log work above) becomes easier to reason about
   with typed events: a future "redact messages on write" flag flips
   only `RequestEvent.messages` to a hash, leaving timings and
   call_ids intact for debugging.

**What to actually do** (estimate: half a day):

1. Vendor the three files into `src/voiceagent/services/observability/`
   (or borrow as-is — they're MIT under Story Viber, same as us).
2. Wrap `LmStudioClient.complete()` in a `CallContext` and emit
   `RequestEvent` before opening the stream + `ChunkEvent` per
   `delta.content`/`delta.reasoning_content` + `ResponseEvent` on
   normal completion.
3. Wire the logger into `LlmController` so the JSONL path is
   configurable from `AppConfig` (default off — opt-in, same shape
   as the conversation-log flip planned for v1.0).
4. Don't replace the existing stdlib-`logging.warning()` calls;
   layer JSONL alongside, since those warnings serve a different
   audience (the developer reading journalctl).

What we **don't** borrow:

- Story Viber's `chunk_timeout` is a single knob; our two-knob model
  (`timeout_seconds` for chat + `load_timeout_seconds` for model
  warmup) is the right shape for our needs.
- Story Viber's pure-asyncio cancellation story is irrelevant —
  Voice Agent's ThreadPoolExecutor model can't use it.
- Story Viber's prose-only contract isn't applicable; Voice Agent
  has a real chat-history loop that needs the full assistant text
  in the return value, which it already has.

Track via a `feat/observability-jsonl` branch when we get to v1.0.

