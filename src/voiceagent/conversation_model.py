from __future__ import annotations

from PySide6.QtCore import (
    QAbstractListModel,
    QByteArray,
    QModelIndex,
    QObject,
    Qt,
)


class ConversationModel(QAbstractListModel):
    MessageRole = Qt.ItemDataRole.UserRole + 1
    LevelRole = Qt.ItemDataRole.UserRole + 2
    TextRole = Qt.ItemDataRole.UserRole + 3
    ReplayableRole = Qt.ItemDataRole.UserRole + 4
    BubbleStateRole = Qt.ItemDataRole.UserRole + 5
    TurnPendingRole = Qt.ItemDataRole.UserRole + 6
    TimestampLabelRole = Qt.ItemDataRole.UserRole + 7

    _ROLE_NAMES = {
        MessageRole: QByteArray(b"messageRole"),
        LevelRole: QByteArray(b"level"),
        TextRole: QByteArray(b"text"),
        ReplayableRole: QByteArray(b"replayable"),
        BubbleStateRole: QByteArray(b"bubbleState"),
        TurnPendingRole: QByteArray(b"turnPending"),
        TimestampLabelRole: QByteArray(b"timestampLabel"),
    }
    _ROLE_KEYS = {
        MessageRole: "role",
        LevelRole: "level",
        TextRole: "text",
        ReplayableRole: "replayable",
        BubbleStateRole: "bubbleState",
        TurnPendingRole: "turnPending",
        TimestampLabelRole: "timestampLabel",
    }

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._messages: list[dict[str, object]] = []

    def rowCount(self, parent: QModelIndex | None = None) -> int:  # noqa: N802
        if parent is not None and parent.isValid():
            return 0
        return len(self._messages)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> object:
        if not index.isValid() or index.row() < 0 or index.row() >= len(self._messages):
            return None
        if role == Qt.ItemDataRole.DisplayRole:
            role = self.TextRole
        key = self._ROLE_KEYS.get(role)
        if key is None:
            return None
        return self._messages[index.row()].get(key)

    def roleNames(self) -> dict[int, QByteArray]:  # noqa: N802
        return self._ROLE_NAMES

    def message(self, index: int) -> dict[str, object] | None:
        if index < 0 or index >= len(self._messages):
            return None
        return self._messages[index]

    def append_message(self, message: dict[str, object]) -> int:
        index = len(self._messages)
        self.beginInsertRows(QModelIndex(), index, index)
        self._messages.append(message)
        self.endInsertRows()
        return index

    def update_message(self, index: int, **updates: object) -> None:
        if index < 0 or index >= len(self._messages):
            return
        message = self._messages[index]
        changed_roles: list[int] = []
        for key, value in updates.items():
            if message.get(key) == value:
                continue
            message[key] = value
            for role, role_key in self._ROLE_KEYS.items():
                if role_key == key:
                    changed_roles.append(role)
                    break
        if changed_roles:
            model_index = self.index(index, 0)
            self.dataChanged.emit(model_index, model_index, changed_roles)

    def remove_message(self, index: int) -> None:
        if index < 0 or index >= len(self._messages):
            return
        self.beginRemoveRows(QModelIndex(), index, index)
        self._messages.pop(index)
        self.endRemoveRows()

    def find_message_index(
        self,
        role: str,
        bubble_state: str | None = None,
        turn_pending: bool | None = None,
    ) -> int:
        for index in range(len(self._messages) - 1, -1, -1):
            message = self._messages[index]
            if message.get("role") != role:
                continue
            if bubble_state is not None and message.get("bubbleState") != bubble_state:
                continue
            if turn_pending is not None and bool(message.get("turnPending")) != turn_pending:
                continue
            return index
        return -1
