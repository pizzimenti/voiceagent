"""Per-instance cache for `PiperTtsService.known_voice_names`.

Without the cache, every per-row `is_item_managed` call (and via the
`_CatalogStateAdapter`, every `model.managed` / `model.downloadable`
read in QML) re-globs `model_root` and re-parses `voices.json`.

The cache is invalidated on every event that mutates the underlying
disk state: catalog refresh, voice download, voice delete.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from voiceagent.services.tts import PiperTtsService


@pytest.fixture
def model_root(tmp_path: Path) -> Path:
    return tmp_path


@pytest.fixture
def service(monkeypatch, model_root: Path) -> PiperTtsService:
    monkeypatch.setattr(
        "voiceagent.services.tts.default_tts_model_root", lambda: model_root
    )
    return PiperTtsService(command=["piper"], model_path=None)


def _add_voice_on_disk(model_root: Path, name: str) -> None:
    (model_root / f"{name}.onnx").write_text("payload")
    (model_root / f"{name}.onnx.json").write_text("{}")


def test_cache_starts_empty(service):
    assert service._known_voice_names_cache is None


def test_first_call_populates_cache(service, model_root):
    _add_voice_on_disk(model_root, "en_US-amy-medium")
    assert service.is_item_managed("en_US-amy-medium") is True
    assert service._known_voice_names_cache == {"en_US-amy-medium"}


def test_second_call_does_not_reread_disk(service, model_root):
    _add_voice_on_disk(model_root, "en_US-amy-medium")
    # First call populates cache from disk.
    service.is_item_managed("en_US-amy-medium")
    # Add a new voice on disk that the cache should NOT see.
    _add_voice_on_disk(model_root, "fr_FR-tom-medium")
    assert service.is_item_managed("fr_FR-tom-medium") is False
    # Cache still holds only the first.
    assert service._known_voice_names_cache == {"en_US-amy-medium"}


def test_invalidate_forces_reread(service, model_root):
    _add_voice_on_disk(model_root, "en_US-amy-medium")
    service.is_item_managed("en_US-amy-medium")
    _add_voice_on_disk(model_root, "fr_FR-tom-medium")
    service.invalidate_known_voice_names_cache()
    assert service._known_voice_names_cache is None
    assert service.is_item_managed("fr_FR-tom-medium") is True
    assert service._known_voice_names_cache == {"en_US-amy-medium", "fr_FR-tom-medium"}


def test_remove_item_invalidates_cache(service, model_root):
    _add_voice_on_disk(model_root, "en_US-amy-medium")
    service.is_item_managed("en_US-amy-medium")
    assert service._known_voice_names_cache == {"en_US-amy-medium"}
    service.remove_item("en_US-amy-medium")
    assert service._known_voice_names_cache is None
    # Subsequent read pulls fresh state — voice no longer on disk.
    assert service.is_item_managed("en_US-amy-medium") is False


def test_refresh_catalog_invalidates_cache(service, model_root, monkeypatch):
    _add_voice_on_disk(model_root, "en_US-amy-medium")
    service.is_item_managed("en_US-amy-medium")  # populates cache
    cached_before = set(service._known_voice_names_cache)

    # Stub the network fetch out — `_fetch_and_cache_voice_names` returns
    # set() on failure, which is the path we exercise here. The cache
    # invalidation is what we're testing, not the fetch itself.
    monkeypatch.setattr(
        PiperTtsService,
        "_fetch_and_cache_voice_names",
        classmethod(lambda _cls, _root: set()),
    )

    service.refresh_catalog()
    # `refresh_catalog` invalidates AFTER calling `refresh_remote_catalog`.
    # The next `is_item_managed` re-pulls disk state.
    assert service._known_voice_names_cache is None
    assert service.is_item_managed("en_US-amy-medium") is True
    assert service._known_voice_names_cache == cached_before
