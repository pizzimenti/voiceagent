"""ChatterboxTtsService behavior tests.

The service is voice-cloning only — there are no built-in voices. The
catalog is the set of `*.wav` reference clips on disk under
`references_root`. The four ONNX model components live under
`model_root` and are downloaded once from
`ResembleAI/chatterbox-turbo-ONNX` on Hugging Face.

These tests stub out the heavy optional extras (`onnxruntime`,
`transformers`, `librosa`, `soundfile`, `huggingface_hub`) via
`monkeypatch` so they run without `pip install voiceagent[chatterbox]`.

If `ChatterboxTtsService` cannot be imported (parallel agent's source
work hasn't landed yet) the entire module is skipped — the contract
below is what the service is expected to satisfy when it lands.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest


chatterbox_tts = pytest.importorskip(
    "voiceagent.services.chatterbox_tts",
    reason="ChatterboxTtsService not yet wired in this branch",
)

ChatterboxTtsService = chatterbox_tts.ChatterboxTtsService


# ---------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------


@pytest.fixture
def model_root(tmp_path: Path) -> Path:
    root = tmp_path / "chatterbox-models"
    root.mkdir(parents=True, exist_ok=True)
    return root


@pytest.fixture
def references_root(tmp_path: Path) -> Path:
    root = tmp_path / "chatterbox-references"
    root.mkdir(parents=True, exist_ok=True)
    return root


@pytest.fixture
def service(model_root: Path, references_root: Path) -> "ChatterboxTtsService":
    return ChatterboxTtsService(
        model_root=model_root, references_root=references_root
    )


def _stub_extras(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the optional Chatterbox extras importable via dummy modules.

    We don't drive real inference in unit tests, so empty stubs are
    sufficient to get past the import-guard inside the service.
    """
    for name in ("onnxruntime", "transformers", "librosa", "soundfile"):
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))


# ---------------------------------------------------------------------
# catalog
# ---------------------------------------------------------------------


def test_empty_catalog_when_no_references(service):
    assert service.available_items() == []


def test_catalog_reflects_wav_files(model_root, references_root):
    (references_root / "alice.wav").write_bytes(b"RIFF....WAVE")
    (references_root / "bob.wav").write_bytes(b"RIFF....WAVE")
    # Non-WAV siblings must not bleed into the catalog.
    (references_root / "notes.txt").write_text("ignore me")

    svc = ChatterboxTtsService(
        model_root=model_root, references_root=references_root
    )
    assert svc.available_items() == ["alice", "bob"]


def test_catalog_sorted_stable(model_root, references_root):
    (references_root / "Charlie.wav").write_bytes(b"RIFF")
    (references_root / "alice.wav").write_bytes(b"RIFF")
    (references_root / "bob.wav").write_bytes(b"RIFF")
    svc = ChatterboxTtsService(
        model_root=model_root, references_root=references_root
    )
    items = svc.available_items()
    # Assert full set membership and stable ordering; the service uses
    # case-insensitive sort so `Alice` and `alice` cluster naturally for
    # users.
    assert items == sorted(items, key=str.lower)
    assert set(items) == {"Charlie", "alice", "bob"}


# ---------------------------------------------------------------------
# Two-layer state: per-voice (reference clip) vs per-engine (model).
# `is_item_available` is reference-clip-only; engine state is exposed
# separately via `is_engine_ready`. The catalog UI uses the per-voice
# check to decide Install/Remove visibility, while the engine banner
# above the catalog drives the model-download flow.
# ---------------------------------------------------------------------


def test_is_item_available_true_when_reference_clip_present(
    monkeypatch, service, references_root,
):
    (references_root / "alice.wav").write_bytes(b"RIFF")
    # Engine state is irrelevant to per-voice availability.
    monkeypatch.setattr(service, "_model_present", lambda: False, raising=False)
    assert service.is_item_available("alice") is True


def test_is_item_available_false_when_reference_clip_missing(monkeypatch, service):
    monkeypatch.setattr(service, "_model_present", lambda: True, raising=False)
    assert service.is_item_available("alice") is False


def test_is_engine_ready_tracks_model_presence(monkeypatch, service):
    monkeypatch.setattr(service, "_model_present", lambda: False, raising=False)
    assert service.is_engine_ready is False
    monkeypatch.setattr(service, "_model_present", lambda: True, raising=False)
    assert service.is_engine_ready is True


def test_is_item_downloadable_always_false(service, references_root):
    """Voices are user-supplied (mic record / file import) and never
    downloads — the per-voice Install button is always hidden in the
    Chatterbox catalog UI. The engine model has its own download
    affordance separate from the voice list.
    """
    assert service.is_item_downloadable("alice") is False
    (references_root / "alice.wav").write_bytes(b"RIFF")
    assert service.is_item_downloadable("alice") is False


# ---------------------------------------------------------------------
# selection
# ---------------------------------------------------------------------


def test_set_selected_item_updates_selection(service):
    service.set_selected_item("alice")
    assert service.selected_item == "alice"
    service.set_selected_item("bob")
    assert service.selected_item == "bob"


def test_constructor_seeds_initial_selection(model_root, references_root):
    svc = ChatterboxTtsService(
        model_root=model_root,
        references_root=references_root,
        selected_item="alice",
    )
    assert svc.selected_item == "alice"


# ---------------------------------------------------------------------
# synthesize guards
# ---------------------------------------------------------------------


def test_synthesize_without_reference_clip_raises(monkeypatch, service):
    """No reference clip selected → loud RuntimeError mentioning the
    voice / reference, not a confusing internal stack trace."""
    _stub_extras(monkeypatch)
    monkeypatch.setattr(service, "_model_present", lambda: True, raising=False)
    service.set_selected_item(None)
    with pytest.raises(RuntimeError) as exc:
        service.synthesize("hello world")
    msg = str(exc.value).lower()
    assert "voice" in msg or "reference" in msg


def test_synthesize_without_extras_raises(monkeypatch, service, references_root):
    """When the chatterbox extras are not installed, the service must
    raise a `RuntimeError` mentioning extras rather than letting an
    `ImportError` escape."""
    (references_root / "alice.wav").write_bytes(b"RIFF")
    service.set_selected_item("alice")
    monkeypatch.setattr(service, "_model_present", lambda: True, raising=False)
    # Guarantee the extras are NOT importable for this test.
    for name in ("onnxruntime", "transformers", "librosa", "soundfile"):
        monkeypatch.setitem(sys.modules, name, None)
    with pytest.raises(RuntimeError) as exc:
        service.synthesize("hello world")
    assert "extras" in str(exc.value).lower()


# ---------------------------------------------------------------------
# remove / download
# ---------------------------------------------------------------------


def test_remove_item_deletes_reference_wav_only(model_root, references_root):
    (references_root / "alice.wav").write_bytes(b"RIFF")
    (model_root / "language_model_q4.onnx").write_bytes(b"FAKE-MODEL")

    svc = ChatterboxTtsService(
        model_root=model_root, references_root=references_root
    )
    svc.remove_item("alice")

    assert not (references_root / "alice.wav").exists()
    # Model must NOT be removed alongside a single reference voice — the
    # 700 MB bundle is shared across every reference clip the user has.
    assert (model_root / "language_model_q4.onnx").exists()


def test_download_item_fetches_four_components(monkeypatch, service):
    """`download_item` must fetch the four ONNX components from
    `ResembleAI/chatterbox-turbo-ONNX` via `huggingface_hub.hf_hub_download`."""
    calls: list[dict[str, object]] = []

    def fake_hf_hub_download(repo_id, filename, **kwargs):
        calls.append({"repo_id": repo_id, "filename": filename, **kwargs})
        # The real API returns the local cache path; for unit-test
        # purposes any string is fine.
        return f"/tmp/fake-cache/{filename}"

    fake_module = types.ModuleType("huggingface_hub")
    fake_module.hf_hub_download = fake_hf_hub_download  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "huggingface_hub", fake_module)

    service.download_item("alice")

    # Four components: conditional_decoder, speech_encoder, embed_tokens,
    # language_model. Each downloaded with its dtype suffix (e.g. `_q4`).
    component_filenames = [str(c["filename"]) for c in calls]
    expected_components = {
        "conditional_decoder",
        "speech_encoder",
        "embed_tokens",
        "language_model",
    }
    found = {
        comp
        for comp in expected_components
        if any(comp in fn for fn in component_filenames)
    }
    assert found == expected_components, (
        f"missing components: {expected_components - found}; calls={component_filenames}"
    )
    # All calls target the documented HF repo.
    assert all(c["repo_id"] == ChatterboxTtsService.HF_REPO for c in calls)
