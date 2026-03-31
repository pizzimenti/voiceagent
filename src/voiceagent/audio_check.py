from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import QDialog, QLabel, QPushButton, QTextEdit, QVBoxLayout

from voiceagent.models import AppState
from voiceagent.services.audio import MicrophoneRecorder
from voiceagent.services.playback import AudioPlayer
from voiceagent.services.stt import WhisperTranscriber
from voiceagent.services.tts import PiperTtsService


class AudioCheckController(QObject):
    state_changed = Signal(str)
    status_changed = Signal(str)
    transcript_changed = Signal(str)
    error_changed = Signal(str)
    playback_ready = Signal(str)
    pipeline_failed = Signal(str)
    pipeline_state_changed = Signal(str, str)

    def __init__(
        self,
        recorder: MicrophoneRecorder,
        transcriber: WhisperTranscriber,
        tts_service: PiperTtsService,
        player: AudioPlayer,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.recorder = recorder
        self.transcriber = transcriber
        self.tts_service = tts_service
        self.player = player
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="voiceagent-audio-check")
        self.state = AppState.IDLE

        self.playback_ready.connect(self._play_transcript_audio)
        self.pipeline_failed.connect(self._handle_pipeline_error)
        self.pipeline_state_changed.connect(self._apply_state)
        self.player.playback_finished.connect(self._handle_playback_finished)
        self.player.playback_failed.connect(self._handle_playback_failed)

        self._apply_state(AppState.IDLE.value, "Ready")

    def start_recording(self) -> None:
        if self.state != AppState.IDLE:
            return

        self.error_changed.emit("")
        try:
            self.recorder.start()
        except Exception as exc:
            self.error_changed.emit(str(exc))
            self._apply_state(AppState.IDLE.value, "Microphone unavailable")
            return

        self._set_state(AppState.RECORDING)
        self.status_changed.emit("Listening")

    def stop_recording(self) -> None:
        if self.state != AppState.RECORDING:
            return

        try:
            audio_path = self.recorder.stop()
        except Exception as exc:
            self.error_changed.emit(str(exc))
            self._apply_state(AppState.IDLE.value, "Recording failed")
            return

        self._apply_state(AppState.TRANSCRIBING.value, "Transcribing")
        future = self.executor.submit(self._run_pipeline, audio_path)
        future.add_done_callback(self._handle_pipeline_done)

    def shutdown(self) -> None:
        self.executor.shutdown(wait=False, cancel_futures=True)

    def _run_pipeline(self, audio_path: Path) -> tuple[str, str]:
        try:
            transcript = self.transcriber.transcribe(audio_path)
            self.pipeline_state_changed.emit(AppState.SYNTHESIZING.value, "Generating speech")
            if not self.tts_service.enabled:
                raise RuntimeError("TTS is not configured. Set TTS_MODEL to a Piper voice or model path.")
            tts_audio_path = self.tts_service.synthesize(transcript)
            if tts_audio_path is None:
                raise RuntimeError("TTS did not return an audio file.")
            return transcript, str(tts_audio_path)
        finally:
            audio_path.unlink(missing_ok=True)

    def _handle_pipeline_done(self, future: Future[tuple[str, str]]) -> None:
        try:
            transcript, tts_audio_path = future.result()
        except Exception as exc:
            self.pipeline_failed.emit(str(exc))
            return

        self.error_changed.emit("")
        self.transcript_changed.emit(transcript)
        self.playback_ready.emit(tts_audio_path)

    def _play_transcript_audio(self, audio_path: str) -> None:
        self._set_state(AppState.SPEAKING)
        self.status_changed.emit("Playing transcript")
        self.player.play_file(Path(audio_path))

    def _handle_pipeline_error(self, message: str) -> None:
        self.error_changed.emit(message)
        self._apply_state(AppState.IDLE.value, "Ready")

    def _handle_playback_finished(self) -> None:
        self._apply_state(AppState.IDLE.value, "Ready")

    def _handle_playback_failed(self, message: str) -> None:
        self.error_changed.emit(message)
        self._apply_state(AppState.IDLE.value, "Ready")

    def _apply_state(self, state: str, status: str) -> None:
        self._set_state(AppState(state))
        self.status_changed.emit(status)

    def _set_state(self, state: AppState) -> None:
        self.state = state
        self.state_changed.emit(state.value)


class AudioCheckDialog(QDialog):
    def __init__(self, controller: AudioCheckController, parent=None) -> None:
        super().__init__(parent)
        self.controller = controller

        self.setWindowTitle("Audio Check")
        self.resize(560, 360)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(12)

        self.status_label = QLabel("Ready", self)
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)

        self.record_button = QPushButton("Hold To Record Check", self)
        self.record_button.setMinimumHeight(64)
        self.record_button.pressed.connect(self.controller.start_recording)
        self.record_button.released.connect(self.controller.stop_recording)

        self.transcript_box = QTextEdit(self)
        self.transcript_box.setReadOnly(True)
        self.transcript_box.setPlaceholderText("Transcription will appear here before playback.")

        self.error_label = QLabel("", self)
        self.error_label.setWordWrap(True)
        self.error_label.setStyleSheet("color: #b00020;")

        layout.addWidget(self.status_label)
        layout.addWidget(self.record_button)
        layout.addWidget(QLabel("Transcript", self))
        layout.addWidget(self.transcript_box, 1)
        layout.addWidget(self.error_label)

        self.controller.status_changed.connect(self.status_label.setText)
        self.controller.transcript_changed.connect(self.transcript_box.setPlainText)
        self.controller.error_changed.connect(self.error_label.setText)
        self.controller.state_changed.connect(self._apply_state)

        self._apply_state("idle")

    def _apply_state(self, state: str) -> None:
        can_record = state == "idle" or state == "recording"
        self.record_button.setEnabled(can_record)

        if state == "recording":
            self.record_button.setText("Release To Transcribe")
        elif state == "transcribing":
            self.record_button.setText("Transcribing...")
        elif state == "synthesizing":
            self.record_button.setText("Generating Speech...")
        elif state == "speaking":
            self.record_button.setText("Playing...")
        else:
            self.record_button.setText("Hold To Record Check")
