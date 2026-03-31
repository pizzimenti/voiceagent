from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile


class PiperTtsService:
    def __init__(self, command: list[str], model_path: str | None, extra_args: list[str] | None = None) -> None:
        self.command = command
        self.model_path = model_path
        self.extra_args = extra_args or []

    @property
    def enabled(self) -> bool:
        return bool(self.command and self.model_path)

    def synthesize(self, text: str) -> Path | None:
        if not self.enabled:
            return None

        fd, raw_path = tempfile.mkstemp(prefix="voiceagent-tts-", suffix=".wav")
        os.close(fd)
        Path(raw_path).unlink(missing_ok=True)
        output_path = Path(raw_path)

        args = [
            *self.command,
            "--model",
            self.model_path,
            "--output_file",
            str(output_path),
            *self.extra_args,
        ]

        proc = subprocess.run(
            args,
            input=text.encode("utf-8"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if proc.returncode != 0:
            output_path.unlink(missing_ok=True)
            stderr = proc.stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(stderr or "TTS command failed.")

        if not output_path.exists() or output_path.stat().st_size == 0:
            output_path.unlink(missing_ok=True)
            raise RuntimeError("TTS did not create an audio file.")

        return output_path
