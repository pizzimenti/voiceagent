# voiceagent

Push-to-talk KDE-friendly desktop voice assistant for local speech workflows.

## Stack

- `PySide6` for the desktop UI and audio playback
- `sounddevice` for microphone capture
- `faster-whisper` for local transcription
- `piper-tts` for local speech synthesis
- `aria2c` for segmented model downloads
- LM Studio's OpenAI-compatible local API for chat
- Piper's Python runtime for local speech synthesis

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
- `VOICEAGENT_STT_MODEL_ROOT` default: `$XDG_DATA_HOME/voiceagent/stt-models` or `~/.local/share/voiceagent/stt-models`
- `VOICEAGENT_TTS_MODEL_ROOT` default: `$XDG_DATA_HOME/voiceagent/tts-models` or `~/.local/share/voiceagent/tts-models`
- `TTS_MODEL` optional Piper voice name like `en_US-lessac-medium` or a path to a Piper model file
- `TTS_EXTRA_ARGS` optional extra command-line flags for TTS

Whisper downloads, Hugging Face cache data, and Piper voices are stored under the app's XDG data directory by default. Logs are stored under `$XDG_STATE_HOME/voiceagent/logs` or `~/.local/state/voiceagent/logs`.

Model downloads use `aria2c` with 10 parallel connections by default, and the app shows live progress and transfer speed while Whisper is loading.

## Run

```bash
source .venv/bin/activate
voiceagent
```

The app assumes LM Studio's local server is already running.

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

1. Create and push a signed or otherwise finalized Git tag such as `v0.1.0`.
2. Confirm the GitHub release tarball for that tag exists and matches the expected source layout.
3. Update `pkgver` in `packaging/PKGBUILD.aur` if needed.
4. Replace `sha256sums=('SKIP')` in `packaging/PKGBUILD.aur` with the real release checksum if you want reproducible source verification.
5. Build the release package locally with `makepkg -f` from an isolated copy of the tagged source.
6. Verify runtime behavior after installation:
   `voiceagent`, desktop entry launch, XDG data paths, model download flow, microphone capture, TTS playback.
7. Regenerate `.SRCINFO` from the AUR package recipe before publishing to the AUR repo.
8. Recheck AUR dependency names, especially for non-core Python speech packages that may move between official repos and AUR.

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
