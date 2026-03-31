from __future__ import annotations

import sys

from PySide6.QtWidgets import QApplication

from voiceagent.audio_check import AudioCheckController
from voiceagent.config import AppConfig
from voiceagent.controller import VoiceController
from voiceagent.services.audio import MicrophoneRecorder
from voiceagent.services.chat import LmStudioClient
from voiceagent.services.playback import AudioPlayer
from voiceagent.services.stt import WhisperTranscriber
from voiceagent.services.tts import PiperTtsService
from voiceagent.window import MainWindow


def build_shared_services(config: AppConfig) -> tuple[WhisperTranscriber, PiperTtsService]:
    transcriber = WhisperTranscriber(
        model_name=config.whisper_model,
        device=config.whisper_device,
        compute_type=config.whisper_compute_type,
    )
    tts_service = PiperTtsService(
        command=config.tts_command,
        model_path=config.tts_model,
        extra_args=config.tts_extra_args,
    )
    return transcriber, tts_service


def build_controller(
    config: AppConfig,
    transcriber: WhisperTranscriber,
    tts_service: PiperTtsService,
) -> VoiceController:
    recorder = MicrophoneRecorder(sample_rate=config.sample_rate)
    chat_client = LmStudioClient(
        base_url=config.lm_studio_base_url,
        model=config.lm_studio_model,
        system_prompt=config.lm_studio_system_prompt,
    )
    player = AudioPlayer()
    return VoiceController(
        recorder=recorder,
        transcriber=transcriber,
        chat_client=chat_client,
        tts_service=tts_service,
        player=player,
    )


def build_audio_check_controller(
    config: AppConfig,
    transcriber: WhisperTranscriber,
    tts_service: PiperTtsService,
) -> AudioCheckController:
    recorder = MicrophoneRecorder(sample_rate=config.sample_rate)
    player = AudioPlayer()
    return AudioCheckController(
        recorder=recorder,
        transcriber=transcriber,
        tts_service=tts_service,
        player=player,
    )


def main() -> int:
    app = QApplication(sys.argv)
    config = AppConfig.from_env()
    transcriber, tts_service = build_shared_services(config)
    controller = build_controller(config, transcriber=transcriber, tts_service=tts_service)
    audio_check_controller = build_audio_check_controller(
        config,
        transcriber=transcriber,
        tts_service=tts_service,
    )
    window = MainWindow(controller, audio_check_controller)
    window.show()
    return app.exec()
