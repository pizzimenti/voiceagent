from __future__ import annotations

import os
from pathlib import Path


APP_NAME = "voiceagent"


def _xdg_home(env_var: str, default_relative: str) -> Path:
    value = os.environ.get(env_var, "").strip()
    if value:
        return Path(value).expanduser()
    return Path.home() / default_relative


def app_data_dir() -> Path:
    return _xdg_home("XDG_DATA_HOME", ".local/share") / APP_NAME


def app_state_dir() -> Path:
    return _xdg_home("XDG_STATE_HOME", ".local/state") / APP_NAME


# ---------------------------------------------------------------------------
# Hierarchical, per-engine layout (v0.12.1+).
#
# `default_data_root()` is the top-level voiceagent data directory; every
# engine-specific helper resolves under it. The structure is:
#
#   ~/.local/share/voiceagent/
#     stt/
#       whisper/                      # default_whisper_root()
#         huggingface/                # HF cache used by faster-whisper
#         tiny.en/                    # downloaded model dirs
#         ...
#     tts/
#       piper/                        # default_piper_voices_root()
#         <voice>.onnx
#         <voice>.onnx.json
#         voices.json                 # Piper's catalog cache
#       chatterbox/                   # default_chatterbox_root()
#         model/                      # default_chatterbox_model_root()
#         references/                 # default_chatterbox_references_root()
#           default.wav
#           my-voice.wav
#
# The legacy `default_stt_model_root` / `default_tts_model_root` helpers
# remain as thin aliases — they now point at the new engine-scoped roots
# (`stt/whisper/` and `tts/piper/` respectively) so any direct caller that
# slipped through the audit lands in the right place rather than at the
# old `stt-models/` / `tts-models/` flat dirs.
# ---------------------------------------------------------------------------


def default_data_root() -> Path:
    """Top-level voiceagent data directory (`~/.local/share/voiceagent/`)."""
    return app_data_dir()


def default_stt_root() -> Path:
    """STT engines parent (`<data>/stt/`)."""
    return default_data_root() / "stt"


def default_tts_root() -> Path:
    """TTS engines parent (`<data>/tts/`)."""
    return default_data_root() / "tts"


def default_whisper_root() -> Path:
    """Whisper models (`<data>/stt/whisper/`)."""
    return default_stt_root() / "whisper"


def default_piper_voices_root() -> Path:
    """Piper voices + `voices.json` (`<data>/tts/piper/`)."""
    return default_tts_root() / "piper"


def default_chatterbox_root() -> Path:
    """Chatterbox engine root (`<data>/tts/chatterbox/`)."""
    return default_tts_root() / "chatterbox"


def default_chatterbox_model_root() -> Path:
    """Chatterbox downloaded model artifacts (`<chatterbox>/model/`)."""
    return default_chatterbox_root() / "model"


def default_chatterbox_references_root() -> Path:
    """Chatterbox user-supplied reference clips (`<chatterbox>/references/`)."""
    return default_chatterbox_root() / "references"


# ---------------------------------------------------------------------------
# Legacy aliases — retained so that any direct caller (or test patch
# target) that wasn't migrated still resolves to a sensible path. Both
# now return the engine-scoped equivalents, NOT the pre-v0.12.1 flat
# `stt-models/` / `tts-models/` directories.
# ---------------------------------------------------------------------------


def default_stt_model_root() -> Path:
    """Deprecated alias for `default_whisper_root()`.

    Pre-v0.12.1 this returned `<data>/stt-models/`. The new layout puts
    Whisper artifacts under `<data>/stt/whisper/`; this alias is kept
    so any straggling import does not break, but new code should call
    `default_whisper_root()` directly.
    """
    return default_whisper_root()


def default_tts_model_root() -> Path:
    """Deprecated alias for `default_piper_voices_root()`.

    Pre-v0.12.1 this returned `<data>/tts-models/`. The new layout puts
    Piper voices under `<data>/tts/piper/`; this alias is kept so any
    straggling import does not break, but new code should call
    `default_piper_voices_root()` directly.
    """
    return default_piper_voices_root()


def default_log_dir() -> Path:
    return app_state_dir() / "logs"
