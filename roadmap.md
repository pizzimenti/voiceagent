# Roadmap

Requested but unscheduled features. Items move from here into a versioned
release once they're picked up.

## Internet access via MCP (web search and beyond)

**Status:** requested, unscheduled.

**Detailed plan:** kept out of repo until the work is picked up; populate when it lands.

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
