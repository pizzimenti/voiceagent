from __future__ import annotations

from dataclasses import dataclass
import logging
import os
from pathlib import Path
import shlex
import sys
from typing import Literal

from voiceagent.paths import default_stt_model_root, default_tts_model_root

_VALID_TTS_ENGINES: frozenset[str] = frozenset({"piper", "chatterbox"})


@dataclass(slots=True)
class AppConfig:
    lm_studio_base_url: str
    lm_studio_model: str
    lm_studio_system_prompt: str
    lm_studio_timeout_seconds: int
    # Separate timeout for `/api/v1/models/load` POST. LM Studio model
    # loads frequently take 30-90+ seconds (disk I/O, GPU offload), so
    # the fast-path `lm_studio_timeout_seconds` (default 10) is too
    # short for the load path and surfaces as a spurious "model failed
    # to load" error in the UI even when LM Studio is still happily
    # loading the model. 300 s mirrors what the LM Studio first-party
    # UI uses internally.
    lm_studio_load_timeout_seconds: int
    whisper_model: str
    whisper_device: str
    whisper_compute_type: str
    tts_command: list[str]
    tts_model: str | None
    tts_extra_args: list[str]
    stt_model_root: Path
    tts_model_root: Path
    chatterbox_references_root: Path
    tts_engine: Literal["piper", "chatterbox"] = "piper"
    sample_rate: int = 16_000
    # Conversation-history cap fed into `ConversationModel.to_openai_messages`.
    # Counts finalized user + assistant rows, so the default `20` keeps the
    # last 10 user / 10 assistant turns. The system prompt is always retained
    # on top of this. A token-aware trim is the natural follow-up; turn-count
    # is good enough to unblock multi-turn for v0.11.
    max_history_turns: int = 20

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
        raw_timeout = (os.environ.get("LM_STUDIO_TIMEOUT_SECONDS", "") or "").strip()
        try:
            lm_studio_timeout_seconds = int(raw_timeout) if raw_timeout else 10
        except ValueError:
            lm_studio_timeout_seconds = 10
        raw_load_timeout = (os.environ.get("LM_STUDIO_LOAD_TIMEOUT_SECONDS", "") or "").strip()
        try:
            lm_studio_load_timeout_seconds = int(raw_load_timeout) if raw_load_timeout else 300
        except ValueError:
            lm_studio_load_timeout_seconds = 300
        raw_history_turns = (os.environ.get("VOICEAGENT_MAX_HISTORY_TURNS", "") or "").strip()
        try:
            max_history_turns = int(raw_history_turns) if raw_history_turns else 20
        except ValueError:
            max_history_turns = 20
        max_history_turns = max(0, max_history_turns)
        raw_engine = (os.environ.get("VOICEAGENT_TTS_ENGINE", "") or "").strip().lower()
        if not raw_engine:
            tts_engine: Literal["piper", "chatterbox"] = "piper"
        elif raw_engine in _VALID_TTS_ENGINES:
            tts_engine = raw_engine  # type: ignore[assignment]
        else:
            logging.getLogger(__name__).warning(
                "Unknown VOICEAGENT_TTS_ENGINE=%r; falling back to 'piper'",
                raw_engine,
            )
            tts_engine = "piper"
        chatterbox_references_root = Path(
            os.environ.get(
                "VOICEAGENT_CHATTERBOX_REFERENCES_ROOT",
                str(Path.home() / ".local/share/voiceagent/chatterbox-references"),
            )
        ).expanduser()
        return cls(
            lm_studio_base_url=os.environ.get("LM_STUDIO_BASE_URL", "http://127.0.0.1:1234/v1").rstrip("/"),
            lm_studio_model=os.environ.get("LM_STUDIO_MODEL", "").strip(),
            lm_studio_system_prompt=os.environ.get(
                "LM_STUDIO_SYSTEM_PROMPT",
                "You are a concise local desktop voice assistant. Answer briefly and directly.",
            ).strip(),
            lm_studio_timeout_seconds=max(1, lm_studio_timeout_seconds),
            lm_studio_load_timeout_seconds=max(1, lm_studio_load_timeout_seconds),
            whisper_model=os.environ.get("WHISPER_MODEL", "large-v3").strip(),
            whisper_device=os.environ.get("WHISPER_DEVICE", "auto").strip(),
            whisper_compute_type=os.environ.get("WHISPER_COMPUTE_TYPE", "auto").strip(),
            tts_command=tts_command,
            tts_model=os.environ.get("TTS_MODEL", "").strip() or None,
            tts_extra_args=shlex.split(os.environ.get("TTS_EXTRA_ARGS", "")),
            stt_model_root=stt_model_root,
            tts_model_root=tts_model_root,
            chatterbox_references_root=chatterbox_references_root,
            tts_engine=tts_engine,
            max_history_turns=max_history_turns,
        )
