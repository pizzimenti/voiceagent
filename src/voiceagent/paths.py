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


def default_stt_model_root() -> Path:
    return app_data_dir() / "stt-models"


def default_tts_model_root() -> Path:
    return app_data_dir() / "tts-models"


def default_log_dir() -> Path:
    return app_state_dir() / "logs"
