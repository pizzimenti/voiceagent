"""KokoroTtsService behavior tests.

Kokoro is a single-bundle engine: `kokoro-v1.0.onnx` + `voices-v1.0.bin`
are one logical install holding all 54 voices, so the catalog lists every
voice up front and installing any one of them fetches the shared pair.

These tests never download real bytes or run real inference — the
`AriaDownloader` is replaced with a stub that plants files, and a fake
`kokoro_onnx` module is injected into `sys.modules` for the synthesize
roundtrip. They therefore run without `pip install voiceagent[kokoro]`.
`numpy` (used by `_write_wav`) is a transitive dep of the base install.
"""

from __future__ import annotations

import sys
import types
import wave
from pathlib import Path

import pytest

from voiceagent.services.kokoro_tts import (
    _DEFAULT_BUNDLED_VOICES,
    _DEFAULT_VOICE,
    KokoroTtsService,
)


# ---------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------


@pytest.fixture
def model_root(tmp_path: Path) -> Path:
    root = tmp_path / "kokoro-model"
    root.mkdir(parents=True, exist_ok=True)
    return root


@pytest.fixture
def service(model_root: Path) -> KokoroTtsService:
    return KokoroTtsService(model_root=model_root)


def _plant_bundle(model_root: Path) -> None:
    (model_root / KokoroTtsService.MODEL_FILENAME).write_bytes(b"onnx-bytes")
    (model_root / KokoroTtsService.VOICES_FILENAME).write_bytes(b"voice-bytes")


class _FakeDownloader:
    """Stub for AriaDownloader: records requested URLs and materializes
    each destination so `_bundle_available()` flips True afterward."""

    def __init__(self) -> None:
        self.requested: list[str] = []

    def get_remote_size(self, url: str) -> int:
        return 123

    def download(self, files, progress_callback=None) -> None:
        for f in files:
            f.destination.parent.mkdir(parents=True, exist_ok=True)
            f.destination.write_bytes(b"downloaded")
            self.requested.append(f.url)


def _install_fake_kokoro(monkeypatch) -> dict:
    """Inject a fake `kokoro_onnx` module. Returns a dict the fake
    populates with the last `create()` kwargs so tests can assert the
    per-voice language code was passed through."""
    import numpy as np

    captured: dict = {}

    class FakeKokoro:
        def __init__(self, model_path, voices_path):
            self.model_path = model_path
            self.voices_path = voices_path
            self.voices = list(_DEFAULT_BUNDLED_VOICES)

        def create(self, text, voice, speed=1.0, lang="en-us"):
            captured.update(text=text, voice=voice, speed=speed, lang=lang)
            return np.linspace(-0.5, 0.5, 2400, dtype=np.float32), 24_000

    mod = types.ModuleType("kokoro_onnx")
    mod.Kokoro = FakeKokoro
    monkeypatch.setitem(sys.modules, "kokoro_onnx", mod)
    return captured


# ---------------------------------------------------------------------
# catalog
# ---------------------------------------------------------------------


def test_catalog_lists_all_bundled_voices(service):
    items = service.available_items()
    assert items == list(_DEFAULT_BUNDLED_VOICES)
    # The v1.0 bundle ships 54 voices across nine languages.
    assert len(items) == 54


def test_catalog_available_before_download(service):
    # Every voice is "managed" / "downloadable" up front because the
    # bundle (which contains them all) is the unit of install.
    assert service.is_item_managed("af_heart")
    assert service.is_item_downloadable("zm_yunyang")


# ---------------------------------------------------------------------
# bundle availability + sidecar trap
# ---------------------------------------------------------------------


def test_bundle_unavailable_until_both_files_present(service, model_root):
    assert service.is_available is False
    (model_root / KokoroTtsService.MODEL_FILENAME).write_bytes(b"onnx")
    # Only one of the two files — still not available.
    assert service.is_available is False
    (model_root / KokoroTtsService.VOICES_FILENAME).write_bytes(b"voices")
    assert service.is_available is True


def test_stale_aria2_sidecar_blocks_availability(service, model_root):
    _plant_bundle(model_root)
    assert service.is_available is True
    # An interrupted download leaves `<file>.aria2`; the partial file is
    # poison for ONNX Runtime, so availability must report False.
    sidecar = model_root / f"{KokoroTtsService.MODEL_FILENAME}.aria2"
    sidecar.write_bytes(b"control")
    assert service.is_available is False
    assert service.is_item_available("af_heart") is False


def test_is_engine_ready_tracks_bundle(service, model_root):
    assert service.is_engine_ready is False
    _plant_bundle(model_root)
    assert service.is_engine_ready is True


# ---------------------------------------------------------------------
# selection
# ---------------------------------------------------------------------


def test_default_selected_item(service):
    assert service.selected_item == _DEFAULT_VOICE


def test_set_selected_item_falls_back_to_default_when_empty(service):
    service.set_selected_item("am_michael")
    assert service.selected_item == "am_michael"
    service.set_selected_item(None)
    assert service.selected_item == _DEFAULT_VOICE


@pytest.mark.parametrize(
    "voice,expected_lang",
    [
        ("af_heart", "en-us"),
        ("am_michael", "en-us"),
        ("bf_emma", "en-gb"),
        ("ef_dora", "es"),
        ("ff_siwis", "fr-fr"),
        ("hf_alpha", "hi"),
        ("if_sara", "it"),
        ("jf_alpha", "ja"),
        ("pf_dora", "pt-br"),
        ("zf_xiaoxiao", "cmn"),
        ("weird_unknown", "en-us"),  # unknown prefix → default
    ],
)
def test_lang_for_voice(service, voice, expected_lang):
    assert service._lang_for_voice(voice) == expected_lang


# ---------------------------------------------------------------------
# download / remove
# ---------------------------------------------------------------------


def test_download_fetches_both_bundle_files(service, model_root):
    fake = _FakeDownloader()
    service.downloader = fake
    service.download_item("af_heart")
    assert any(KokoroTtsService.MODEL_FILENAME in u for u in fake.requested)
    assert any(KokoroTtsService.VOICES_FILENAME in u for u in fake.requested)
    assert service.is_available is True


def test_download_any_voice_fetches_same_bundle(service, model_root):
    fake = _FakeDownloader()
    service.downloader = fake
    # A different voice name maps to the identical two-file fetch.
    service.download_item("zm_yunyang")
    assert len(fake.requested) == 2
    assert service.is_available is True


def test_download_noop_when_bundle_present(service, model_root):
    _plant_bundle(model_root)
    fake = _FakeDownloader()
    service.downloader = fake
    service.download_item("af_heart")
    assert fake.requested == []


def test_download_selected_item_uses_current_voice(service):
    fake = _FakeDownloader()
    service.downloader = fake
    service.set_selected_item("bf_lily")
    service.download_selected_item()
    assert len(fake.requested) == 2


def test_remove_deletes_bundle_and_sidecars(service, model_root):
    _plant_bundle(model_root)
    (model_root / f"{KokoroTtsService.MODEL_FILENAME}.aria2").write_bytes(b"c")
    service.remove_item("af_heart")
    assert not (model_root / KokoroTtsService.MODEL_FILENAME).exists()
    assert not (model_root / KokoroTtsService.VOICES_FILENAME).exists()
    assert not (model_root / f"{KokoroTtsService.MODEL_FILENAME}.aria2").exists()
    assert service.is_available is False


def test_artifact_paths(service, model_root):
    paths = service.artifact_paths("af_heart")
    assert paths == [
        model_root / KokoroTtsService.MODEL_FILENAME,
        model_root / KokoroTtsService.VOICES_FILENAME,
    ]


def test_artifact_manifest_is_empty(service):
    # No machine-readable per-file checksums upstream — layers 1 + 4 only.
    assert service.artifact_manifest("af_heart") == {}


# ---------------------------------------------------------------------
# synthesize roundtrip
# ---------------------------------------------------------------------


def test_synthesize_raises_when_not_downloaded(service):
    with pytest.raises(RuntimeError, match="not downloaded"):
        service.synthesize("hello")


def test_synthesize_writes_valid_wav(service, model_root, monkeypatch):
    _plant_bundle(model_root)
    captured = _install_fake_kokoro(monkeypatch)
    service.set_selected_item("af_heart")

    out = service.synthesize("Hello from Kokoro.")
    assert out is not None
    assert out.exists()
    with wave.open(str(out), "rb") as w:
        assert w.getnchannels() == 1
        assert w.getsampwidth() == 2
        assert w.getframerate() == 24_000
        assert w.getnframes() > 0
    out.unlink(missing_ok=True)
    # The selected voice + its mapped language reached the engine.
    assert captured["voice"] == "af_heart"
    assert captured["lang"] == "en-us"


def test_synthesize_passes_per_voice_language(service, model_root, monkeypatch):
    _plant_bundle(model_root)
    captured = _install_fake_kokoro(monkeypatch)
    service.set_selected_item("ef_dora")  # Spanish voice
    out = service.synthesize("Hola")
    assert captured["lang"] == "es"
    if out:
        out.unlink(missing_ok=True)


def test_loaded_voice_names_overlay_after_synth(service, model_root, monkeypatch):
    _plant_bundle(model_root)
    _install_fake_kokoro(monkeypatch)
    # Before load, the static catalog is returned.
    assert service._loaded_voice_names() is None
    service.synthesize("warm up")
    # After load, available_items reflects the live `kokoro.voices`.
    live = service._loaded_voice_names()
    assert live is not None
    assert "af_heart" in live
