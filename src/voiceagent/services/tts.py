from __future__ import annotations

import logging
import os
from pathlib import Path
import subprocess
import tempfile
import urllib.request

from huggingface_hub import hf_hub_url

from voiceagent.downloaders import AriaDownloader, DownloadFile, DownloadProgress


class PiperTtsService:
    VOICE_REPOSITORY = "rhasspy/piper-voices"
    VOICES_JSON_URL = "https://huggingface.co/rhasspy/piper-voices/resolve/main/voices.json?download=true"

    def __init__(self, command: list[str], model_path: str | None, extra_args: list[str] | None = None) -> None:
        self.command = command
        self.model_path = model_path
        self.extra_args = extra_args or []
        self.model_root = Path.cwd() / "tts-models"
        self.downloader = AriaDownloader(connections=10)
        self._logger = logging.getLogger(__name__)

    @property
    def enabled(self) -> bool:
        return bool(self.command and self.model_path)

    @property
    def is_available(self) -> bool:
        if not self.model_path:
            return False
        return self._resolve_existing_model_path() is not None

    @property
    def can_download(self) -> bool:
        return bool(self.model_path and self._looks_like_voice_name(self.model_path))

    @classmethod
    def available_voice_names(cls, model_root: Path, configured_model: str | None = None) -> list[str]:
        voices: set[str] = set()
        if configured_model:
            voices.add(configured_model)

        voices.update(cls._cached_voice_names(model_root))
        cached_voices = cls._voice_names_from_cache_file(model_root)
        voices.update(cached_voices)
        if not cached_voices:
            voices.update(cls._fetch_and_cache_voice_names(model_root))

        return sorted(voices)

    @classmethod
    def is_voice_available(cls, model_root: Path, model_path: str | None) -> bool:
        if not model_path:
            return False

        candidate = Path(model_path).expanduser()
        if candidate.exists():
            return True

        local_candidate = model_root / model_path
        if local_candidate.exists():
            return True

        onnx_candidate = model_root / f"{model_path}.onnx"
        json_candidate = model_root / f"{model_path}.onnx.json"
        return onnx_candidate.exists() and json_candidate.exists()

    def set_model_path(self, model_path: str | None) -> None:
        self.model_path = model_path

    def synthesize(self, text: str, progress_callback=None) -> Path | None:
        if not self.enabled:
            return None

        fd, raw_path = tempfile.mkstemp(prefix="voiceagent-tts-", suffix=".wav")
        os.close(fd)
        Path(raw_path).unlink(missing_ok=True)
        output_path = Path(raw_path)

        resolved_model_path = self._resolve_existing_model_path()
        if resolved_model_path is None:
            raise RuntimeError(self._missing_model_message())
        args = [
            *self.command,
            "--model",
            str(resolved_model_path),
            "--data-dir",
            str(self.model_root),
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

    def download_voice(self, progress_callback=None) -> None:
        if not self.enabled:
            raise RuntimeError("TTS is not configured. Set TTS_MODEL to a Piper voice or model path.")

        if self.is_available:
            return

        if not self.can_download:
            raise RuntimeError(self._missing_model_message())

        assert self.model_path is not None
        self._download_voice(self.model_path, progress_callback=progress_callback)

    def _resolve_existing_model_path(self) -> Path | None:
        assert self.model_path is not None

        candidate = Path(self.model_path).expanduser()
        if candidate.exists():
            return candidate

        local_candidate = self.model_root / self.model_path
        if local_candidate.exists():
            return local_candidate

        onnx_candidate = self.model_root / f"{self.model_path}.onnx"
        if onnx_candidate.exists():
            return onnx_candidate

        return None

    def _download_voice(self, voice_name: str, progress_callback=None) -> None:
        onnx_path = self.model_root / f"{voice_name}.onnx"
        json_path = self.model_root / f"{voice_name}.onnx.json"
        if onnx_path.exists() and json_path.exists():
            return

        remote_prefix = self._voice_remote_prefix(voice_name)
        onnx_url = hf_hub_url(self.VOICE_REPOSITORY, filename=f"{remote_prefix}.onnx")
        json_url = hf_hub_url(self.VOICE_REPOSITORY, filename=f"{remote_prefix}.onnx.json")
        self._logger.info("Downloading Piper voice voice=%s model_root=%s", voice_name, self.model_root)
        files = [
            DownloadFile(
                url=onnx_url,
                destination=onnx_path,
                size_bytes=self.downloader.get_remote_size(onnx_url),
            ),
            DownloadFile(
                url=json_url,
                destination=json_path,
                size_bytes=self.downloader.get_remote_size(json_url),
            ),
        ]
        callback = progress_callback or (lambda progress: None)
        callback(DownloadProgress(completed_bytes=0, total_bytes=sum(file.size_bytes for file in files), download_speed_bytes_per_second=0))
        self.downloader.download(files, progress_callback=callback)
        self._logger.info("Piper voice download completed voice=%s model_root=%s", voice_name, self.model_root)

    def _missing_model_message(self) -> str:
        assert self.model_path is not None
        if self.can_download:
            return f"Piper voice '{self.model_path}' is not downloaded. Click Load Voice first."
        return f"TTS model path not found: {self.model_path}"

    @classmethod
    def _cached_voice_names(cls, model_root: Path) -> set[str]:
        voices: set[str] = set()
        for onnx_path in model_root.glob("*.onnx"):
            if (model_root / f"{onnx_path.name}.json").exists():
                voices.add(onnx_path.stem)
        return voices

    @classmethod
    def _voice_names_from_cache_file(cls, model_root: Path) -> set[str]:
        cache_path = model_root / "voices.json"
        if not cache_path.exists():
            return set()

        try:
            import json

            return set(json.loads(cache_path.read_text(encoding="utf-8")).keys())
        except Exception:
            return set()

    @classmethod
    def _fetch_and_cache_voice_names(cls, model_root: Path) -> set[str]:
        try:
            with urllib.request.urlopen(cls.VOICES_JSON_URL, timeout=5) as response:
                payload = response.read().decode("utf-8")
        except Exception:
            return set()

        try:
            import json

            voices = set(json.loads(payload).keys())
            cache_path = model_root / "voices.json"
            cache_path.write_text(payload, encoding="utf-8")
            return voices
        except Exception:
            return set()

    def _looks_like_voice_name(self, value: str) -> bool:
        return "://" not in value and "/" not in value and value.count("-") >= 2

    def _voice_remote_prefix(self, voice_name: str) -> str:
        parts = voice_name.split("-")
        locale = parts[0]
        quality = parts[-1]
        speaker = "-".join(parts[1:-1])
        if not locale or not speaker or not quality or "_" not in locale:
            raise RuntimeError(f"Unsupported Piper voice name format: {voice_name}")

        language = locale.split("_", 1)[0]
        return f"{language}/{locale}/{speaker}/{quality}/{voice_name}"
