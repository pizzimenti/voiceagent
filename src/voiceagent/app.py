from __future__ import annotations

import sys
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

from voiceagent.backends import SpeechToTextBackend, TextToSpeechBackend
from voiceagent.config import AppConfig
from voiceagent.controller import VoiceController
from voiceagent.logging_utils import configure_logging
from voiceagent.model_loader import WhisperModelLoader
from voiceagent.services.audio import MicrophoneRecorder
from voiceagent.services.chat import LmStudioClient
from voiceagent.services.playback import AudioPlayer
from voiceagent.tts_loader import TtsVoiceLoader
from voiceagent.window import MainWindow

if TYPE_CHECKING:
    from voiceagent.services.stt import WhisperTranscriber
    from voiceagent.services.tts import PiperTtsService


def build_shared_services(
    config: AppConfig,
) -> tuple[SpeechToTextBackend, TextToSpeechBackend, WhisperModelLoader, TtsVoiceLoader]:
    # Backend imports stay local so future optional engines can be added
    # without forcing every provider dependency to be importable at startup.
    from voiceagent.services.stt import WhisperTranscriber
    from voiceagent.services.tts import PiperTtsService

    transcriber = WhisperTranscriber(
        model_name=config.whisper_model,
        device=config.whisper_device,
        compute_type=config.whisper_compute_type,
    )
    transcriber.model_root = config.stt_model_root
    tts_service = PiperTtsService(
        command=config.tts_command,
        model_path=config.tts_model,
        extra_args=config.tts_extra_args,
    )
    tts_service.model_root = config.tts_model_root
    model_loader = WhisperModelLoader(transcriber)
    tts_loader = TtsVoiceLoader(tts_service)
    return transcriber, tts_service, model_loader, tts_loader


def configure_model_environment(stt_model_root: Path, tts_model_root: Path) -> None:
    stt_model_root.mkdir(parents=True, exist_ok=True)
    tts_model_root.mkdir(parents=True, exist_ok=True)
    hf_home = stt_model_root / "huggingface"
    hf_home.mkdir(parents=True, exist_ok=True)

    os.environ["HF_HOME"] = str(hf_home)
    os.environ["HUGGINGFACE_HUB_CACHE"] = str(hf_home / "hub")
    os.environ["HF_HUB_CACHE"] = str(hf_home / "hub")
    os.environ["TRANSFORMERS_CACHE"] = str(hf_home / "transformers")


def build_controller(
    config: AppConfig,
    transcriber: SpeechToTextBackend,
    tts_service: TextToSpeechBackend,
) -> VoiceController:
    recorder = MicrophoneRecorder(sample_rate=config.sample_rate)
    chat_client = LmStudioClient(
        base_url=config.lm_studio_base_url,
        model=config.lm_studio_model,
        system_prompt=config.lm_studio_system_prompt,
        timeout_seconds=config.lm_studio_timeout_seconds,
    )
    player = AudioPlayer()
    return VoiceController(
        recorder=recorder,
        transcriber=transcriber,
        chat_client=chat_client,
        tts_service=tts_service,
        player=player,
    )


def main() -> int:
    log_path = configure_logging()
    logging.getLogger(__name__).info("Starting voiceagent")
    app = QApplication(sys.argv)
    app.setApplicationName("voiceagent")
    app.setApplicationDisplayName("Voice Agent")
    app.setDesktopFileName("voiceagent")
    app.setOrganizationName("voiceagent")
    app.setWindowIcon(QIcon.fromTheme("audio-input-microphone"))
    config = AppConfig.from_env()
    configure_model_environment(config.stt_model_root, config.tts_model_root)
    logging.getLogger(__name__).info("Configured log file path=%s", log_path)
    logging.getLogger(__name__).info("Configured STT model root path=%s", config.stt_model_root)
    logging.getLogger(__name__).info("Configured TTS model root path=%s", config.tts_model_root)
    transcriber, tts_service, model_loader, tts_loader = build_shared_services(config)
    controller = build_controller(config, transcriber=transcriber, tts_service=tts_service)
    window = MainWindow(controller, model_loader, tts_loader)
    window.show()
    return app.exec()
