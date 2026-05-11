"""Tests for `voiceagent.paths.migrate_legacy_data_dirs`.

The migration moves v0.11.x flat dirs (`stt-models/`, `tts-models/`,
`chatterbox-references/`) under `~/.local/share/voiceagent/` into the
v0.12+ engine-scoped tree (`stt/whisper/`, `tts/piper/`,
`tts/chatterbox/references/`). It runs at startup and must be:

- Safe when nothing legacy is present (no-op).
- Safe to re-run (idempotent).
- Conservative when both old and new paths have content (skip + warn).
- Honors `VOICEAGENT_*_ROOT` env-var overrides (skip; user has chosen).
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from voiceagent import paths


@pytest.fixture
def fake_home(monkeypatch, tmp_path):
    """Redirect `default_data_root()` into `tmp_path` for isolation.

    `paths.default_data_root()` resolves to `<XDG_DATA_HOME>/voiceagent/`,
    which itself reads `XDG_DATA_HOME` from the env. Setting the env var
    to `tmp_path` means every legacy / new path the migration touches
    lands inside the test's tmpdir.
    """
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    # Clear any user env overrides so the env-skip rules are the
    # default ones for the test run.
    for var in (
        "VOICEAGENT_STT_MODEL_ROOT",
        "VOICEAGENT_TTS_MODEL_ROOT",
        "VOICEAGENT_CHATTERBOX_REFERENCES_ROOT",
    ):
        monkeypatch.delenv(var, raising=False)
    return tmp_path / "voiceagent"


def _seed(path: Path, marker: str) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / marker).write_text("seed")


def test_no_legacy_dirs_is_noop(fake_home):
    moved = paths.migrate_legacy_data_dirs()
    assert moved == []


def test_moves_stt_models_when_new_dir_empty(fake_home):
    legacy = fake_home / "stt-models"
    _seed(legacy / "tiny.en", "model.bin")
    moved = paths.migrate_legacy_data_dirs()

    new = paths.default_whisper_root()
    assert (legacy, new) in moved
    assert not legacy.exists()
    assert (new / "tiny.en" / "model.bin").exists()


def test_moves_tts_models_when_new_dir_empty(fake_home):
    legacy = fake_home / "tts-models"
    _seed(legacy, "voices.json")
    _seed(legacy / "en_US-amy-low.onnx".replace(".onnx", ""), "data")
    moved = paths.migrate_legacy_data_dirs()

    new = paths.default_piper_voices_root()
    assert (legacy, new) in moved
    assert not legacy.exists()
    assert (new / "voices.json").exists()


def test_moves_chatterbox_references_when_new_dir_empty(fake_home):
    legacy = fake_home / "chatterbox-references"
    _seed(legacy, "user-voice.wav")
    moved = paths.migrate_legacy_data_dirs()

    new = paths.default_chatterbox_references_root()
    assert (legacy, new) in moved
    assert not legacy.exists()
    assert (new / "user-voice.wav").exists()


def test_skips_when_new_dir_has_content(fake_home, caplog):
    """Don't clobber a populated new dir — both paths having content
    means the user post-upgrade started fresh and accumulated state.
    Surface the conflict via a warning so they can resolve manually.
    """
    legacy = fake_home / "stt-models"
    _seed(legacy / "tiny.en", "old-model.bin")
    new = paths.default_whisper_root()
    _seed(new / "large-v3", "new-model.bin")

    with caplog.at_level(logging.WARNING):
        moved = paths.migrate_legacy_data_dirs(
            logger=logging.getLogger("test"),
        )

    assert moved == []
    # Old data untouched
    assert (legacy / "tiny.en" / "old-model.bin").exists()
    # New data untouched
    assert (new / "large-v3" / "new-model.bin").exists()
    assert any("both" in rec.getMessage().lower() for rec in caplog.records)


def test_skips_when_env_override_set(fake_home, monkeypatch):
    """An explicit `VOICEAGENT_STT_MODEL_ROOT` means the user has
    chosen a custom path. Auto-migrating to the *default* would put
    the data somewhere they're not reading from — leave the legacy
    dir alone for manual handling.
    """
    legacy = fake_home / "stt-models"
    _seed(legacy / "tiny.en", "model.bin")
    monkeypatch.setenv("VOICEAGENT_STT_MODEL_ROOT", str(fake_home / "custom"))

    moved = paths.migrate_legacy_data_dirs()

    # STT pair skipped; legacy still on disk.
    assert (legacy / "tiny.en" / "model.bin").exists()
    new = paths.default_whisper_root()
    assert not new.exists()
    assert all(legacy_dst[0] != legacy for legacy_dst in moved)


def test_idempotent_second_run(fake_home):
    legacy = fake_home / "tts-models"
    _seed(legacy, "voices.json")

    first = paths.migrate_legacy_data_dirs()
    second = paths.migrate_legacy_data_dirs()

    assert first  # something moved
    assert second == []  # nothing to do on the second pass


def test_handles_all_three_legacy_dirs_in_one_run(fake_home):
    _seed(fake_home / "stt-models" / "tiny.en", "data")
    _seed(fake_home / "tts-models", "voices.json")
    _seed(fake_home / "chatterbox-references", "voice.wav")

    moved = paths.migrate_legacy_data_dirs()

    assert len(moved) == 3
    assert (paths.default_whisper_root() / "tiny.en" / "data").exists()
    assert (paths.default_piper_voices_root() / "voices.json").exists()
    assert (
        paths.default_chatterbox_references_root() / "voice.wav"
    ).exists()
