from __future__ import annotations

import logging
import os
from pathlib import Path
import shutil
import tempfile
import urllib.request
import wave

from huggingface_hub import hf_hub_url
from piper import PiperVoice

from voiceagent.backends import TextToSpeechBackend
from voiceagent.downloaders import AriaDownloader, DownloadFile, DownloadProgress
from voiceagent.paths import default_tts_model_root


class PiperTtsService(TextToSpeechBackend):
    backend_name = "Piper"
    selection_label = "Voice"
    VOICE_REPOSITORY = "rhasspy/piper-voices"
    VOICES_JSON_URL = "https://huggingface.co/rhasspy/piper-voices/resolve/main/voices.json?download=true"

    def __init__(self, command: list[str], model_path: str | None, extra_args: list[str] | None = None) -> None:
        self.command = command
        self.model_path = model_path
        self.extra_args = extra_args or []
        self.model_root = default_tts_model_root()
        self.downloader = AriaDownloader(connections=10)
        self._logger = logging.getLogger(__name__)
        self._loaded_voice_path: Path | None = None
        self._voice: PiperVoice | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.command and self.model_path)

    @property
    def is_available(self) -> bool:
        if not self.model_path:
            return False
        # Stay in lockstep with `is_item_available(selected)`: a voice is
        # only "available" when BOTH the `.onnx` and its paired
        # `.onnx.json` config exist. Previously this returned True as
        # soon as any `.onnx` candidate resolved, which let a partial
        # download (onnx only, no json) masquerade as ready and drove
        # the loader to emit `load_completed` for a voice that would
        # crash on first synthesis.
        return self.is_item_available(self.model_path)

    @property
    def can_download(self) -> bool:
        return bool(self.model_path and self._looks_like_voice_name(self.model_path))

    @classmethod
    def known_voice_names(cls, model_root: Path) -> set[str]:
        """Union of on-disk installed voices and remote-cache catalog.

        Single source of truth for "is this name a Piper voice we know
        about" — used by `available_voice_names` (sorted listing) and by
        `is_item_managed` (membership check). `_looks_like_voice_name`
        is a syntactic guess on user-typed strings and is intentionally
        NOT folded in here.
        """
        return cls._cached_voice_names(model_root) | cls._voice_names_from_cache_file(model_root)

    @classmethod
    def available_voice_names(cls, model_root: Path, configured_model: str | None = None) -> list[str]:
        """Return the eager on-disk catalog (installed + cached + configured).

        This path must never touch the network — see AGENTS.md's "keep
        network/model refreshes off the first paint path" rule. The
        asynchronous refresh that adds remote-only entries is driven by
        `refresh_remote_catalog`, which is expected to run after the QML
        window has painted.
        """
        voices: set[str] = set(cls.known_voice_names(model_root))
        if configured_model:
            voices.add(configured_model)
        return sorted(voices)

    @classmethod
    def refresh_remote_catalog(
        cls, model_root: Path, configured_model: str | None = None
    ) -> list[str]:
        """Fetch `voices.json`, refresh the on-disk cache, and return the union.

        Safe to run from a worker thread: only performs a `urlopen` and a
        file write to the cache path. Returns the same eager union as
        `available_voice_names` when the network fetch fails, so callers
        can treat any failure as a no-op.
        """
        cls._fetch_and_cache_voice_names(model_root)
        return cls.available_voice_names(model_root, configured_model)

    def available_items(self) -> list[str]:
        return self.available_voice_names(self.model_root, self.model_path)

    def refresh_catalog(self) -> list[str]:
        """Worker-thread entry point for the deferred catalog refresh."""
        return self.refresh_remote_catalog(self.model_root, self.model_path)

    @classmethod
    def is_voice_available(cls, model_root: Path, model_path: str | None) -> bool:
        if not model_path:
            return False

        # Synthesis (`_get_voice`) loads `<resolved>.json` alongside the
        # `.onnx`. Reporting "available" without that sidecar lets a
        # bare `custom.onnx` masquerade as ready and crash on first
        # synthesis. Require both files for every resolved branch.
        candidate = Path(model_path).expanduser()
        if candidate.exists():
            return Path(f"{candidate}.json").exists()

        local_candidate = model_root / model_path
        if local_candidate.exists():
            return Path(f"{local_candidate}.json").exists()

        onnx_candidate = model_root / f"{model_path}.onnx"
        json_candidate = model_root / f"{model_path}.onnx.json"
        return onnx_candidate.exists() and json_candidate.exists()

    def is_item_available(self, item_name: str) -> bool:
        return self.is_voice_available(self.model_root, item_name)

    def is_item_managed(self, item_name: str) -> bool:
        return item_name in self.known_voice_names(self.model_root)

    def is_item_downloadable(self, item_name: str) -> bool:
        return self.is_item_managed(item_name)

    @property
    def selected_item(self) -> str | None:
        return self.model_path

    def set_model_path(self, model_path: str | None) -> None:
        self.model_path = model_path
        self._loaded_voice_path = None
        self._voice = None

    def set_selected_item(self, item_name: str | None) -> None:
        self.set_model_path(item_name)

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

        try:
            with wave.open(str(output_path), "wb") as wav_file:
                self._get_voice(resolved_model_path).synthesize_wav(text, wav_file)
        except Exception as exc:
            output_path.unlink(missing_ok=True)
            raise RuntimeError(str(exc) or "TTS synthesis failed.") from exc

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

    def download_selected_item(self, progress_callback=None) -> None:
        self.download_voice(progress_callback=progress_callback)

    def download_item(self, item_name: str, progress_callback=None) -> None:
        self._download_voice(item_name, progress_callback=progress_callback)

    def remove_item(self, item_name: str) -> None:
        if not item_name:
            return

        candidate = self.model_root / f"{item_name}.onnx"
        config_candidate = self.model_root / f"{item_name}.onnx.json"
        nested_candidate = self.model_root / item_name

        if candidate.exists():
            candidate.unlink()
        if config_candidate.exists():
            config_candidate.unlink()
        if nested_candidate.exists() and nested_candidate.is_dir():
            shutil.rmtree(nested_candidate)

        if self.model_path == item_name:
            self._loaded_voice_path = None
            self._voice = None

    def artifact_paths(self, item_name: str) -> list[Path]:
        """Return the two files a Piper voice install is made of.

        Used by `ParallelItemLoader._verify_download` (to look for
        aria2 sidecars) and `_cleanup_failed_download` (to wipe
        partials). The order is `[onnx, onnx.json]`; the base
        verifier treats any `<artifact>.aria2` as a failed transfer.
        """
        onnx_path = self.model_root / f"{item_name}.onnx"
        json_path = self.model_root / f"{item_name}.onnx.json"
        return [onnx_path, json_path]

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

    def describe_selection_state(self) -> dict[str, str | bool]:
        model_path = self.model_path
        if not model_path:
            return {
                "selected_model": "",
                "available": False,
                "can_download": False,
                "resolved_model_path": "",
                "direct_candidate": "",
                "local_candidate": "",
                "onnx_candidate": "",
                "json_candidate": "",
            }

        candidate = Path(model_path).expanduser()
        local_candidate = self.model_root / model_path
        onnx_candidate = self.model_root / f"{model_path}.onnx"
        json_candidate = self.model_root / f"{model_path}.onnx.json"
        resolved_model_path = self._resolve_existing_model_path()
        return {
            "selected_model": model_path,
            "available": resolved_model_path is not None,
            "can_download": self.can_download,
            "resolved_model_path": str(resolved_model_path) if resolved_model_path else "",
            "direct_candidate": str(candidate),
            "local_candidate": str(local_candidate),
            "onnx_candidate": str(onnx_candidate),
            "json_candidate": str(json_candidate),
        }

    def _get_voice(self, resolved_model_path: Path) -> PiperVoice:
        if self._voice is not None and self._loaded_voice_path == resolved_model_path:
            return self._voice

        config_path = Path(f"{resolved_model_path}.json")
        self._logger.info("Loading Piper voice model=%s config=%s", resolved_model_path, config_path)
        self._voice = PiperVoice.load(
            resolved_model_path,
            config_path=config_path,
            use_cuda=False,
            download_dir=self.model_root,
        )
        self._loaded_voice_path = resolved_model_path
        return self._voice

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
