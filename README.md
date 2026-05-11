# voiceagent

KDE-friendly desktop voice assistant for local speech workflows.

## Stack

- `PySide6` for the desktop UI and audio playback
- `sounddevice` for microphone capture
- `faster-whisper` for local transcription
- `piper-tts` for local speech synthesis
- `aria2c` for segmented model downloads
- LM Studio's OpenAI-compatible local API for chat

## Setup

Use a virtual environment so the app dependencies stay isolated from the system Python:

```bash
sudo apt install aria2
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -e .
```

## Configuration

Environment variables:

- `LM_STUDIO_BASE_URL` default: `http://127.0.0.1:1234/v1`
- `LM_STUDIO_MODEL` required for chat
- `LM_STUDIO_SYSTEM_PROMPT` optional system prompt
- `WHISPER_MODEL` default: `large-v3`
- `WHISPER_DEVICE` default: `auto`
- `WHISPER_COMPUTE_TYPE` default: `auto`
- `VOICEAGENT_STT_MODEL_ROOT` default: `$XDG_DATA_HOME/voiceagent/stt/whisper` or `~/.local/share/voiceagent/stt/whisper`
- `VOICEAGENT_TTS_MODEL_ROOT` default: `$XDG_DATA_HOME/voiceagent/tts/piper` or `~/.local/share/voiceagent/tts/piper`
- `TTS_MODEL` optional Piper voice name like `en_US-lessac-medium` or a path to a Piper model file
- `TTS_EXTRA_ARGS` optional extra command-line flags for TTS
- `VOICEAGENT_TTS_ENGINE` default: `piper` — selects the TTS engine. Set to `chatterbox` to use Resemble AI's zero-shot voice cloning model. See [TTS engines](#tts-engines) below.
- `VOICEAGENT_CHATTERBOX_REFERENCES_ROOT` default: `~/.local/share/voiceagent/tts/chatterbox/references` — directory holding user-recorded reference voice clips (`*.wav`) for the Chatterbox engine.
- `VOICEAGENT_MAX_HISTORY_TURNS` default: `20` — how many user/assistant entries (≈ 10 pairs) the conversation pane keeps. See [Conversation memory](#conversation-memory) below.

Whisper downloads, Hugging Face cache data, and Piper voices are stored under the app's XDG data directory by default. Logs are stored under `$XDG_STATE_HOME/voiceagent/logs` or `~/.local/state/voiceagent/logs`:

- `voiceagent.log` — main app log (Qt warnings, pipeline lifecycle, audio device events). Size-rotated: 1 MB × 4 files = 4 MB cap.
- `conversation.log` — per-turn content shipped to / received from the LLM (full `messages` list, assistant response, token usage, model swaps, history trims). **Session-rotated**: each launch shifts the prior file to `.1`, drops the oldest beyond `.5`. Useful when debugging multi-turn behaviour ("what context did the model actually see for that turn?").

Model downloads use `aria2c` with 10 parallel connections by default, and the app shows live progress and transfer speed while Whisper is loading.

## TTS engines

Voiceagent ships two TTS engines that coexist behind a runtime engine
selector in the main window:

- **Piper** (default) — fast, deterministic, multilingual catalog of
  pretrained voices. No optional install needed; bundled in the base
  package.
- **Chatterbox** (optional) — Resemble AI's zero-shot voice-cloning
  model. Voice-cloning only — there are no built-in voices; the user
  supplies a 6–10 second reference clip per voice and Chatterbox
  synthesizes new speech in that voice.

### Installing Chatterbox

```bash
pip install voiceagent[chatterbox]
```

That extra pulls in `onnxruntime`, `transformers`, `librosa`, and
`soundfile`. The engine targets the `q4`-quantized variant of
`ResembleAI/chatterbox-turbo-ONNX` (pure ONNX, no PyTorch). The first
synthesis triggers a one-time ~700 MB model download under
`~/.local/share/voiceagent/tts/chatterbox/model/`.

If you select the Chatterbox engine without those extras installed,
voiceagent logs a warning and falls back to Piper rather than failing
on first synth.

### Switching engines

The TTS engine selector lives in the main window header; switching
takes effect immediately, without restarting voiceagent. Per-engine
voice selection is remembered: switching from Piper → Chatterbox →
Piper restores your prior Piper voice, and likewise for Chatterbox
reference clips.

### Chatterbox first run

The first time you switch to Chatterbox, voiceagent prompts you to
provide a reference voice. You can:

1. **Record** — speak into the microphone for 6–10 seconds.
2. **Import** — pick an existing `.wav` (or any format `librosa`
   reads) from disk.
3. **Use bundled default** — fall back to a generic reference clip
   shipped with the package.

Reference clips are stored under
`$VOICEAGENT_CHATTERBOX_REFERENCES_ROOT` (default
`~/.local/share/voiceagent/tts/chatterbox/references/`) as `*.wav`
files, one per cloned voice. Manage them via Settings → Voice →
Manage reference voices.

### Performance

- **Output**: 24 kHz mono WAV.
- **Realtime factor**: ~1.35× on a Ryzen 5 8640HS / Intel-i5-class
  CPU with the `q4` variant. (Synthesizing a 6 s utterance takes
  about 8 s wall time.)
- **GPU**: Not currently available on AMD integrated GPUs (Phoenix /
  RDNA3 iGPU) — ONNX Runtime has no Vulkan execution provider, so
  CPU inference is the only path on those machines today.

## Conversation memory

When voiceagent talks to LM Studio, it speaks the OpenAI
`/chat/completions` HTTP protocol. That endpoint is **stateless** —
the server runs the model, returns the answer, and forgets everything
before the next request arrives. Memory of prior turns is the
caller's job: voiceagent has to re-send the entire conversation on
every new turn, or the model sees each question as a brand-new chat.

### What the conversation pane is, mechanically

The chat pane in the window IS voiceagent's conversation memory.
Every finalized user/assistant bubble you see there is also a row in
the `messages` array that gets sent to LM Studio on the next turn.
The serialized payload looks like:

```json
[
  {"role": "system",    "content": "<your LM_STUDIO_SYSTEM_PROMPT>"},
  {"role": "user",      "content": "what's the capital of France?"},
  {"role": "assistant", "content": "Paris."},
  {"role": "user",      "content": "what's the population?"}
]
```

That's 4 messages → the model reads all four, then writes the next
assistant turn. "what's the population?" resolves correctly because
the model can see the prior pair. There is no other "memory store"
behind the scenes — the chat pane is the source of truth.

### The cap, and why the pane trims itself

Every model has a hard limit on how many *tokens* (chunks of ~0.75
words each) it can read in a single call — its **context window**. A
7B model with an 8k window starts struggling around ~6,000 tokens of
serialized transcript. Long conversations would eventually exceed
that ceiling and either error or get silently truncated by LM Studio.

`VOICEAGENT_MAX_HISTORY_TURNS` (default 20 = last 10 user/assistant
pairs, system prompt always retained) caps how many entries
voiceagent serializes. **The cap also trims the visible pane**: when
a new pair lands and the conversation has more entries than the cap,
the oldest pair (and any per-turn status breadcrumbs that belong to
it) disappears from the chat pane at the same moment it leaves the
LLM payload. The invariant is *what you see is what the model
sees on the next turn* — no phantom history, no rows in the pane
that the model has already lost track of.

If a conversation that has been trimmed needs to recall something
that scrolled off the top, the model will not be able to: that
information is gone from both the pane and the prompt.

### What changes when you swap models

Switching the loaded LLM mid-session does **not** wipe the
conversation. The transcript carries forward; you can ask the same
follow-up to two models and compare. Modern instruction-tuned local
models handle each other's transcripts well in practice.

What does reset on a swap is the per-model context-token bar at the
bottom of the window — voiceagent re-fetches the new model's
`loaded_context_length` and the bar starts fresh against that
ceiling.

### Tuning

- Lower the cap (`VOICEAGENT_MAX_HISTORY_TURNS=10`) if Whisper
  artefacts (filler words, hallucinated phrases during silence)
  start polluting recall, or if you're running a small-context
  model where every token matters.
- Raise the cap (`VOICEAGENT_MAX_HISTORY_TURNS=40`) on a
  large-context model if you want longer recall and don't mind the
  larger prompt size and slower per-turn latency.
- `0` means unbounded — the pane and payload grow without limit.
  LM Studio will eventually truncate from the front when the
  context window fills; the visual ceiling bar will turn red as
  you approach the limit.
- A token-aware trim (count actual prompt tokens against the
  loaded model's context window, drop oldest pairs until the
  request fits) is on the v0.11.x roadmap. For now the simple
  turn-count cap is the knob.

## Run

```bash
source .venv/bin/activate
voiceagent
```

The app assumes LM Studio's local server is already running.

For a quick non-packaging smoke check of the desktop shell:

```bash
./voiceagent-compiletest.sh
```

That verifies the key Python entrypoints compile and that the Kirigami QML window loads offscreen.

## Tests

Run the test suite from the project root:

```bash
QT_QPA_PLATFORM=offscreen ./.venv/bin/python -m pytest -q tests/
```

If the venv is already active, `pytest -q tests/` is enough — `tests/conftest.py` sets `QT_QPA_PLATFORM=offscreen` and adds `src/` to `sys.path` automatically.

## Arch / Manjaro Packaging

The repo includes two Arch packaging paths:

- `PKGBUILD` builds directly from the current checkout, which is useful for local `makepkg -si` installs while iterating on the app.
- `packaging/PKGBUILD.aur` is the publication-oriented template for AUR releases from tagged source tarballs.

Both package variants install the `voiceagent` launcher and a desktop entry. Runtime data stays in the current user's XDG data and state directories.

Build it locally with:

```bash
makepkg -si
```

For the local checkout `PKGBUILD`, `makepkg -si` installs the pacman package and pulls in only official repository packages such as `python`, `pyside6`, `aria2`, and `portaudio`. The Python speech stack is bundled into the package itself during the build, so end users do not need AUR helpers or a separate Python environment.

First run is reserved for user data only: downloading Whisper models, Piper voices, and writing local config/state under XDG directories.

Current Arch package expectations:

- `pyside6`
- `aria2`
- `portaudio`

The application is packaged as a normal pacman package, but it vendors the Python speech/runtime dependencies inside `/usr/lib/voiceagent/vendor` so the installed app does not depend on AUR Python packages or an app-managed venv.

When additional STT or TTS backends are introduced, prefer this packaging policy:

- keep official repository packages in `depends`
- vendor Python-only backend dependencies inside the package
- keep first-run setup limited to user data and downloadable models
- keep backend imports behind adapter boundaries so large optional engines can still be introduced intentionally

## AUR Release Checklist

Before publishing or updating the AUR package:

1. Create and push a signed or otherwise finalized Git tag such as `v0.3.0`.
2. Confirm the GitHub release tarball for that tag exists and matches the expected source layout.
3. Update `pkgver` in `packaging/PKGBUILD.aur` if needed.
4. Replace `sha256sums=('SKIP')` in `packaging/PKGBUILD.aur` with the real release checksum if you want reproducible source verification.
5. Build the release package locally with `makepkg -f` from an isolated copy of the tagged source.
6. Verify runtime behavior after installation:
   `voiceagent`, desktop entry launch, XDG data paths, model download flow, microphone capture, TTS playback.
7. Regenerate `.SRCINFO` from the AUR package recipe before publishing to the AUR repo.
8. Confirm `packaging/vendor-requirements.txt` is in sync with `pyproject.toml` so the Python speech stack vendored into `/usr/lib/voiceagent/vendor` matches what the wheel expects at runtime.

## Acknowledgements

This project depends on and benefits from a number of upstream projects. Thanks to:

- Qt for the UI toolkit, and the PySide6 maintainers for Python bindings.
- The `sounddevice` and PortAudio projects for microphone capture and playback plumbing.
- OpenAI for Whisper, and SYSTRAN for `faster-whisper`.
- Hugging Face and `huggingface_hub` for model distribution and retrieval tooling.
- The Piper and Rhasspy projects for local text-to-speech voices and inference tooling.
- LM Studio for a practical local OpenAI-compatible chat endpoint.
- The `aria2` project for fast segmented model downloads.

If the app grows support for additional STT or TTS engines, they should be documented and acknowledged here as first-class upstream dependencies as well.
