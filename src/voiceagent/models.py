from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


class AppState(StrEnum):
    IDLE = "idle"
    RECORDING = "recording"
    TRANSCRIBING = "transcribing"
    THINKING = "thinking"
    SYNTHESIZING = "synthesizing"
    SPEAKING = "speaking"


@dataclass(slots=True)
class PipelineResult:
    transcript: str
    response: str
    tts_audio_path: Path | None

