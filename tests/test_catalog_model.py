from __future__ import annotations

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QSignalSpy

from voiceagent.catalog_model import CatalogModel


class CatalogStateStub:
    """Dict-backed stub for the CatalogStateProvider Protocol.

    Per-name state defaults to (False, False, 0.0, True, True). The
    `installed`/`loading`/`progress` maps are mutated by tests; the
    `downloadable`/`managed` defaults match the current backend
    behavior (every catalog entry is both downloadable and managed).
    """

    def __init__(
        self,
        installed: dict[str, bool] | None = None,
        loading: dict[str, bool] | None = None,
        progress: dict[str, float] | None = None,
        downloadable: dict[str, bool] | None = None,
        managed: dict[str, bool] | None = None,
    ) -> None:
        self._installed = dict(installed or {})
        self._loading = dict(loading or {})
        self._progress = dict(progress or {})
        self._downloadable = dict(downloadable or {})
        self._managed = dict(managed or {})

    def is_installed(self, name: str) -> bool:
        return bool(self._installed.get(name, False))

    def is_loading(self, name: str) -> bool:
        return bool(self._loading.get(name, False))

    def progress(self, name: str) -> float:
        return float(self._progress.get(name, 0.0))

    def is_downloadable(self, name: str) -> bool:
        return bool(self._downloadable.get(name, True))

    def is_managed(self, name: str) -> bool:
        return bool(self._managed.get(name, True))

    def set_installed(self, name: str, value: bool) -> None:
        self._installed[name] = value

    def set_loading(self, name: str, value: bool) -> None:
        self._loading[name] = value

    def set_progress(self, name: str, value: float) -> None:
        self._progress[name] = value


@pytest.fixture
def names() -> list[str]:
    return ["alpha", "beta", "gamma"]


@pytest.fixture
def stub() -> CatalogStateStub:
    return CatalogStateStub(
        installed={"alpha": True, "beta": False, "gamma": True},
        loading={"beta": True},
        progress={"beta": 0.42},
    )


@pytest.fixture
def model(qtbot, names, stub):
    return CatalogModel(names, stub)


def test_row_count_matches_constructor_list_length(model, names):
    assert model.rowCount() == len(names)


def test_data_name_role_returns_constructor_name(model, names):
    for row, expected in enumerate(names):
        idx = model.index(row, 0)
        assert model.data(idx, CatalogModel.NameRole) == expected


def test_data_installed_role_uses_provider(model, stub, names):
    for row, name in enumerate(names):
        idx = model.index(row, 0)
        assert model.data(idx, CatalogModel.InstalledRole) is stub.is_installed(name)
    stub.set_installed("beta", True)
    assert model.data(model.index(1, 0), CatalogModel.InstalledRole) is True


def test_data_loading_role_uses_provider(model):
    assert model.data(model.index(1, 0), CatalogModel.LoadingRole) is True
    assert model.data(model.index(0, 0), CatalogModel.LoadingRole) is False


def test_data_progress_role_uses_provider(model):
    assert model.data(model.index(1, 0), CatalogModel.ProgressRole) == pytest.approx(0.42)
    assert model.data(model.index(0, 0), CatalogModel.ProgressRole) == 0.0


def test_data_downloadable_and_managed_default_true(model, names):
    for row in range(len(names)):
        idx = model.index(row, 0)
        assert model.data(idx, CatalogModel.DownloadableRole) is True
        assert model.data(idx, CatalogModel.ManagedRole) is True


def test_data_display_role_falls_through_to_name(model, names):
    idx = model.index(0, 0)
    assert model.data(idx, Qt.ItemDataRole.DisplayRole) == names[0]


def test_refresh_row_emits_data_changed_for_all_dynamic_roles(model):
    spy = QSignalSpy(model.dataChanged)
    model.refresh_row("beta")
    assert spy.count() == 1
    top_left, bottom_right, roles = spy.at(0)
    assert top_left.row() == 1
    assert bottom_right.row() == 1
    role_set = set(roles)
    # All dynamic roles fire together; name is never re-emitted.
    assert role_set == {
        CatalogModel.InstalledRole,
        CatalogModel.LoadingRole,
        CatalogModel.ProgressRole,
        CatalogModel.DownloadableRole,
        CatalogModel.ManagedRole,
    }


def test_refresh_row_unknown_name_does_not_emit(model):
    spy = QSignalSpy(model.dataChanged)
    model.refresh_row("nonexistent")
    assert spy.count() == 0


def test_refresh_progress_emits_only_progress_role(model):
    spy = QSignalSpy(model.dataChanged)
    model.refresh_progress("beta")
    assert spy.count() == 1
    top_left, bottom_right, roles = spy.at(0)
    assert top_left.row() == 1
    assert bottom_right.row() == 1
    # Narrow role list keeps installed/loading-driven sibling bindings asleep.
    assert list(roles) == [CatalogModel.ProgressRole]


def test_refresh_progress_unknown_name_does_not_emit(model):
    spy = QSignalSpy(model.dataChanged)
    model.refresh_progress("nonexistent")
    assert spy.count() == 0


def test_refresh_all_rows_spans_every_row(model, names):
    spy = QSignalSpy(model.dataChanged)
    model.refresh_all_rows()
    assert spy.count() == 1
    top_left, bottom_right, roles = spy.at(0)
    assert top_left.row() == 0
    assert bottom_right.row() == len(names) - 1
    assert set(roles) == {
        CatalogModel.InstalledRole,
        CatalogModel.LoadingRole,
        CatalogModel.ProgressRole,
        CatalogModel.DownloadableRole,
        CatalogModel.ManagedRole,
    }


def test_refresh_all_rows_with_empty_list_does_not_emit(qtbot):
    empty_model = CatalogModel([], CatalogStateStub())
    spy = QSignalSpy(empty_model.dataChanged)
    empty_model.refresh_all_rows()
    assert spy.count() == 0


def test_data_invalid_row_returns_none(model):
    from PySide6.QtCore import QModelIndex

    assert model.data(QModelIndex(), CatalogModel.NameRole) is None


def test_replace_names_swaps_underlying_list(model):
    model.replace_names(["delta", "epsilon"])
    assert model.rowCount() == 2
    assert model.data(model.index(0, 0), CatalogModel.NameRole) == "delta"
