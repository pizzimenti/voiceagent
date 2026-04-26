"""TTS `is_item_managed` / `is_item_downloadable` semantics.

Distinguishes three row shapes the catalog has to render correctly:

* **Managed (in cache):** the voice is in `known_voice_names()` —
  either already on disk or in the cached `voices.json`. Both
  predicates are True.
* **Voice-name shaped, not in cache:** a configured `TTS_MODEL=<voice>`
  on first run before the deferred `voices.json` fetch lands. The
  name is resolvable via Piper's URL convention, so the row stays
  installable; `managed` is False, `downloadable` is True.
* **Path-shaped:** the user pointed `TTS_MODEL` at a file. Voice
  Agent does not own the lifecycle; both predicates are False.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from voiceagent.services.tts import PiperTtsService


@pytest.fixture
def empty_root(tmp_path: Path) -> Path:
    # No `voices.json`, no on-disk voices.
    return tmp_path


@pytest.fixture
def service(monkeypatch, empty_root: Path) -> PiperTtsService:
    monkeypatch.setattr(
        "voiceagent.services.tts.default_tts_model_root", lambda: empty_root
    )
    return PiperTtsService(command=["piper"], model_path=None)


def test_voice_name_not_in_cache_is_downloadable_but_not_managed(service):
    name = "en_US-lessac-medium"
    assert service.is_item_managed(name) is False
    assert service.is_item_downloadable(name) is True


def test_path_shaped_value_is_neither_managed_nor_downloadable(service):
    name = "/home/user/voices/custom.onnx"
    assert service.is_item_managed(name) is False
    assert service.is_item_downloadable(name) is False


def test_voice_present_on_disk_is_managed_and_downloadable(monkeypatch, tmp_path):
    # Create the .onnx + .onnx.json sidecar pair on disk.
    (tmp_path / "en_US-amy-medium.onnx").write_text("payload")
    (tmp_path / "en_US-amy-medium.onnx.json").write_text("{}")
    monkeypatch.setattr(
        "voiceagent.services.tts.default_tts_model_root", lambda: tmp_path
    )
    service = PiperTtsService(command=["piper"], model_path=None)
    assert service.is_item_managed("en_US-amy-medium") is True
    assert service.is_item_downloadable("en_US-amy-medium") is True


def test_voice_in_cache_file_is_managed_and_downloadable(monkeypatch, tmp_path):
    (tmp_path / "voices.json").write_text(
        '{"en_GB-alan-medium": {}, "fr_FR-tom-medium": {}}'
    )
    monkeypatch.setattr(
        "voiceagent.services.tts.default_tts_model_root", lambda: tmp_path
    )
    service = PiperTtsService(command=["piper"], model_path=None)
    assert service.is_item_managed("en_GB-alan-medium") is True
    assert service.is_item_downloadable("en_GB-alan-medium") is True


def test_unknown_short_token_is_neither(service):
    # Doesn't look like a voice name (no double-dash), not a path.
    name = "garbage"
    assert service.is_item_managed(name) is False
    assert service.is_item_downloadable(name) is False
