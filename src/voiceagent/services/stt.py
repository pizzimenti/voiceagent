from __future__ import annotations

from pathlib import Path
from typing import Any


class WhisperTranscriber:
    def __init__(self, model_name: str, device: str = "auto", compute_type: str = "auto") -> None:
        self.model_name = model_name
        self.device = device
        self.compute_type = compute_type
        self._model: Any | None = None

    def transcribe(self, audio_path: Path) -> str:
        model = self._get_model()
        segments, info = model.transcribe(
            str(audio_path),
            beam_size=1,
            vad_filter=True,
        )
        transcript = " ".join(segment.text.strip() for segment in segments if segment.text.strip()).strip()
        if not transcript:
            detected_language = getattr(info, "language", "unknown")
            raise RuntimeError(f"Whisper did not return any transcript. Detected language: {detected_language}.")

        return transcript

    def _get_model(self):
        if self._model is None:
            from faster_whisper import WhisperModel

            self._model = WhisperModel(
                self.model_name,
                device=self.device,
                compute_type=self.compute_type,
            )

        return self._model
