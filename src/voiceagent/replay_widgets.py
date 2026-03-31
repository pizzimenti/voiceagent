from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import QTimer, Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QTextEdit,
    QStyle,
    QVBoxLayout,
    QWidget,
)

from voiceagent.backends import TextToSpeechBackend
from voiceagent.services.playback import AudioPlayer


class ClickableTextEdit(QTextEdit):
    activated = Signal()

    def mousePressEvent(self, event) -> None:  # noqa: N802
        self.activated.emit()
        super().mousePressEvent(event)


class ReplayableTextBlock(QWidget):
    def __init__(
        self,
        title: str,
        tts_service: TextToSpeechBackend,
        player: AudioPlayer,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.tts_service = tts_service
        self.player = player
        self._logger = logging.getLogger(__name__)
        self._text = ""
        self._active = False
        self._audio_path: Path | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        self.title_label = QLabel(title, self)

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)

        self.text_box = ClickableTextEdit(self)
        self.text_box.setReadOnly(True)
        self.text_box.setPlaceholderText(f"{title} will appear here.")
        self.text_box.activated.connect(self._handle_text_activated)

        self.replay_button = QPushButton(self)
        self.replay_button.setFlat(True)
        self.replay_button.setFixedWidth(32)
        self.replay_button.setVisible(False)
        self.replay_button.clicked.connect(self._toggle_playback)
        self._set_play_icon()

        layout.addWidget(self.title_label)
        row.addWidget(self.text_box, 1)
        row.addWidget(self.replay_button, alignment=Qt.AlignmentFlag.AlignTop)
        layout.addLayout(row, 1)

        self.player.playback_started.connect(self._handle_playback_started)
        self.player.playback_finished.connect(self._handle_playback_finished)
        self.player.playback_failed.connect(self._handle_playback_failed)
        self.player.playback_state_changed.connect(self._handle_playback_state_changed)

    def set_text(self, text: str) -> None:
        self._text = text.strip()
        self.text_box.setPlainText(text)
        if not self._text:
            self._clear_replay_state()

    def _handle_text_activated(self) -> None:
        if not self._text:
            return

        self._active = True
        self.replay_button.setVisible(True)
        if self.player.is_playing or self.player.is_paused:
            return

        self._play_text()

    def _toggle_playback(self) -> None:
        if not self._active:
            self._active = True
            self.replay_button.setVisible(True)

        if self.player.is_playing:
            self.player.pause()
            return

        if self.player.is_paused:
            self.player.resume()
            return

        self._play_text()

    def _play_text(self) -> None:
        if not self.tts_service.enabled:
            self._logger.warning("Replay requested without a selected TTS voice")
            return

        self._audio_path = self.tts_service.synthesize(self._text)
        if self._audio_path is None:
            return

        self.player.play_file(self._audio_path)

    def _handle_playback_started(self, path: str) -> None:
        if not self._active or self._audio_path is None or str(self._audio_path) != path:
            return
        self._set_pause_icon()

    def _handle_playback_finished(self, path: str) -> None:
        if not self._active or self._audio_path is None or str(self._audio_path) != path:
            return
        self._clear_replay_state()

    def _handle_playback_failed(self, path: str, _message: str) -> None:
        if not self._active or self._audio_path is None or str(self._audio_path) != path:
            return
        self._clear_replay_state()

    def _handle_playback_state_changed(self, path: str, state_name: str) -> None:
        if not self._active or self._audio_path is None or str(self._audio_path) != path:
            return
        if state_name == "PausedState":
            self._set_play_icon()
        elif state_name == "PlayingState":
            self._set_pause_icon()

    def _clear_replay_state(self) -> None:
        self._active = False
        self._audio_path = None
        self.replay_button.setVisible(False)
        self._set_play_icon()

    def _set_play_icon(self) -> None:
        self.replay_button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_MediaPlay))

    def _set_pause_icon(self) -> None:
        self.replay_button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_MediaPause))


class ClickableLabel(QLabel):
    activated = Signal()

    def mousePressEvent(self, event) -> None:  # noqa: N802
        self.activated.emit()
        super().mousePressEvent(event)


class ConversationBubble(QWidget):
    def __init__(
        self,
        role: str,
        text: str,
        tts_service: TextToSpeechBackend,
        player: AudioPlayer,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.role = role
        self.tts_service = tts_service
        self.player = player
        self._logger = logging.getLogger(__name__)
        self._audio_path: Path | None = None
        self._active = False
        self._text = text.strip()
        self._replayable = role == "assistant"

        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)

        self.bubble = QFrame(self)
        self.bubble.setObjectName(f"{role}-bubble")
        self.bubble.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Minimum)
        self.bubble.setMaximumWidth(520)

        bubble_layout = QVBoxLayout(self.bubble)
        bubble_layout.setContentsMargins(14, 10, 14, 10)
        bubble_layout.setSpacing(0)

        self.text_label = ClickableLabel(self.bubble)
        self.text_label.setWordWrap(True)
        self.text_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.text_label.setText(self._text)
        if self._replayable:
            self.text_label.activated.connect(self._handle_text_activated)
        bubble_layout.addWidget(self.text_label)

        self.replay_button = QPushButton(self)
        self.replay_button.setFlat(True)
        self.replay_button.setFixedWidth(32)
        self.replay_button.setVisible(False)
        self.replay_button.clicked.connect(self._toggle_playback)
        self._set_play_icon()

        if role == "user":
            row.addStretch(1)
            row.addWidget(self.bubble)
        else:
            row.addWidget(self.bubble)
            row.addWidget(self.replay_button, alignment=Qt.AlignmentFlag.AlignBottom)
            row.addStretch(1)

        self.setStyleSheet(
            """
            QFrame#user-bubble {
                background-color: #1f8f4c;
                border-radius: 14px;
                color: white;
            }
            QFrame#assistant-bubble {
                background-color: #5b6167;
                border-radius: 14px;
                color: white;
            }
            """
        )

        self.player.playback_started.connect(self._handle_playback_started)
        self.player.playback_finished.connect(self._handle_playback_finished)
        self.player.playback_failed.connect(self._handle_playback_failed)
        self.player.playback_state_changed.connect(self._handle_playback_state_changed)

    def matches(self, role: str, text: str) -> bool:
        return self.role == role and self._text == text.strip()

    def _handle_text_activated(self) -> None:
        if not self._replayable or not self._text:
            return

        self._active = True
        self.replay_button.setVisible(True)
        if self.player.is_playing or self.player.is_paused:
            return
        self._play_text()

    def _toggle_playback(self) -> None:
        if not self._replayable:
            return
        if not self._active:
            self._active = True
            self.replay_button.setVisible(True)

        if self.player.is_playing:
            self.player.pause()
            return

        if self.player.is_paused:
            self.player.resume()
            return

        self._play_text()

    def _play_text(self) -> None:
        if not self.tts_service.enabled:
            self._logger.warning("Replay requested without a selected TTS voice")
            return

        self._audio_path = self.tts_service.synthesize(self._text)
        if self._audio_path is None:
            return

        self.player.play_file(self._audio_path)

    def _handle_playback_started(self, path: str) -> None:
        if not self._active or self._audio_path is None or str(self._audio_path) != path:
            return
        self._set_pause_icon()

    def _handle_playback_finished(self, path: str) -> None:
        if not self._active or self._audio_path is None or str(self._audio_path) != path:
            return
        self._clear_replay_state()

    def _handle_playback_failed(self, path: str, _message: str) -> None:
        if not self._active or self._audio_path is None or str(self._audio_path) != path:
            return
        self._clear_replay_state()

    def _handle_playback_state_changed(self, path: str, state_name: str) -> None:
        if not self._active or self._audio_path is None or str(self._audio_path) != path:
            return
        if state_name == "PausedState":
            self._set_play_icon()
        elif state_name == "PlayingState":
            self._set_pause_icon()

    def _clear_replay_state(self) -> None:
        self._active = False
        self._audio_path = None
        self.replay_button.setVisible(False)
        self._set_play_icon()

    def _set_play_icon(self) -> None:
        self.replay_button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_MediaPlay))

    def _set_pause_icon(self) -> None:
        self.replay_button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_MediaPause))


class ConversationView(QScrollArea):
    def __init__(self, tts_service: TextToSpeechBackend, player: AudioPlayer, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.tts_service = tts_service
        self.player = player
        self._last_bubble: ConversationBubble | None = None

        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)

        self.container = QWidget(self)
        self.container.setObjectName("conversation-container")
        self.container_layout = QVBoxLayout(self.container)
        self.container_layout.setContentsMargins(0, 0, 0, 0)
        self.container_layout.setSpacing(10)
        self.container_layout.addStretch(1)
        self.setWidget(self.container)

    def add_user_message(self, text: str) -> None:
        self._add_message("user", text)

    def add_assistant_message(self, text: str) -> None:
        self._add_message("assistant", text)

    def _add_message(self, role: str, text: str) -> None:
        cleaned = text.strip()
        if not cleaned:
            return
        if self._last_bubble is not None and self._last_bubble.matches(role, cleaned):
            return

        bubble = ConversationBubble(role, cleaned, self.tts_service, self.player, self.container)
        self.container_layout.insertWidget(self.container_layout.count() - 1, bubble)
        self._last_bubble = bubble
        QTimer.singleShot(0, self._scroll_to_bottom)

    def _scroll_to_bottom(self) -> None:
        self.verticalScrollBar().setValue(self.verticalScrollBar().maximum())
