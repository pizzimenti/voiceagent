from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import shlex
import sys

from voiceagent.paths import default_stt_model_root, default_tts_model_root


@dataclass(slots=True)
class AppConfig:
    lm_studio_base_url: str
    lm_studio_model: str
    lm_studio_system_prompt: str
    whisper_model: str
    whisper_device: str
    whisper_compute_type: str
    tts_command: list[str]
    tts_model: str | None
    tts_extra_args: list[str]
    stt_model_root: Path
    tts_model_root: Path
    sample_rate: int = 16_000

    @classmethod
    def from_env(cls) -> "AppConfig":
        stt_model_root = Path(os.environ.get("VOICEAGENT_STT_MODEL_ROOT", default_stt_model_root())).expanduser()
        tts_model_root = Path(os.environ.get("VOICEAGENT_TTS_MODEL_ROOT", default_tts_model_root())).expanduser()
        default_tts_command = os.environ.get("TTS_COMMAND", "").strip()
        if default_tts_command:
            tts_command = shlex.split(default_tts_command)
        else:
            venv_piper = Path(sys.executable).with_name("piper")
            tts_command = [str(venv_piper)] if venv_piper.exists() else ["piper"]
        return cls(
            lm_studio_base_url=os.environ.get("LM_STUDIO_BASE_URL", "http://127.0.0.1:1234/v1").rstrip("/"),
            lm_studio_model=os.environ.get("LM_STUDIO_MODEL", "").strip(),
            lm_studio_system_prompt=os.environ.get(
                "LM_STUDIO_SYSTEM_PROMPT",
                "You are a concise local desktop voice assistant. Answer briefly and directly.",
            ).strip(),
            whisper_model=os.environ.get("WHISPER_MODEL", "large-v3").strip(),
            whisper_device=os.environ.get("WHISPER_DEVICE", "auto").strip(),
            whisper_compute_type=os.environ.get("WHISPER_COMPUTE_TYPE", "auto").strip(),
            tts_command=tts_command,
            tts_model=os.environ.get("TTS_MODEL", "").strip() or None,
            tts_extra_args=shlex.split(os.environ.get("TTS_EXTRA_ARGS", "")),
            stt_model_root=stt_model_root,
            tts_model_root=tts_model_root,
        )
