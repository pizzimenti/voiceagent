"""SHA pinning for the Piper download + verification path.

`_download_voice` and `artifact_manifest` previously both resolved
against `main` of `rhasspy/piper-voices`. If upstream pushed a voice
update in the few-second window between aria2 fetching the file
bytes and the manifest refresh, layer 2/3 verification would
fail-close on a healthy install. v0.8.0 closes that TOCTOU window by
capturing the upstream commit SHA at download start and resolving
both the file fetch and the manifest read against the same revision.

Tests in this module exercise:

1. `_capture_repo_sha()` returns the SHA from `HfApi.repo_info` and
   fails closed on missing/empty SHA.
2. `_voices_json_url_for_sha()` constructs the SHA-pinned manifest URL.
3. `_download_voice` sets `_current_download_sha`, threads the SHA
   through `hf_hub_url(..., revision=sha)`, and clears the SHA on
   download failure.
4. `_download_voice` and `artifact_manifest` agree on the same SHA
   when invoked back-to-back as part of one install.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

# Ensure the worktree's `src/` resolves before any editable install.
_HERE = Path(__file__).resolve().parent
_SRC = _HERE.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

# Headless Qt for any environment that didn't already set this.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from voiceagent.services.tts import PiperTtsService  # noqa: E402


_FAKE_SHA = "0123456789abcdef0123456789abcdef01234567"


@pytest.fixture
def piper_service(tmp_path: Path) -> PiperTtsService:
    service = PiperTtsService(command=["piper"], model_path=None)
    service.model_root = tmp_path
    return service


# --- _capture_repo_sha ----------------------------------------------------


def test_capture_repo_sha_returns_sha_from_hf_api(piper_service, monkeypatch):
    captured: dict[str, Any] = {}

    def _fake_repo_info(self, repo_id, *args, **kwargs):
        captured["repo_id"] = repo_id
        return SimpleNamespace(sha=_FAKE_SHA)

    from voiceagent.services.tts import HfApi

    monkeypatch.setattr(HfApi, "repo_info", _fake_repo_info)

    assert piper_service._capture_repo_sha() == _FAKE_SHA
    assert captured["repo_id"] == PiperTtsService.VOICE_REPOSITORY


def test_capture_repo_sha_raises_when_sha_missing(piper_service, monkeypatch):
    """Fail-closed: no SHA → no download. Falling back to `main` would
    re-open the TOCTOU window pinning is meant to close.
    """

    def _fake_repo_info(self, repo_id, *args, **kwargs):
        return SimpleNamespace(sha=None)

    from voiceagent.services.tts import HfApi

    monkeypatch.setattr(HfApi, "repo_info", _fake_repo_info)

    with pytest.raises(RuntimeError, match="Could not capture upstream commit SHA"):
        piper_service._capture_repo_sha()


def test_capture_repo_sha_raises_when_sha_empty(piper_service, monkeypatch):
    def _fake_repo_info(self, repo_id, *args, **kwargs):
        return SimpleNamespace(sha="")

    from voiceagent.services.tts import HfApi

    monkeypatch.setattr(HfApi, "repo_info", _fake_repo_info)

    with pytest.raises(RuntimeError, match="Could not capture upstream commit SHA"):
        piper_service._capture_repo_sha()


def test_capture_repo_sha_propagates_network_error(piper_service, monkeypatch):
    """Any underlying HfApi exception bubbles — `_download_voice` raises
    and the existing download-error UI surfaces it. We deliberately do
    NOT swallow + fall back to `main`.
    """

    def _boom(self, *args, **kwargs):
        raise RuntimeError("simulated HF API outage")

    from voiceagent.services.tts import HfApi

    monkeypatch.setattr(HfApi, "repo_info", _boom)

    with pytest.raises(RuntimeError, match="simulated HF API outage"):
        piper_service._capture_repo_sha()


# --- _voices_json_url_for_sha --------------------------------------------


def test_voices_json_url_for_sha_constructs_pinned_url():
    url = PiperTtsService._voices_json_url_for_sha(_FAKE_SHA)
    assert url == (
        f"https://huggingface.co/rhasspy/piper-voices/resolve/"
        f"{_FAKE_SHA}/voices.json?download=true"
    )


def test_voices_json_url_for_sha_does_not_use_main():
    """The whole point: never `resolve/main/`. The catalog-refresh path
    uses `main` (it wants the latest browsable list); the verification
    path must not.
    """
    url = PiperTtsService._voices_json_url_for_sha(_FAKE_SHA)
    assert "/resolve/main/" not in url
    assert f"/resolve/{_FAKE_SHA}/" in url


# --- _download_voice plumbing --------------------------------------------


class _RecordingDownloader:
    """Captures URLs and destination paths passed to `download` /
    `get_remote_size` so we can assert SHA threading without a real
    network call. Mirrors the subset of `AriaDownloader` that
    `_download_voice` invokes.
    """

    def __init__(self) -> None:
        self.size_calls: list[str] = []
        self.download_calls: list[list] = []

    def get_remote_size(self, url: str, headers: dict | None = None) -> int:
        self.size_calls.append(url)
        return 1234

    def download(self, files, progress_callback=None, headers=None) -> None:
        self.download_calls.append(list(files))
        # Simulate aria2 producing the on-disk artifacts so the
        # idempotence check on a follow-up call short-circuits.
        for f in files:
            f.destination.parent.mkdir(parents=True, exist_ok=True)
            f.destination.write_bytes(b"fake-bytes")


def _patch_repo_info(monkeypatch, sha: str) -> None:
    def _fake(self, repo_id, *args, **kwargs):
        return SimpleNamespace(sha=sha)

    from voiceagent.services.tts import HfApi

    monkeypatch.setattr(HfApi, "repo_info", _fake)


def test_download_voice_threads_sha_into_hf_hub_url(piper_service, monkeypatch):
    """Both file URLs (`onnx`, `onnx.json`) must contain the SHA, not
    `main`.
    """
    _patch_repo_info(monkeypatch, _FAKE_SHA)
    recorder = _RecordingDownloader()
    piper_service.downloader = recorder

    name = "en_US-ryan-high"
    piper_service._download_voice(name)

    # Both `get_remote_size` calls and the final `download` urls must
    # carry the SHA (one per file: .onnx and .onnx.json).
    for url in recorder.size_calls:
        assert f"/resolve/{_FAKE_SHA}/" in url
        assert "/resolve/main/" not in url

    download_files = recorder.download_calls[0]
    assert len(download_files) == 2
    for f in download_files:
        assert f"/resolve/{_FAKE_SHA}/" in f.url


def test_download_voice_sets_current_download_sha(piper_service, monkeypatch):
    _patch_repo_info(monkeypatch, _FAKE_SHA)
    captured_sha_during_download: list[str | None] = []

    class _Downloader:
        def get_remote_size(self, url, headers=None):
            return 0

        def download(self, files, progress_callback=None, headers=None):
            # Verifier is called on this thread immediately after,
            # via `_verify_download` → `artifact_manifest` — so the
            # SHA must be set NOW, not just before/after.
            captured_sha_during_download.append(
                piper_service._current_download_sha
            )
            for f in files:
                f.destination.parent.mkdir(parents=True, exist_ok=True)
                f.destination.write_bytes(b"")

    piper_service.downloader = _Downloader()
    piper_service._download_voice("en_US-ryan-high")

    assert captured_sha_during_download == [_FAKE_SHA]


def test_download_voice_clears_sha_on_failure(piper_service, monkeypatch):
    """If any step inside `_download_voice` raises, the SHA must be
    cleared so a stale value cannot leak into a follow-up call.
    """
    _patch_repo_info(monkeypatch, _FAKE_SHA)

    class _BoomDownloader:
        def get_remote_size(self, url, headers=None):
            raise RuntimeError("aria2 failed")

        def download(self, *args, **kwargs):
            raise AssertionError("download should not be reached")

    piper_service.downloader = _BoomDownloader()

    with pytest.raises(RuntimeError, match="aria2 failed"):
        piper_service._download_voice("en_US-ryan-high")

    assert piper_service._current_download_sha is None


def test_download_voice_skips_when_files_already_present(
    piper_service, monkeypatch
):
    """Idempotence guard runs BEFORE the SHA capture — no network
    on a no-op.
    """

    def _must_not_be_called(self, *args, **kwargs):
        raise AssertionError("HfApi.repo_info called for a no-op install")

    from voiceagent.services.tts import HfApi

    monkeypatch.setattr(HfApi, "repo_info", _must_not_be_called)

    name = "en_US-ryan-high"
    (piper_service.model_root / f"{name}.onnx").write_bytes(b"x")
    (piper_service.model_root / f"{name}.onnx.json").write_text("{}")

    piper_service._download_voice(name)  # must not raise


# --- end-to-end agreement: download SHA == manifest SHA ------------------


def test_download_and_manifest_agree_on_sha(piper_service, monkeypatch):
    """The whole point of pinning: both file URLs and the manifest
    fetch resolve against the same SHA in a single install.
    """
    _patch_repo_info(monkeypatch, _FAKE_SHA)

    fetched_shas: list[str] = []

    def _fake_voices_at_sha(cls, sha):
        fetched_shas.append(sha)
        return {
            "en_US-ryan-high": {
                "files": {
                    "en/en_US/ryan/high/en_US-ryan-high.onnx": {
                        "size_bytes": 1,
                        "md5_digest": "abc",
                    },
                }
            }
        }

    monkeypatch.setattr(
        PiperTtsService,
        "_fetch_voices_json_at_sha",
        classmethod(_fake_voices_at_sha),
    )

    recorder = _RecordingDownloader()
    piper_service.downloader = recorder

    # Run one install: download (sets SHA) → verify (reads manifest
    # under that same SHA, simulating `_verify_download`).
    name = "en_US-ryan-high"
    piper_service._download_voice(name)
    manifest = piper_service.artifact_manifest(name)

    # Manifest fetch saw the same SHA as the download.
    assert fetched_shas == [_FAKE_SHA]
    # Manifest is non-empty + maps to the local artifact path.
    assert manifest != {}
    assert (piper_service.model_root / f"{name}.onnx") in manifest
