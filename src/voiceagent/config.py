from __future__ import annotations

from dataclasses import dataclass
import os
import shlex


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
    sample_rate: int = 16_000

    @classmethod
    def from_env(cls) -> "AppConfig":
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
            tts_command=shlex.split(os.environ.get("TTS_COMMAND", "piper")),
            tts_model=os.environ.get("TTS_MODEL", "").strip() or None,
            tts_extra_args=shlex.split(os.environ.get("TTS_EXTRA_ARGS", "")),
        )

