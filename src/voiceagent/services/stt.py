from __future__ import annotations

import logging
import os
from pathlib import Path
import shutil
from typing import Any

from huggingface_hub import HfApi, hf_hub_url

from voiceagent.backends import SpeechToTextBackend
from voiceagent.downloaders import AriaDownloader, DownloadFile
from voiceagent.paths import default_stt_model_root


class WhisperTranscriber(SpeechToTextBackend):
    backend_name = "Whisper"
    selection_label = "Model"
    REQUIRED_MODEL_FILES = (
        ".gitattributes",
        "README.md",
        "config.json",
        "model.bin",
        "tokenizer.json",
    )
    VOCABULARY_FILES = (
        "vocabulary.json",
        "vocabulary.txt",
    )

    MODEL_REPOSITORIES = {
        "tiny": "Systran/faster-whisper-tiny",
        "tiny.en": "Systran/faster-whisper-tiny.en",
        "base": "Systran/faster-whisper-base",
        "base.en": "Systran/faster-whisper-base.en",
        "small": "Systran/faster-whisper-small",
        "small.en": "Systran/faster-whisper-small.en",
        "medium": "Systran/faster-whisper-medium",
        "medium.en": "Systran/faster-whisper-medium.en",
        "large-v1": "Systran/faster-whisper-large-v1",
        "large-v2": "Systran/faster-whisper-large-v2",
        "large-v3": "Systran/faster-whisper-large-v3",
        "distil-large-v2": "Systran/faster-distil-whisper-large-v2",
        "distil-large-v3": "Systran/faster-distil-whisper-large-v3",
    }

    def __init__(self, model_name: str, device: str = "auto", compute_type: str = "auto") -> None:
        self.model_name = model_name
        self.device = device
        self.compute_type = compute_type
        self._model: Any | None = None
        self._logger = logging.getLogger(__name__)
        self.model_root = default_stt_model_root()
        self.downloader = AriaDownloader(connections=10)

    @classmethod
    def available_model_names(cls) -> list[str]:
        return list(cls.MODEL_REPOSITORIES.keys())

    def available_items(self) -> list[str]:
        return self.available_model_names()

    @property
    def is_loaded(self) -> bool:
        return self._model is not None

    @property
    def is_available(self) -> bool:
        if self.is_loaded:
            return True

        return self.is_model_available(self.model_root, self.model_name)

    @classmethod
    def is_model_available(cls, model_root: Path, model_name: str) -> bool:
        model_path = Path(model_name).expanduser()
        if model_path.exists():
            return True

        repo_id = cls.MODEL_REPOSITORIES.get(model_name)
        if repo_id is None:
            return False

        local_dir = model_root / model_name
        has_required_files = all(
            (local_dir / filename).exists() and (local_dir / filename).stat().st_size > 0
            for filename in cls.REQUIRED_MODEL_FILES
        )
        has_vocabulary_file = any(
            (local_dir / filename).exists() and (local_dir / filename).stat().st_size > 0
            for filename in cls.VOCABULARY_FILES
        )
        return has_required_files and has_vocabulary_file

    def is_item_available(self, item_name: str) -> bool:
        return self.is_model_available(self.model_root, item_name)

    @property
    def selected_item(self) -> str:
        return self.model_name

    def set_model_name(self, model_name: str) -> None:
        if self.model_name == model_name:
            return

        self.model_name = model_name
        self._model = None

    def set_selected_item(self, item_name: str) -> None:
        self.set_model_name(item_name)

    def ensure_loaded(self) -> None:
        self._get_model()

    def download_item(self, item_name: str, progress_callback=None) -> None:
        self._prepare_model_source(item_name=item_name, progress_callback=progress_callback)

    def remove_item(self, item_name: str) -> None:
        repo_id = self.MODEL_REPOSITORIES.get(item_name)
        if repo_id is None:
            raise RuntimeError(f"Whisper model '{item_name}' cannot be removed because it is not a managed catalog item.")

        local_dir = self.model_root / item_name
        if not local_dir.exists():
            return

        shutil.rmtree(local_dir)
        if self.model_name == item_name:
            self._model = None

    def download_and_load(self, progress_callback=None) -> None:
        model_source = self._prepare_model_source(item_name=self.model_name, progress_callback=progress_callback)
        if self._model is None:
            from faster_whisper import WhisperModel

            self._logger.info(
                "Loading Whisper model name=%s source=%s device=%s compute_type=%s",
                self.model_name,
                model_source,
                self.device,
                self.compute_type,
            )
            self._model = WhisperModel(
                model_source,
                device=self.device,
                compute_type=self.compute_type,
            )
            self._logger.info("Whisper model loaded name=%s source=%s", self.model_name, model_source)

    def transcribe(self, audio_path: Path) -> str:
        self._logger.info("Starting Whisper transcription path=%s", audio_path)
        model = self._get_model()
        segments, info = model.transcribe(
            str(audio_path),
            beam_size=1,
            vad_filter=True,
        )
        transcript = " ".join(segment.text.strip() for segment in segments if segment.text.strip()).strip()
        if not transcript:
            detected_language = getattr(info, "language", "unknown")
            self._logger.warning(
                "Whisper returned empty transcript path=%s detected_language=%s",
                audio_path,
                detected_language,
            )
            raise RuntimeError(f"Whisper did not return any transcript. Detected language: {detected_language}.")

        self._logger.info("Whisper transcription completed path=%s transcript_chars=%s", audio_path, len(transcript))
        return transcript

    def _get_model(self):
        if self._model is None:
            self.download_and_load()

        return self._model

    def _prepare_model_source(self, item_name: str, progress_callback=None) -> str:
        model_path = Path(item_name).expanduser()
        if model_path.exists():
            return str(model_path)

        repo_id = self.MODEL_REPOSITORIES.get(item_name)
        if repo_id is None:
            return item_name

        local_dir = self.model_root / item_name
        local_dir.mkdir(parents=True, exist_ok=True)

        self._logger.info("Checking Whisper model repo=%s local_dir=%s", repo_id, local_dir)
        api = HfApi()
        headers = self._download_headers()
        hf_token = os.environ.get("HF_TOKEN", "").strip() or None
        files: list[DownloadFile] = []
        for sibling in sorted(api.repo_info(repo_id, files_metadata=True, token=hf_token).siblings, key=self._sibling_name):
            filename = sibling.rfilename
            if "/" in filename:
                continue

            url = hf_hub_url(repo_id, filename=filename)
            size_bytes = getattr(sibling, "size", None) or self.downloader.get_remote_size(url, headers=headers)
            destination = local_dir / filename
            if destination.exists() and destination.stat().st_size == size_bytes:
                continue

            files.append(
                DownloadFile(
                    url=url,
                    destination=destination,
                    size_bytes=size_bytes,
                )
            )

        if files:
            self._logger.info(
                "Downloading Whisper model repo=%s local_dir=%s file_count=%s",
                repo_id,
                local_dir,
                len(files),
            )
            self.downloader.download(
                files,
                progress_callback=progress_callback,
                headers=headers,
            )
            self._logger.info("Whisper model download completed repo=%s local_dir=%s", repo_id, local_dir)

        return str(local_dir)

    def _download_headers(self) -> dict[str, str]:
        hf_token = os.environ.get("HF_TOKEN", "").strip()
        if not hf_token:
            return {}

        return {"Authorization": f"Bearer {hf_token}"}

    def _sibling_name(self, sibling: Any) -> str:
        return getattr(sibling, "rfilename", "")
