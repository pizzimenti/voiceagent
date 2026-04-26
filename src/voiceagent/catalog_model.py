from __future__ import annotations

from types import MappingProxyType
from typing import ClassVar, Mapping, Protocol, runtime_checkable

from PySide6.QtCore import (
    QAbstractListModel,
    QByteArray,
    QModelIndex,
    QObject,
    Qt,
)


@runtime_checkable
class CatalogStateProvider(Protocol):
    """Per-row state surface CatalogModel reads on every `data()` call.

    Implementations live in window.py (`_CatalogStateAdapter`) and pull
    from the loader + backend services. Tests stub the protocol directly.
    """

    def is_installed(self, name: str) -> bool: ...
    def is_loading(self, name: str) -> bool: ...
    def progress(self, name: str) -> float: ...
    def is_downloadable(self, name: str) -> bool: ...
    def is_managed(self, name: str) -> bool: ...


class CatalogModel(QAbstractListModel):
    """Stable list of model names with per-row dynamic state roles."""

    NameRole = Qt.ItemDataRole.UserRole + 1
    InstalledRole = Qt.ItemDataRole.UserRole + 2
    LoadingRole = Qt.ItemDataRole.UserRole + 3
    ProgressRole = Qt.ItemDataRole.UserRole + 4
    DownloadableRole = Qt.ItemDataRole.UserRole + 5
    ManagedRole = Qt.ItemDataRole.UserRole + 6

    _ROLE_NAMES: ClassVar[dict[int, QByteArray]] = {
        NameRole: QByteArray(b"name"),
        InstalledRole: QByteArray(b"installed"),
        LoadingRole: QByteArray(b"loading"),
        ProgressRole: QByteArray(b"progress"),
        DownloadableRole: QByteArray(b"downloadable"),
        ManagedRole: QByteArray(b"managed"),
    }

    # Roles whose values can change after construction without a name-list
    # reset. `name` is excluded — names only change via `replace_names`,
    # which goes through `beginResetModel`.
    _DYNAMIC_ROLES: ClassVar[list[int]] = [
        InstalledRole,
        LoadingRole,
        ProgressRole,
        DownloadableRole,
        ManagedRole,
    ]
    # Read-only view handed to Qt so the framework cannot mutate the
    # class-level dict (PySide6 doesn't guarantee read-only semantics
    # on the returned mapping).
    _ROLE_NAMES_VIEW: ClassVar[Mapping[int, QByteArray]] = MappingProxyType(_ROLE_NAMES)

    def __init__(
        self,
        names,
        state_provider: CatalogStateProvider,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._names: list[str] = list(names)
        self._state = state_provider

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
            return bool(self._state.is_installed(name))
        if role == self.LoadingRole:
            return bool(self._state.is_loading(name))
        if role == self.ProgressRole:
            return float(self._state.progress(name))
        if role == self.DownloadableRole:
            return bool(self._state.is_downloadable(name))
        if role == self.ManagedRole:
            return bool(self._state.is_managed(name))
        return None

    def roleNames(self) -> Mapping[int, QByteArray]:  # noqa: N802
        return self._ROLE_NAMES_VIEW

    def refresh_row(self, name: str) -> None:
        """Emit dataChanged for all dynamic roles on the matching row.

        Use on loading-state transitions where `installed`, `loading`,
        `downloadable`, and `managed` may all flip together.
        """
        try:
            row = self._names.index(name)
        except ValueError:
            return
        idx = self.index(row, 0)
        self.dataChanged.emit(idx, idx, self._DYNAMIC_ROLES)

    def refresh_progress(self, name: str) -> None:
        """Emit dataChanged with ONLY the progress role.

        Aria2 ticks sub-second on fast links — the narrow role list
        keeps `installed`/`loading`-driven sibling bindings asleep
        between progress frames.
        """
        try:
            row = self._names.index(name)
        except ValueError:
            return
        idx = self.index(row, 0)
        self.dataChanged.emit(idx, idx, [self.ProgressRole])

    def refresh_all_rows(self) -> None:
        if not self._names:
            return
        self.dataChanged.emit(
            self.index(0, 0),
            self.index(len(self._names) - 1, 0),
            self._DYNAMIC_ROLES,
        )

    def replace_names(self, names) -> None:
        """Swap the full name list (e.g. after a deferred catalog refresh).

        Uses `beginResetModel` so QML rebuilds the delegates against the
        new list. Callers should only invoke this on genuine catalog
        changes (not on every per-row state flip) because a full reset
        disturbs `contentY` — see AGENTS.md's sticky-scroll guidance.
        """
        new_names = list(names)
        if new_names == self._names:
            return
        self.beginResetModel()
        self._names = new_names
        self.endResetModel()
