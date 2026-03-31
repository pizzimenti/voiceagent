from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
import logging
from pathlib import Path

from PySide6.QtCore import QObject, Signal

from voiceagent.backends import SpeechToTextBackend, TextToSpeechBackend
from voiceagent.models import AppState, PipelineResult
from voiceagent.services.audio import MicrophoneRecorder
from voiceagent.services.chat import LmStudioClient
from voiceagent.services.playback import AudioPlayer


class VoiceController(QObject):
    state_changed = Signal(str)
    status_changed = Signal(str)
    transcript_changed = Signal(str)
    response_changed = Signal(str)
    error_changed = Signal(str)
    pipeline_completed = Signal(object)
    pipeline_failed = Signal(str)
    pipeline_state_changed = Signal(str, str)

    def __init__(
        self,
        recorder: MicrophoneRecorder,
        transcriber: SpeechToTextBackend,
        chat_client: LmStudioClient,
        tts_service: TextToSpeechBackend,
        player: AudioPlayer,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.recorder = recorder
        self.transcriber = transcriber
        self.chat_client = chat_client
        self.tts_service = tts_service
        self.player = player
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="voiceagent")
        self.state = AppState.IDLE
        self._logger = logging.getLogger(__name__)

        self.pipeline_completed.connect(self._apply_pipeline_result)
        self.pipeline_failed.connect(self._apply_pipeline_error)
        self.pipeline_state_changed.connect(self._apply_state)
        self.player.playback_finished.connect(self._handle_playback_finished)
        self.player.playback_failed.connect(self._handle_playback_failed)

        self._apply_state(AppState.IDLE.value, "Ready")

    def start_recording(self) -> None:
        if self.state not in {AppState.IDLE}:
            return

        self.error_changed.emit("")
        self.transcript_changed.emit("")
        self.response_changed.emit("")
        self.status_changed.emit("Listening")
        try:
            self.recorder.start()
        except Exception as exc:
            self._logger.exception("Failed to start microphone recording")
            self.error_changed.emit(str(exc))
            self._apply_state(AppState.IDLE.value, "Microphone unavailable")
            return

        self._set_state(AppState.RECORDING)

    def stop_recording(self) -> None:
        if self.state != AppState.RECORDING:
            return

        try:
            audio_path = self.recorder.stop()
        except Exception as exc:
            self._logger.exception("Failed to stop microphone recording")
            self.error_changed.emit(str(exc))
            self._apply_state(AppState.IDLE.value, "Recording failed")
            return

        self._apply_state(AppState.TRANSCRIBING.value, "Preparing audio")
        future = self.executor.submit(self._run_pipeline, audio_path)
        future.add_done_callback(self._handle_pipeline_done)

    def shutdown(self) -> None:
        self.executor.shutdown(wait=False, cancel_futures=True)

    def _run_pipeline(self, audio_path: Path) -> PipelineResult:
        try:
            if not self.transcriber.is_loaded:
                self.pipeline_state_changed.emit(
                    AppState.TRANSCRIBING.value,
                    f"Loading {self.transcriber.backend_name} {self.transcriber.selection_label.lower()}",
                )
                self.transcriber.ensure_loaded()

            self.pipeline_state_changed.emit(AppState.TRANSCRIBING.value, "Transcribing")
            transcript = self.transcriber.transcribe(audio_path)
            self.transcript_changed.emit(transcript)
            self.pipeline_state_changed.emit(AppState.THINKING.value, "Waiting for LM Studio")
            response = self.chat_client.complete(transcript)
            self.response_changed.emit(response)

            tts_audio_path = None
            if self.tts_service.enabled:
                self.pipeline_state_changed.emit(AppState.SYNTHESIZING.value, "Generating speech")
                tts_audio_path = self.tts_service.synthesize(response)
            else:
                self._logger.info("TTS skipped because no voice is selected")

            return PipelineResult(
                transcript=transcript,
                response=response,
                tts_audio_path=tts_audio_path,
            )
        except Exception:
            self._logger.exception("Voice pipeline failed path=%s", audio_path)
            raise
        finally:
            audio_path.unlink(missing_ok=True)

    def _handle_pipeline_done(self, future: Future[PipelineResult]) -> None:
        try:
            result = future.result()
        except Exception as exc:
            self.pipeline_failed.emit(str(exc))
            return

        self.pipeline_completed.emit(result)

    def _apply_pipeline_result(self, result: PipelineResult) -> None:
        self.error_changed.emit("")

        if result.tts_audio_path is None:
            self._apply_state(AppState.IDLE.value, "Ready")
            return

        self._set_state(AppState.SPEAKING)
        self.status_changed.emit("Playing response")
        self.player.play_file(result.tts_audio_path)

    def _apply_pipeline_error(self, message: str) -> None:
        self.error_changed.emit(message)
        self._apply_state(AppState.IDLE.value, "Ready")

    def _handle_playback_finished(self, _path: str) -> None:
        self._apply_state(AppState.IDLE.value, "Ready")

    def _handle_playback_failed(self, _path: str, message: str) -> None:
        self.error_changed.emit(message)
        self._apply_state(AppState.IDLE.value, "Ready")

    def _apply_state(self, state: str, status: str) -> None:
        self._set_state(AppState(state))
        self.status_changed.emit(status)

    def _set_state(self, state: AppState) -> None:
        self.state = state
        self.state_changed.emit(state.value)
