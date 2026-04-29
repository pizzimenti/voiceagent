"""Tests for `PiperTtsService.is_available` vs `is_item_available`.

Previously `is_available` returned True as soon as an `.onnx` file
resolved — even when the paired `.onnx.json` config was missing.
`is_item_available(selected)` correctly required both. The mismatch
meant that a partial download (onnx but no json) reported ready via
the loader but crashed on first synthesis. The fix makes `is_available`
delegate to `is_item_available(self.model_path)` so the two are
always in lockstep.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Ensure the worktree's `src/` resolves before any editable install.
_HERE = Path(__file__).resolve().parent
_SRC = _HERE.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

# Headless Qt for any environment that didn't already set this.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from voiceagent.services.tts import PiperTtsService


def _make_service(model_root: Path, configured: str) -> PiperTtsService:
    service = PiperTtsService(command=["piper"], model_path=configured)
    service.model_root = model_root
    return service


def test_is_available_false_when_json_missing(tmp_path):
    """A voice with only the `.onnx` on disk (no config) must not be
    reported as available — this was the corrupt-Piper-voice case.
    """
    voice = "en_US-ryan-high"
    (tmp_path / f"{voice}.onnx").write_bytes(b"fake-onnx")
    # Intentionally: no `.onnx.json`.

    service = _make_service(tmp_path, voice)
    assert service.is_available is False
    assert service.is_item_available(voice) is False


def test_is_available_true_when_both_files_present(tmp_path):
    voice = "en_US-ryan-high"
    (tmp_path / f"{voice}.onnx").write_bytes(b"fake-onnx")
    (tmp_path / f"{voice}.onnx.json").write_text("{}", encoding="utf-8")

    service = _make_service(tmp_path, voice)
    assert service.is_available is True
    assert service.is_item_available(voice) is True


def test_is_available_agrees_with_is_item_available_on_selected(tmp_path):
    """`is_available` must be semantically identical to
    `is_item_available(selected)`, across all partial-download states.
    """
    voice = "en_US-ryan-high"
    service = _make_service(tmp_path, voice)

    # No files yet.
    assert service.is_available is False
    assert service.is_item_available(voice) is False
    assert service.is_available == service.is_item_available(voice)

    # Only the onnx.
    (tmp_path / f"{voice}.onnx").write_bytes(b"fake-onnx")
    assert service.is_available is False
    assert service.is_item_available(voice) is False
    assert service.is_available == service.is_item_available(voice)

    # Both files.
    (tmp_path / f"{voice}.onnx.json").write_text("{}", encoding="utf-8")
    assert service.is_available is True
    assert service.is_item_available(voice) is True
    assert service.is_available == service.is_item_available(voice)

    # Then json gone again (corruption mid-use).
    (tmp_path / f"{voice}.onnx.json").unlink()
    assert service.is_available is False
    assert service.is_item_available(voice) is False
    assert service.is_available == service.is_item_available(voice)


def test_is_available_false_when_aria2_sidecar_present(tmp_path):
    """A voice with a stale `<onnx>.aria2` control file is a partial
    download — aria2 leaves that file behind only when the transfer
    didn't complete. Reporting "available" in that state lets
    `synthesize()` proceed to `_get_voice` and crash inside Piper
    with `wave.Error: # channels not specified` (the WAV is closed
    without `setnchannels` ever being called because Piper failed
    silently). v0.8.7 added the `.aria2` guard so the loader treats
    the partial install as not-installed instead.
    """
    voice = "en_GB-alba-medium"
    (tmp_path / f"{voice}.onnx").write_bytes(b"partial-onnx-bytes")
    (tmp_path / f"{voice}.onnx.json").write_text("{}", encoding="utf-8")
    (tmp_path / f"{voice}.onnx.aria2").write_bytes(b"\x00" * 100)

    service = _make_service(tmp_path, voice)
    assert service.is_available is False
    assert service.is_item_available(voice) is False


def test_is_available_recovers_after_aria2_sidecar_removed(tmp_path):
    """Once the download completes, aria2 removes the `.aria2` sidecar.
    The voice should flip back to available without needing any other
    state to update.
    """
    voice = "en_GB-alba-medium"
    onnx = tmp_path / f"{voice}.onnx"
    sidecar = tmp_path / f"{voice}.onnx.aria2"
    onnx.write_bytes(b"complete-onnx-bytes")
    (tmp_path / f"{voice}.onnx.json").write_text("{}", encoding="utf-8")
    sidecar.write_bytes(b"\x00" * 100)

    service = _make_service(tmp_path, voice)
    assert service.is_available is False  # sidecar present → blocked
    sidecar.unlink()
    assert service.is_available is True  # sidecar gone → ready


def test_resolve_path_skips_partial_download(tmp_path):
    """`_resolve_existing_model_path` must also reject partial downloads
    so `synthesize()` raises its missing-model RuntimeError instead of
    proceeding to `wave.open` and crashing with `# channels not specified`.
    """
    voice = "en_GB-alba-medium"
    (tmp_path / f"{voice}.onnx").write_bytes(b"partial-onnx-bytes")
    (tmp_path / f"{voice}.onnx.json").write_text("{}", encoding="utf-8")
    (tmp_path / f"{voice}.onnx.aria2").write_bytes(b"\x00" * 100)

    service = _make_service(tmp_path, voice)
    assert service._resolve_existing_model_path() is None


def test_artifact_paths_reports_both_voice_files(tmp_path):
    """The new `artifact_paths` protocol method must expose the exact
    files the verifier will check for `.aria2` sidecars.
    """
    service = _make_service(tmp_path, "en_US-ryan-high")
    paths = service.artifact_paths("en_US-amy-medium")
    assert len(paths) == 2
    suffixes = [p.name for p in paths]
    assert any(s.endswith(".onnx") and not s.endswith(".onnx.json") for s in suffixes)
    assert any(s.endswith(".onnx.json") for s in suffixes)
