from __future__ import annotations

import os
from pathlib import Path
import tempfile
import threading
from typing import Any
import wave


class MicrophoneRecorder:
    def __init__(self, sample_rate: int = 16_000, channels: int = 1) -> None:
        self.sample_rate = sample_rate
        self.channels = channels
        self._frames: list[bytes] = []
        self._lock = threading.Lock()
        self._stream: Any | None = None

    def start(self) -> None:
        with self._lock:
            if self._stream is not None:
                raise RuntimeError("Recording is already in progress.")

            import sounddevice as sd

            self._frames = []
            self._stream = sd.RawInputStream(
                samplerate=self.sample_rate,
                channels=self.channels,
                dtype="int16",
                callback=self._handle_audio_chunk,
            )
            self._stream.start()

    def stop(self) -> Path:
        with self._lock:
            if self._stream is None:
                raise RuntimeError("Recording is not in progress.")

            stream = self._stream
            self._stream = None

        stream.stop()
        stream.close()

        if not self._frames:
            raise RuntimeError("No audio was captured from the microphone.")

        fd, raw_path = tempfile.mkstemp(prefix="voiceagent-input-", suffix=".wav")
        os.close(fd)
        Path(raw_path).unlink(missing_ok=True)
        path = Path(raw_path)

        with wave.open(str(path), "wb") as wav_file:
            wav_file.setnchannels(self.channels)
            wav_file.setsampwidth(2)
            wav_file.setframerate(self.sample_rate)
            for frame in self._frames:
                wav_file.writeframes(frame)

        return path

    def _handle_audio_chunk(self, indata, frames, time_info, status) -> None:
        if status:
            return

        self._frames.append(bytes(indata))
