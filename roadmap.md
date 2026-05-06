# Roadmap

Scheduled work, in order. Each entry maps to a target minor version.
Items move out of here into `CHANGELOG.md` once they ship.

## v0.12 — Kokoro TTS engine (second backend)

**Status:** next up.

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
Engine comparison artifact:
`/home/bradley/.claude/plans/give-me-a-table-squishy-frog.md`.

### Outcome

User opens Settings → TTS engine, picks "Kokoro", picks a Kokoro voice
from the catalog dropdown, and the next assistant turn speaks in a
markedly more natural voice. Piper users see no behaviour change —
default stays `piper` until Kokoro's multilingual catalog is broad
enough to flip the default.

### Scope

One PR. Four surfaces:

**1. New `services/kokoro_tts.py:KokoroTtsService`.** Implements the
existing `TextToSpeechBackend` Protocol from `backends.py:42`. Mirrors
`PiperTtsService`'s download / verify / synthesize lifecycle. Loads
the Kokoro `.onnx` + voice-pack file(s) from
`$VOICEAGENT_TTS_MODEL_ROOT/kokoro/` (subfolder per engine so Piper
and Kokoro coexist on disk; existing Piper voices in `tts-models/`
root continue to resolve via backwards-compatible lookup, no flag-day
migration). Synthesizes to a temp WAV the same way Piper does —
pipeline-side code in `controller.py:300` doesn't change. Reuse
`AriaDownloader` for segmented downloads. Reuse the v0.8.x install
hardening (`.aria2` sidecar trap, parallel-install guard, sha pin) —
those live at the downloader / catalog layer, not inside the Piper
service, so the new service inherits them for free.

**2. `AppConfig.tts_engine`** in `src/voiceagent/config.py`. New
`Literal["piper", "kokoro"]` field, default `"piper"`. Persisted in
QSettings the same way other engine choices are. Read at `app.py`
construction time to wire the right service into `VoiceController`
(`controller.py:76` already takes a `TextToSpeechBackend` Protocol —
no controller changes needed).

**3. UI: engine selector + per-engine catalog.** New TTS-engine
dropdown in the model selector area, alongside the existing voice
dropdown. Switching the engine swaps the voice catalog underneath.
The catalog asymmetry (Piper has 100+ voices, Kokoro ships ~10) is
real — handle it by making the engine selector a top-level choice
and the voice catalog scoped to the selected engine. The
`CatalogStateProvider` per-backend adapter from the v0.10.x cycle
already supports this shape.

**4. Catalog source for Kokoro.** Piper uses
`rhasspy/piper-voices`'s `voices.json` for catalog refresh. Kokoro's
voice pack is shipped from a HuggingFace repo (likely
`hexgrad/Kokoro-82M` — confirm the canonical repo and packaging
when implementation starts; upstream layout has changed across
1.x releases). Catalog refresh fetches the upstream manifest and
pins SHAs the same way the Piper path does.

### Tests

- `tests/test_kokoro_tts.py` — synthesize → WAV roundtrip on a
  fixture voice; verify the file is a valid WAV with non-zero
  samples and the expected sample rate.
- `tests/test_kokoro_tts.py` — download + verify path with an
  injected fake aria2 stub, mirroring `tests/test_tts.py` patterns.
- `tests/test_config.py` — `tts_engine` field round-trips through
  QSettings and defaults to `"piper"` for users upgrading.
- `tests/test_app.py` (or wherever wiring is tested) — engine
  selection wires the right service into `VoiceController`.
- `tests/test_catalog_state.py` — switching engine in the UI swaps
  the voice catalog without leaking entries from the other engine.

### Streaming TTS — out of scope

Sentence-streamed playback (speech starts mid-LLM-response) is a
separate effort orthogonal to the engine swap. Bolting both into
one PR doubles risk for no sequencing benefit. Track it as a
v0.12.x or post-v0.14 follow-up. Kokoro chunk-streams natively, so
when the streaming overhaul does happen Kokoro inherits it cleanly.

### Open questions and risks

- **espeak-ng dependency.** Kokoro uses espeak-ng for phonemization.
  espeak-ng is on most desktop Linux installs but isn't a hard dep
  of voiceagent today. PKGBUILD + README install notes need to add
  it. Confirm exact phonemization integration when implementation
  starts; some Kokoro distributions bundle a Misaki-based phonemizer
  instead.
- **Voice catalog UX asymmetry.** Piper's 100+ voices and Kokoro's
  ~10 don't share a sensible single dropdown. Engine-selector-first
  is the proposed shape — verify in implementation that it doesn't
  regress the Piper user's existing flow.
- **Multilingual coverage gap.** Kokoro is English-strongest; JP /
  ZH support landed mid-2025 but the catalog beyond that is thin.
  Default stays `piper` for non-English users until Kokoro's catalog
  closes the gap. Document explicitly in the README.
- **First-run download size.** Kokoro's model is ~330 MB vs. Piper's
  20-60 MB per voice. Surface this clearly in the engine-selector UI
  so users know what they're committing to.
- **Disk layout migration.** Existing Piper voices in `tts-models/`
  root (not `tts-models/piper/`) need backwards-compatible lookup,
  not in-place migration. Avoid a flag day; leave user files alone.
- **Voice-name collisions.** Engine-scoped catalog avoids ambiguity
  at the UI layer. Keep storage paths engine-scoped too
  (`tts-models/kokoro/...`, `tts-models/<piper-voice>.onnx`) so file
  lookups can't cross.

## v0.13 — Chatterbox Turbo TTS engine (third backend, optional)

**Status:** scheduled after v0.12. The pluggable layer landed in
v0.12 is what makes this cheap.

### Why

Kokoro covers the "better default voice" gap. Chatterbox Turbo
(Resemble AI, mid-2025) covers the next axis: **voice cloning and
expressive prosody**. Permissive license (base Chatterbox is MIT),
voice cloning from a short reference clip, emotion / expressivity
controls. "Turbo" is Resemble's latency-optimized variant designed
to bring per-token cost down enough for interactive use. **Confirm
the exact model name, license, and footprint when implementation
starts** — these are 2025-current details and worth re-verifying
before work begins.

This is not a default-engine candidate. PyTorch runtime weight and
larger model size make it heavier than Kokoro. It's the "if you want
expressive voices or to clone your own" tier.

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

## v0.14 — Internet access via MCP (web search and beyond)

**Status:** scheduled after v0.13.

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
