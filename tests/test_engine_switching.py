"""Engine selection wiring in `voiceagent.app.build_shared_services`.

Verifies the four expected branches:

1. `tts_engine == "piper"` (the default) → `PiperTtsService`.
2. `tts_engine == "chatterbox"` AND extras importable → `ChatterboxTtsService`.
3. `tts_engine == "chatterbox"` AND extras absent → falls back to
   `PiperTtsService` with a logged warning, never `ImportError`.
4. The Chatterbox service's `model_root` is rooted at
   `<config.tts_model_root>/chatterbox`, not the bare TTS root.
"""

from __future__ import annotations

import importlib.util
import logging
from pathlib import Path

import pytest

from voiceagent.app import build_shared_services
from voiceagent.config import AppConfig
from voiceagent.services.tts import PiperTtsService


# ---------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------


def _make_config(
    tmp_path: Path,
    *,
    tts_engine: str = "piper",
) -> AppConfig:
    return AppConfig(
        lm_studio_base_url="http://127.0.0.1:1234/v1",
        lm_studio_model="",
        lm_studio_system_prompt="",
        lm_studio_timeout_seconds=10,
        lm_studio_load_timeout_seconds=300,
        whisper_model="tiny.en",
        whisper_device="auto",
        whisper_compute_type="auto",
        tts_command=["piper"],
        tts_model=None,
        tts_extra_args=[],
        stt_model_root=tmp_path / "stt",
        tts_model_root=tmp_path / "tts",
        chatterbox_references_root=tmp_path / "chatterbox-references",
        tts_engine=tts_engine,  # type: ignore[arg-type]
    )


@pytest.fixture(autouse=True)
def _stub_whisper(monkeypatch, tmp_path):
    """Avoid loading a real faster-whisper model in these tests.

    The substitute mirrors `FakeTranscriber` in `tests.fakes` — it
    must satisfy the catalog protocol that `WhisperModelLoader` /
    `ParallelItemLoader` reach for during construction (`is_available`,
    `available_items`, `is_item_available`, etc.), or `__init__` blows
    up on the first signal emission.
    """
    from tests.fakes import FakeTranscriber

    def _factory(*args, **kwargs):
        return FakeTranscriber(model_root=tmp_path / "stt")

    monkeypatch.setattr(
        "voiceagent.services.stt.WhisperTranscriber", _factory
    )


def _force_extras(monkeypatch, *, present: bool) -> None:
    """Make `_chatterbox_extras_available` deterministic regardless of
    what is actually installed in the test venv."""
    real_find_spec = importlib.util.find_spec
    targets = {"onnxruntime", "transformers", "librosa", "soundfile"}

    def fake_find_spec(name, *args, **kwargs):
        if name in targets:
            return object() if present else None
        return real_find_spec(name, *args, **kwargs)

    monkeypatch.setattr(importlib.util, "find_spec", fake_find_spec)


# ---------------------------------------------------------------------
# tests
# ---------------------------------------------------------------------


def test_default_engine_is_piper(tmp_path):
    config = _make_config(tmp_path, tts_engine="piper")
    _, tts, _, _ = build_shared_services(config)
    assert isinstance(tts, PiperTtsService)


def test_chatterbox_when_extras_present(monkeypatch, tmp_path):
    pytest.importorskip(
        "voiceagent.services.chatterbox_tts",
        reason="ChatterboxTtsService not yet wired in this branch",
    )
    from voiceagent.services.chatterbox_tts import ChatterboxTtsService

    _force_extras(monkeypatch, present=True)
    config = _make_config(tmp_path, tts_engine="chatterbox")
    _, tts, _, _ = build_shared_services(config)
    assert isinstance(tts, ChatterboxTtsService)


def test_chatterbox_falls_back_to_piper_when_extras_absent(
    monkeypatch, caplog, tmp_path
):
    _force_extras(monkeypatch, present=False)
    config = _make_config(tmp_path, tts_engine="chatterbox")
    with caplog.at_level(logging.WARNING):
        _, tts, _, _ = build_shared_services(config)
    assert isinstance(tts, PiperTtsService)
    # The warning must mention the engine name so logs are diagnosable.
    joined = " ".join(rec.getMessage() for rec in caplog.records).lower()
    assert "chatterbox" in joined


def test_chatterbox_model_root_is_under_tts_model_root(monkeypatch, tmp_path):
    pytest.importorskip(
        "voiceagent.services.chatterbox_tts",
        reason="ChatterboxTtsService not yet wired in this branch",
    )
    _force_extras(monkeypatch, present=True)
    config = _make_config(tmp_path, tts_engine="chatterbox")
    _, tts, _, _ = build_shared_services(config)
    expected = (tmp_path / "tts" / "chatterbox").resolve()
    actual = Path(tts.model_root).resolve()
    assert actual == expected
