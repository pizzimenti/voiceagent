"""Custom STT path support in WhisperTranscriber.

`WHISPER_MODEL=/path/to/model` becomes the active selection at startup
and now needs to surface in the catalog as a `managed=False,
downloadable=False` row alongside the managed Whisper models.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from voiceagent.services.stt import WhisperTranscriber


@pytest.fixture
def managed_name() -> str:
    return "tiny.en"


@pytest.fixture
def custom_path(tmp_path: Path) -> str:
    return str(tmp_path / "custom-whisper")


def test_managed_init_does_not_set_custom_path(managed_name):
    transcriber = WhisperTranscriber(managed_name)
    assert transcriber._custom_path is None
    assert transcriber.available_items() == list(
        WhisperTranscriber.MODEL_REPOSITORIES.keys()
    )


def test_path_init_sets_custom_path_and_appends_to_catalog(custom_path):
    transcriber = WhisperTranscriber(custom_path)
    assert transcriber._custom_path == custom_path
    catalog = transcriber.available_items()
    assert custom_path in catalog
    # Custom path appears once, after managed names.
    assert catalog.count(custom_path) == 1
    assert catalog[: len(WhisperTranscriber.MODEL_REPOSITORIES)] == list(
        WhisperTranscriber.MODEL_REPOSITORIES.keys()
    )


def test_managed_name_is_managed_and_downloadable(managed_name):
    transcriber = WhisperTranscriber(managed_name)
    assert transcriber.is_item_managed(managed_name) is True
    assert transcriber.is_item_downloadable(managed_name) is True


def test_custom_path_is_not_managed_or_downloadable(custom_path):
    transcriber = WhisperTranscriber(custom_path)
    assert transcriber.is_item_managed(custom_path) is False
    assert transcriber.is_item_downloadable(custom_path) is False


def test_set_model_name_swap_managed_to_path(managed_name, custom_path):
    transcriber = WhisperTranscriber(managed_name)
    assert transcriber._custom_path is None
    transcriber.set_model_name(custom_path)
    assert transcriber._custom_path == custom_path
    assert custom_path in transcriber.available_items()


def test_set_model_name_swap_path_to_managed(custom_path, managed_name):
    transcriber = WhisperTranscriber(custom_path)
    assert transcriber._custom_path == custom_path
    transcriber.set_model_name(managed_name)
    assert transcriber._custom_path is None
    assert transcriber.available_items() == list(
        WhisperTranscriber.MODEL_REPOSITORIES.keys()
    )


def test_set_model_name_swap_path_to_path(tmp_path):
    first = str(tmp_path / "first")
    second = str(tmp_path / "second")
    transcriber = WhisperTranscriber(first)
    assert transcriber._custom_path == first
    transcriber.set_model_name(second)
    assert transcriber._custom_path == second
    catalog = transcriber.available_items()
    assert second in catalog
    assert first not in catalog


def test_relative_string_with_slash_is_treated_as_path():
    # Relative path: not in MODEL_REPOSITORIES, contains "/" → custom.
    transcriber = WhisperTranscriber("subdir/model")
    assert transcriber._custom_path == "subdir/model"


def test_tilde_string_is_treated_as_path():
    # `~/foo` is path-shaped even before expanduser.
    transcriber = WhisperTranscriber("~/whisper-models/custom")
    assert transcriber._custom_path == "~/whisper-models/custom"


def test_bare_unknown_name_is_not_treated_as_path():
    # Bare token without separator: ambiguous between "custom path" and
    # "user typed an unknown managed name". We treat it as managed-but-
    # unknown (returns is_item_managed=False, but does NOT enter the
    # custom-path slot). This avoids polluting the catalog with junk
    # entries from typos in env vars or QSettings.
    transcriber = WhisperTranscriber("not-a-real-model")
    assert transcriber._custom_path is None
    assert "not-a-real-model" not in transcriber.available_items()


def test_empty_name_is_not_treated_as_path():
    transcriber = WhisperTranscriber("")
    assert transcriber._custom_path is None


def test_custom_path_is_available_when_directory_has_required_files(tmp_path):
    custom_dir = tmp_path / "whisper-custom"
    custom_dir.mkdir()
    for filename in WhisperTranscriber.REQUIRED_MODEL_FILES:
        (custom_dir / filename).write_text("payload")
    (custom_dir / "vocabulary.json").write_text("[]")

    transcriber = WhisperTranscriber(str(custom_dir))
    assert transcriber.is_item_available(str(custom_dir)) is True


def test_custom_path_not_available_when_files_missing(tmp_path):
    custom_dir = tmp_path / "whisper-incomplete"
    custom_dir.mkdir()
    transcriber = WhisperTranscriber(str(custom_dir))
    assert transcriber.is_item_available(str(custom_dir)) is False
