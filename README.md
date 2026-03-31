# voiceagent

Simple KDE-friendly push-to-talk desktop app in Python.

## Stack

- `PySide6` for the desktop UI and audio playback
- `sounddevice` for microphone capture
- `faster-whisper` for local transcription
- `piper-tts` for local speech synthesis
- LM Studio's OpenAI-compatible local API for chat
- Piper CLI invocation for local speech synthesis

## Setup

Use a virtual environment so the app dependencies stay isolated from the system Python:

```bash
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
- `TTS_COMMAND` default: `piper`
- `TTS_MODEL` optional Piper voice name like `en_US-lessac-medium` or a path to a Piper model file
- `TTS_EXTRA_ARGS` optional extra command-line flags for TTS

## Run

```bash
source .venv/bin/activate
voiceagent
```

The app assumes LM Studio's local server is already running.
