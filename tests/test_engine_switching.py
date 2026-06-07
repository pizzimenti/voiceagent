"""Engine selection wiring in `voiceagent.app.build_shared_services`.

Verifies the four expected branches:

1. `tts_engine == "piper"` (the default) → `PiperTtsService`.
2. `tts_engine == "chatterbox"` AND extras importable → `ChatterboxTtsService`.
3. `tts_engine == "chatterbox"` AND extras absent → falls back to
   `PiperTtsService` with a logged warning, never `ImportError`.
4. The Chatterbox service's `model_root` is rooted at
   `default_chatterbox_model_root()` (`<data>/tts/chatterbox/model/`),
   independent of the Piper-specific `tts_model_root`.
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


def _force_kokoro_extras(monkeypatch, *, present: bool) -> None:
    """Make `_kokoro_extras_available` deterministic regardless of what is
    actually installed. The probe is a single `find_spec("kokoro_onnx")`."""
    real_find_spec = importlib.util.find_spec

    def fake_find_spec(name, *args, **kwargs):
        if name == "kokoro_onnx":
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
    # Hard import — the previous `pytest.importorskip` here masked
    # wiring failures (renamed module, broken import chain, missing
    # symbol) by quietly skipping. The Chatterbox engine is part of
    # the v0.12 surface and any import regression should fail the
    # suite, not be tolerated as "not yet wired."
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


def test_chatterbox_model_root_uses_engine_scoped_helper(monkeypatch, tmp_path):
    """Chatterbox `model_root` must come from
    `default_chatterbox_model_root()` (engine-scoped, `<data>/tts/chatterbox/model/`),
    not from `config.tts_model_root` which is now Piper-specific.
    Patch the helper into `tmp_path` so the test never touches the
    user's real XDG data dir.
    """
    pytest.importorskip(
        "voiceagent.services.chatterbox_tts",
        reason="ChatterboxTtsService not yet wired in this branch",
    )
    _force_extras(monkeypatch, present=True)

    expected = tmp_path / "tts" / "chatterbox" / "model"
    # `build_shared_services` imports the helper inside the function
    # body (`from voiceagent.paths import default_chatterbox_model_root`),
    # so patching the source module is sufficient.
    monkeypatch.setattr(
        "voiceagent.paths.default_chatterbox_model_root",
        lambda: expected,
    )

    config = _make_config(tmp_path, tts_engine="chatterbox")
    _, tts, _, _ = build_shared_services(config)
    assert Path(tts.model_root).resolve() == expected.resolve()


def test_kokoro_when_extras_present(monkeypatch, tmp_path):
    # Hard import — a wiring regression (renamed module, broken import
    # chain) should fail the suite, not be silently skipped.
    from voiceagent.services.kokoro_tts import KokoroTtsService

    _force_kokoro_extras(monkeypatch, present=True)
    config = _make_config(tmp_path, tts_engine="kokoro")
    _, tts, _, _ = build_shared_services(config)
    assert isinstance(tts, KokoroTtsService)


def test_kokoro_falls_back_to_piper_when_extras_absent(
    monkeypatch, caplog, tmp_path
):
    _force_kokoro_extras(monkeypatch, present=False)
    config = _make_config(tmp_path, tts_engine="kokoro")
    with caplog.at_level(logging.WARNING):
        _, tts, _, _ = build_shared_services(config)
    assert isinstance(tts, PiperTtsService)
    joined = " ".join(rec.getMessage() for rec in caplog.records).lower()
    assert "kokoro" in joined


def test_kokoro_model_root_uses_engine_scoped_helper(monkeypatch, tmp_path):
    """Kokoro `model_root` must come from `default_kokoro_model_root()`
    (`<data>/tts/kokoro/model/`), not the Piper-specific `tts_model_root`.
    """
    from voiceagent.services.kokoro_tts import KokoroTtsService  # noqa: F401

    _force_kokoro_extras(monkeypatch, present=True)
    expected = tmp_path / "tts" / "kokoro" / "model"
    monkeypatch.setattr(
        "voiceagent.paths.default_kokoro_model_root",
        lambda: expected,
    )
    config = _make_config(tmp_path, tts_engine="kokoro")
    _, tts, _, _ = build_shared_services(config)
    assert Path(tts.model_root).resolve() == expected.resolve()


# ---------------------------------------------------------------------
# Runtime swap tests — exercise `MainWindow._perform_tts_engine_swap`
# end-to-end so per-engine voice memory + first-installed fallback
# remain visible to QA after future refactors.
# ---------------------------------------------------------------------


@pytest.fixture
def _swap_window(qapp, tmp_path, monkeypatch):
    """Build a MainWindow against fakes + temp Piper voice dir, with
    the QML load patched out so the headless test env doesn't need
    Kirigami modules. Returns the live `MainWindow` instance.
    """
    import voiceagent.window as window_mod
    from voiceagent.controller import VoiceController
    from voiceagent.model_loader import WhisperModelLoader
    from voiceagent.tts_loader import TtsVoiceLoader
    from voiceagent.services.tts import PiperTtsService
    from tests.fakes import (
        FakeRecorder, FakeChatClient, FakePlayer, FakeTranscriber,
    )

    # Temp dirs for the engines so we don't touch the real user state.
    tts_root = tmp_path / "tts-models"
    tts_root.mkdir()
    # Plant 4 fake Piper voices so available_items is non-empty.
    for name in ("voice-a", "voice-b", "voice-c", "voice-d"):
        (tts_root / f"{name}.onnx").write_bytes(b"fake")
        (tts_root / f"{name}.onnx.json").write_text("{}")

    refs_root = tmp_path / "chatterbox-references"
    refs_root.mkdir()

    monkeypatch.setenv("VOICEAGENT_TTS_MODEL_ROOT", str(tts_root))
    monkeypatch.setenv("VOICEAGENT_CHATTERBOX_REFERENCES_ROOT", str(refs_root))
    # Engine env stays unset so AppConfig.from_env defaults to piper —
    # mirrors the user's actual launch configuration.
    monkeypatch.delenv("VOICEAGENT_TTS_ENGINE", raising=False)

    _force_extras(monkeypatch, present=True)

    piper = PiperTtsService(
        command=["piper"], model_path=None, extra_args=[]
    )
    piper.model_root = tts_root
    transcriber = FakeTranscriber(model_root=tmp_path / "stt")
    controller = VoiceController(
        recorder=FakeRecorder(),
        transcriber=transcriber,
        chat_client=FakeChatClient(),
        tts_service=piper,
        player=FakePlayer(),
    )
    model_loader = WhisperModelLoader(transcriber)
    tts_loader = TtsVoiceLoader(piper)

    # Patch the QML-load gate: in this env Kirigami isn't available, so
    # `engine.load(...)` returns no rootObjects and MainWindow raises.
    # We don't need actual rendering for these state-machine tests.
    real_init = window_mod.MainWindow.__init__

    def _patched_init(self, *args, **kwargs):
        try:
            real_init(self, *args, **kwargs)
        except RuntimeError as exc:
            if "Failed to load QML" not in str(exc):
                raise
            self._window = None

    monkeypatch.setattr(window_mod.MainWindow, "__init__", _patched_init)

    win = window_mod.MainWindow(controller, model_loader, tts_loader)
    return win


def test_swap_chatterbox_then_back_to_piper_preserves_installed_voices(_swap_window):
    """Regression: after chatterbox→piper swap with no remembered voice,
    the user must see installed voices in the dropdown rather than the
    `displayText: "No installed TTS voices"` placeholder. The swap
    needs to call `_sync_installed_selections` so a sensible default
    is selected when `selected_tts_model_<engine>` is unset.
    """
    win = _swap_window

    # Initial: piper, 4 voices installed, none selected yet.
    assert win.tts_loader.tts_service.backend_name == "Piper"
    assert len(win.ttsOptions) == 4

    # Swap to chatterbox — empty references_root means empty catalog.
    win._perform_tts_engine_swap("chatterbox")
    assert win.tts_loader.tts_service.backend_name == "Chatterbox"
    assert list(win.ttsOptions) == []

    # Swap back to piper.
    win._perform_tts_engine_swap("piper")
    assert win.tts_loader.tts_service.backend_name == "Piper"

    # The catalog must still surface 4 installed voices …
    assert len(win.ttsOptions) == 4

    # … AND a voice must be selected so QML's
    # `displayText: currentIndex >= 0 ? currentText : "No installed …"`
    # branch lands on `currentText`, not the placeholder.
    selected = win.selectedTtsModel
    assert selected, (
        f"swap-to-piper left no voice selected (selectedTtsModel={selected!r}); "
        "ComboBox would render the misleading 'No installed TTS voices' "
        "placeholder text. _sync_installed_selections must run after the swap."
    )
    assert selected in win.ttsOptions


def test_startup_desync_kokoro_extras_missing_reverts_to_piper(
    _swap_window, monkeypatch
):
    """Regression for the startup desync path: if QSettings remembers
    `selected_tts_engine=kokoro` but the Kokoro extras are gone, startup
    must revert QSettings to piper — NOT swap into a Kokoro service whose
    first synth would fail with a deferred import error. The desync path
    bypasses the `selectTtsEngine` extras gate, so the guard has to live
    in `_resolve_startup_engine_desync` itself.
    """
    win = _swap_window
    # Live service is Piper (fixture default); pretend the user last
    # picked Kokoro, but its extras are now unavailable.
    win.settings.setValue("selected_tts_engine", "kokoro")
    _force_kokoro_extras(monkeypatch, present=False)

    win._resolve_startup_engine_desync()

    assert win.settings.value("selected_tts_engine", "", str) == "piper"
    assert win.tts_loader.tts_service.backend_name == "Piper"


def test_swap_to_chatterbox_with_empty_references_yields_empty_catalog(_swap_window):
    """When references_root is empty, swapping to Chatterbox produces
    an empty voice catalog. The user must explicitly record (option A)
    or import (option B) a reference clip — there is no bundled
    default. (Pre-1.0 design choice: a Piper-synthesized 'default'
    voice would only be a clone of an inferior model, not a meaningful
    default. The user provides their own clone source from minute one.)
    """
    win = _swap_window
    win._perform_tts_engine_swap("chatterbox")
    assert win.tts_loader.tts_service.backend_name == "Chatterbox"
    assert list(win.ttsOptions) == []
    assert win.selectedTtsModel == ""
