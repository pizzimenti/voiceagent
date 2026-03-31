from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QLabel,
    QMainWindow,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from voiceagent.audio_check import AudioCheckController, AudioCheckDialog
from voiceagent.controller import VoiceController


class MainWindow(QMainWindow):
    def __init__(self, controller: VoiceController, audio_check_controller: AudioCheckController) -> None:
        super().__init__()
        self.controller = controller
        self.audio_check_controller = audio_check_controller
        self.audio_check_dialog: QDialog | None = None
        self.setWindowTitle("Voice Agent")
        self.resize(760, 520)

        root = QWidget(self)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(12)

        self.status_label = QLabel("Ready", root)
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)

        self.push_to_talk_button = QPushButton("Hold To Talk", root)
        self.push_to_talk_button.setMinimumHeight(72)
        self.push_to_talk_button.pressed.connect(self.controller.start_recording)
        self.push_to_talk_button.released.connect(self.controller.stop_recording)

        self.audio_check_button = QPushButton("Audio Check", root)
        self.audio_check_button.clicked.connect(self._open_audio_check)

        self.transcript_box = QTextEdit(root)
        self.transcript_box.setReadOnly(True)
        self.transcript_box.setPlaceholderText("Transcript will appear here.")

        self.response_box = QTextEdit(root)
        self.response_box.setReadOnly(True)
        self.response_box.setPlaceholderText("Assistant response will appear here.")

        self.error_label = QLabel("", root)
        self.error_label.setWordWrap(True)
        self.error_label.setStyleSheet("color: #b00020;")

        layout.addWidget(self.status_label)
        layout.addWidget(self.push_to_talk_button)
        layout.addWidget(self.audio_check_button)
        layout.addWidget(QLabel("Transcript", root))
        layout.addWidget(self.transcript_box, 1)
        layout.addWidget(QLabel("Response", root))
        layout.addWidget(self.response_box, 1)
        layout.addWidget(self.error_label)

        self.setCentralWidget(root)

        self.controller.status_changed.connect(self.status_label.setText)
        self.controller.transcript_changed.connect(self.transcript_box.setPlainText)
        self.controller.response_changed.connect(self.response_box.setPlainText)
        self.controller.error_changed.connect(self.error_label.setText)
        self.controller.state_changed.connect(self._apply_state)

        self._apply_state("idle")

    def closeEvent(self, event) -> None:  # noqa: N802
        self.controller.shutdown()
        self.audio_check_controller.shutdown()
        if self.audio_check_dialog is not None:
            self.audio_check_dialog.close()
        super().closeEvent(event)

    def _apply_state(self, state: str) -> None:
        can_record = state == "idle" or state == "recording"
        self.push_to_talk_button.setEnabled(can_record)

        if state == "recording":
            self.push_to_talk_button.setText("Release To Send")
        elif state == "transcribing":
            self.push_to_talk_button.setText("Transcribing...")
        elif state == "thinking":
            self.push_to_talk_button.setText("Thinking...")
        elif state == "synthesizing":
            self.push_to_talk_button.setText("Synthesizing...")
        elif state == "speaking":
            self.push_to_talk_button.setText("Speaking...")
        else:
            self.push_to_talk_button.setText("Hold To Talk")

    def _open_audio_check(self) -> None:
        if self.audio_check_dialog is None:
            self.audio_check_dialog = AudioCheckDialog(self.audio_check_controller, self)

        self.audio_check_dialog.show()
        self.audio_check_dialog.raise_()
        self.audio_check_dialog.activateWindow()
