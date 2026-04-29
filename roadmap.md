# Roadmap

Scheduled work, in order. Each entry maps to a target minor version.
Items move out of here into `CHANGELOG.md` once they ship.

## v0.11 — Conversation history (multi-turn context)

**Status:** next up.

### Why

Voiceagent today is single-turn. `LmStudioClient.complete()` rebuilds
`messages` from scratch on every call:

```python
"messages": [
    {"role": "system", "content": self.system_prompt},
    {"role": "user", "content": user_text},
],
```

(`src/voiceagent/services/chat.py:259-262`.) Nothing above the HTTP
layer accumulates prior turns either — `controller.py:237` passes only
the latest transcript, and `ConversationModel` exists for the UI but is
not fed back into the prompt. Result: the model sees each turn as a
fresh conversation, can't follow up on its own previous answer, can't
resolve pronouns ("what about *that* one?"), and can't carry any
working memory across turns.

LM Studio's chat mode appears to "have memory" because its frontend
replays the full `messages` thread on every call. The
OpenAI-compatible `/chat/completions` endpoint is stateless — history
is the **caller's** responsibility. We need to do what LM Studio's UI
already does.

### Outcome

User says "what's the capital of France?" → "Paris." → "what's the
population?" — the second turn resolves correctly because the model
sees both prior turns. (The original v0.11 plan also clear-wiped on
model swap; that was retired during the PR after user feedback —
modern instruction-tuned models handle each other's transcripts
fine, and the surprise wipe was the bigger UX cost. See the v0.11.0
CHANGELOG entry "Design choice — conversation persists across model
swaps" for the final shipped behavior.)

### Scope

One PR. Three surfaces:

**1. `services/chat.py:LmStudioClient.complete()`** — change signature
to accept `messages: list[dict]` instead of `user_text: str`. Caller
owns the full message list; the client just posts it. System prompt
moves out of the client (or stays as a default the caller can
override) — the client should not silently inject anything the caller
didn't ask for. Streaming, usage callback, thinking-channel routing
all unchanged.

**2. `conversation_model.py:ConversationModel`** — add a
`to_openai_messages(system_prompt: str) -> list[dict]` method that
serializes the visible conversation to the OpenAI message format:
`[{role: "system", content: <prompt>}, {role: "user", content: <t1>},
{role: "assistant", content: <r1>}, ...]`. Skip thinking-channel
content (it's not part of the assistant's *output* turn — feeding
`reasoning_content` back as assistant content would confuse the model
and waste tokens). Add a `clear()` method if one isn't already there,
wired to the New Conversation action.

**3. `controller.py:_run_pipeline()`** — append the new user turn to
the conversation, call `to_openai_messages()`, pass that to
`complete()`, append the assistant response. Order matters: append
user *before* the LLM call (so it's visible immediately and included
in the prompt), append assistant *after* the call returns.

### History-budget policy

Unbounded history will eventually exceed the loaded model's context
window. v0.11 ships with a **simple turn-count cap** — keep the last
`N` turns (default 20, i.e. 10 user + 10 assistant pairs), drop the
oldest first, system prompt always retained. Configurable via
`AppConfig.max_history_turns`.

`fetch_loaded_context_length()` already exists at
`services/chat.py:199`. A token-aware trim (count prompt tokens, drop
oldest pairs until prompt+headroom fits) is the natural v0.11.x
follow-up but **not** v0.11 — turn-count is good enough to unblock the
multi-turn experience and ship.

### Clear-on-model-switch — DROPPED (see CHANGELOG v0.11.0)

The original plan called for clearing history on every model swap.
Retired during the PR after user testing: continuity wins, and modern
instruction-tuned local models handle each other's transcripts well
enough that a surprise wipe was the worse UX. The
context-token bar still resets on swap and the new model's
`loaded_context_length` is re-fetched, so the visual warning stays
accurate under the new ceiling. If a swap to a smaller-context model
exceeds the new ceiling mid-session, LM Studio truncates from the
front; the v0.11.x token-aware-trim follow-up handles this
automatically.

### Tests

- `tests/test_chat.py` — `complete()` posts the exact `messages`
  list it was given, no injection, no reordering.
- `tests/test_conversation_model.py` —
  `to_openai_messages()` round-trips a multi-turn convo correctly,
  skips thinking content, respects the `max_history_turns` cap.
- `tests/test_controller.py` — pipeline appends user-then-assistant
  in the right order. (Model-change-clears-history was dropped per
  above.)

### Open questions and risks

- **First-turn token cost is unchanged; later-turn costs grow.** A
  10-turn convo with verbose answers can easily hit 4–8k prompt
  tokens. The existing context-token bar (`v0.10.0`) already shows
  this, so the user gets a visual cue before the model starts
  truncating.
- **Whisper transcripts are noisier than typed input.** Fillers,
  half-words, and STT artefacts get permanently baked into history.
  Acceptable for v0.11; a "edit / retry last turn" affordance is a
  later UX improvement, not a blocker.
- **Thinking content exclusion.** Confirmed above — assistant
  history carries `content` only, not `reasoning_content`.

## v0.12 — Internet access via MCP (web search and beyond)

**Status:** scheduled after v0.11.

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

### Dependency on v0.11

v0.12 *requires* v0.11 to be in. The MCP agent loop assumes the caller
owns a running `messages` list (so it can append tool-call /
tool-result messages between iterations). v0.11 is what gives us that
list in the first place — without it, every MCP iteration would lose
the prior conversation and the agent loop would be a single-turn-only
feature, defeating the point.

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
  `~/.local/state/voiceagent/logs/conversation.log*` if the content
  is sensitive.

### Other v1.0 items

(Reserved — add as the v0.11.x → v0.12 cycle surfaces other
"good for a release-candidate" cleanups.)
