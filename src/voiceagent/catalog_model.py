from __future__ import annotations

from PySide6.QtCore import (
    QAbstractListModel,
    QByteArray,
    QModelIndex,
    QObject,
    Qt,
)


class CatalogModel(QAbstractListModel):
    """Stable list of model names. Only the per-row `installed` flag ever changes."""

    NameRole = Qt.ItemDataRole.UserRole + 1
    InstalledRole = Qt.ItemDataRole.UserRole + 2

    _ROLE_NAMES = {
        NameRole: QByteArray(b"name"),
        InstalledRole: QByteArray(b"installed"),
    }

    def __init__(self, names, is_installed, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._names: list[str] = list(names)
        self._is_installed = is_installed

    def rowCount(self, parent: QModelIndex | None = None) -> int:  # noqa: N802
        if parent is not None and parent.isValid():
            return 0
        return len(self._names)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> object:
        if not index.isValid() or index.row() < 0 or index.row() >= len(self._names):
            return None
        if role == Qt.ItemDataRole.DisplayRole:
            role = self.NameRole
        name = self._names[index.row()]
        if role == self.NameRole:
            return name
        if role == self.InstalledRole:
            return bool(self._is_installed(name))
        return None

    def roleNames(self) -> dict[int, QByteArray]:  # noqa: N802
        return self._ROLE_NAMES

    def refresh_installed(self, model_name: str) -> None:
        try:
            row = self._names.index(model_name)
        except ValueError:
            return
        idx = self.index(row, 0)
        self.dataChanged.emit(idx, idx, [self.InstalledRole])

    def refresh_all(self) -> None:
        if not self._names:
            return
        self.dataChanged.emit(
            self.index(0, 0),
            self.index(len(self._names) - 1, 0),
            [self.InstalledRole],
        )
