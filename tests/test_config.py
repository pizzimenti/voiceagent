"""`AppConfig.from_env` — engine selector + chatterbox references root.

The env contract:

* `VOICEAGENT_TTS_ENGINE` selects between `piper` (default) and
  `chatterbox`. Whitespace-tolerant, case-insensitive. Anything else
  falls back silently to `piper` (and logs a warning).
* `VOICEAGENT_CHATTERBOX_REFERENCES_ROOT` points at the directory
  holding user-recorded `*.wav` reference clips. Defaults to
  `~/.local/share/voiceagent/tts/chatterbox/references` under the
  v0.12.1 hierarchical layout.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from voiceagent.config import AppConfig


# ---------------------------------------------------------------------
# environment hygiene
# ---------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    """Strip every env var `AppConfig.from_env` reads so each test
    starts from a clean slate. Tests that need a value monkeypatch it
    in explicitly."""
    for var in (
        "VOICEAGENT_TTS_ENGINE",
        "VOICEAGENT_CHATTERBOX_REFERENCES_ROOT",
        "VOICEAGENT_TTS_MODEL_ROOT",
        "VOICEAGENT_STT_MODEL_ROOT",
        "TTS_MODEL",
        "TTS_COMMAND",
        "TTS_EXTRA_ARGS",
        "LM_STUDIO_BASE_URL",
        "LM_STUDIO_MODEL",
        "LM_STUDIO_SYSTEM_PROMPT",
        "LM_STUDIO_TIMEOUT_SECONDS",
        "LM_STUDIO_LOAD_TIMEOUT_SECONDS",
        "WHISPER_MODEL",
        "WHISPER_DEVICE",
        "WHISPER_COMPUTE_TYPE",
        "VOICEAGENT_MAX_HISTORY_TURNS",
    ):
        monkeypatch.delenv(var, raising=False)


# ---------------------------------------------------------------------
# tts_engine
# ---------------------------------------------------------------------


def test_tts_engine_defaults_to_piper():
    config = AppConfig.from_env()
    assert config.tts_engine == "piper"


def test_tts_engine_chatterbox_round_trips(monkeypatch):
    monkeypatch.setenv("VOICEAGENT_TTS_ENGINE", "chatterbox")
    config = AppConfig.from_env()
    assert config.tts_engine == "chatterbox"


@pytest.mark.parametrize(
    "raw",
    [
        "  chatterbox  ",
        "CHATTERBOX",
        "Chatterbox",
        "\tchatterbox\n",
    ],
)
def test_tts_engine_whitespace_and_case_insensitive(monkeypatch, raw):
    monkeypatch.setenv("VOICEAGENT_TTS_ENGINE", raw)
    config = AppConfig.from_env()
    assert config.tts_engine == "chatterbox"


def test_tts_engine_invalid_falls_back_to_piper(monkeypatch, caplog):
    monkeypatch.setenv("VOICEAGENT_TTS_ENGINE", "bogus-engine")
    with caplog.at_level(logging.WARNING):
        config = AppConfig.from_env()
    # Falls back, does NOT raise.
    assert config.tts_engine == "piper"


def test_tts_engine_empty_string_falls_back_to_piper(monkeypatch):
    monkeypatch.setenv("VOICEAGENT_TTS_ENGINE", "   ")
    config = AppConfig.from_env()
    assert config.tts_engine == "piper"


# ---------------------------------------------------------------------
# chatterbox_references_root
# ---------------------------------------------------------------------


def test_chatterbox_references_root_default():
    config = AppConfig.from_env()
    expected = Path.home() / ".local/share/voiceagent/tts/chatterbox/references"
    assert Path(config.chatterbox_references_root) == expected


def test_chatterbox_references_root_override(monkeypatch, tmp_path):
    monkeypatch.setenv(
        "VOICEAGENT_CHATTERBOX_REFERENCES_ROOT",
        str(tmp_path / "custom-refs"),
    )
    config = AppConfig.from_env()
    assert Path(config.chatterbox_references_root) == (tmp_path / "custom-refs")


def test_chatterbox_references_root_expanduser(monkeypatch):
    monkeypatch.setenv(
        "VOICEAGENT_CHATTERBOX_REFERENCES_ROOT",
        "~/voiceagent-refs",
    )
    config = AppConfig.from_env()
    assert Path(config.chatterbox_references_root) == (
        Path.home() / "voiceagent-refs"
    )
