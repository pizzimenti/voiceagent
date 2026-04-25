from __future__ import annotations

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QSignalSpy

from voiceagent.catalog_model import CatalogModel


class InstalledStub:
    """Dict-backed unary predicate; mutate _state to flip per-name installed flags."""

    def __init__(self, initial: dict[str, bool] | None = None) -> None:
        self._state: dict[str, bool] = dict(initial or {})

    def __call__(self, name: str) -> bool:
        return bool(self._state.get(name, False))

    def set(self, name: str, value: bool) -> None:
        self._state[name] = value


@pytest.fixture
def names() -> list[str]:
    return ["alpha", "beta", "gamma"]


@pytest.fixture
def stub() -> InstalledStub:
    return InstalledStub({"alpha": True, "beta": False, "gamma": True})


@pytest.fixture
def model(qtbot, names, stub):
    return CatalogModel(names, stub)


def test_row_count_matches_constructor_list_length(model, names):
    assert model.rowCount() == len(names)


def test_data_name_role_returns_constructor_name(model, names):
    for row, expected in enumerate(names):
        idx = model.index(row, 0)
        assert model.data(idx, CatalogModel.NameRole) == expected


def test_data_installed_role_uses_injected_callback(model, stub, names):
    for row, name in enumerate(names):
        idx = model.index(row, 0)
        assert model.data(idx, CatalogModel.InstalledRole) is stub(name)
    # Flip the predicate; subsequent reads should reflect the new state immediately.
    stub.set("beta", True)
    assert model.data(model.index(1, 0), CatalogModel.InstalledRole) is True


def test_data_display_role_falls_through_to_name(model, names):
    idx = model.index(0, 0)
    assert model.data(idx, Qt.ItemDataRole.DisplayRole) == names[0]


def test_refresh_installed_emits_data_changed_for_matching_row(model):
    spy = QSignalSpy(model.dataChanged)
    model.refresh_installed("beta")
    assert spy.count() == 1
    top_left, bottom_right, roles = spy.at(0)
    assert top_left.row() == 1
    assert bottom_right.row() == 1
    assert list(roles) == [CatalogModel.InstalledRole]


def test_refresh_installed_unknown_name_does_not_emit(model):
    spy = QSignalSpy(model.dataChanged)
    model.refresh_installed("nonexistent")
    assert spy.count() == 0


def test_refresh_all_emits_single_data_changed_spanning_all_rows(model, names):
    spy = QSignalSpy(model.dataChanged)
    model.refresh_all()
    assert spy.count() == 1
    top_left, bottom_right, roles = spy.at(0)
    assert top_left.row() == 0
    assert bottom_right.row() == len(names) - 1
    assert list(roles) == [CatalogModel.InstalledRole]


def test_refresh_all_with_empty_list_does_not_emit(qtbot):
    empty_model = CatalogModel([], lambda _name: False)
    spy = QSignalSpy(empty_model.dataChanged)
    empty_model.refresh_all()
    assert spy.count() == 0


def test_data_invalid_row_returns_none(model):
    from PySide6.QtCore import QModelIndex

    assert model.data(QModelIndex(), CatalogModel.NameRole) is None
