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
3. `_download_voice` writes its SHA to `_download_sha_by_name[name]`,
   threads the SHA through `hf_hub_url(..., revision=sha)`, and pops
   the entry on download failure.
4. `_download_voice` and `artifact_manifest` agree on the same SHA
   when invoked back-to-back as part of one install.
5. Concurrent `_download_voice` calls (the loader runs with
   `max_workers=3`) keep their per-voice pins isolated — voice A's
   verifier always reads voice A's SHA, never voice B's.
"""

from __future__ import annotations

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

    name = "en_US-ryan-high"

    class _Downloader:
        def get_remote_size(self, url, headers=None):
            return 0

        def download(self, files, progress_callback=None, headers=None):
            # Verifier is called on this thread immediately after,
            # via `_verify_download` → `artifact_manifest` — so the
            # SHA must be in the per-voice dict NOW, not just
            # before/after.
            captured_sha_during_download.append(
                piper_service._download_sha_by_name.get(name)
            )
            for f in files:
                f.destination.parent.mkdir(parents=True, exist_ok=True)
                f.destination.write_bytes(b"")

    piper_service.downloader = _Downloader()
    piper_service._download_voice(name)

    assert captured_sha_during_download == [_FAKE_SHA]


def test_download_voice_clears_sha_on_failure(piper_service, monkeypatch):
    """If any step inside `_download_voice` raises, this voice's pin
    must be popped so a stale value cannot leak into a follow-up call.
    Other voices' pins (concurrent installs) are untouched.
    """
    _patch_repo_info(monkeypatch, _FAKE_SHA)

    class _BoomDownloader:
        def get_remote_size(self, url, headers=None):
            raise RuntimeError("aria2 failed")

        def download(self, *args, **kwargs):
            raise AssertionError("download should not be reached")

    piper_service.downloader = _BoomDownloader()

    # Pre-populate an unrelated voice's pin to prove we only pop the
    # failing entry, not the whole dict.
    piper_service._download_sha_by_name["en_US-amy-low"] = "other-sha"

    name = "en_US-ryan-high"
    with pytest.raises(RuntimeError, match="aria2 failed"):
        piper_service._download_voice(name)

    assert name not in piper_service._download_sha_by_name
    assert piper_service._download_sha_by_name == {"en_US-amy-low": "other-sha"}


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


# --- concurrent installs: per-voice pin isolation ------------------------


def test_concurrent_downloads_keep_per_voice_sha_isolated(
    piper_service, monkeypatch
):
    """Two concurrent `_download_voice` calls (the loader runs with
    `max_workers=3`) must not bleed each other's pinned SHA.

    Pre-fix, both calls wrote the same instance attribute
    `_current_download_sha`, so whichever finished `_capture_repo_sha`
    last won — voice A's verifier could end up reading voice B's SHA
    and silently passing/failing the wrong artifact. Post-fix, each
    install carries its own per-voice entry in
    `_download_sha_by_name` and the verifier reads its own pin.

    We simulate the race deterministically:
    - Voice A and voice B have DIFFERENT SHAs.
    - The fake downloader for each voice blocks on a barrier so we
      know both `_download_voice` calls are mid-flight at the same
      time.
    - While both pins are simultaneously live in the dict, each call
      records what `artifact_manifest` returns for its own voice —
      i.e., what SHA the verifier *would* see.
    - Each voice's recorded SHA must equal its OWN pin, not the
      other's.
    """
    import threading
    from concurrent.futures import ThreadPoolExecutor

    sha_a = "a" * 40
    sha_b = "b" * 40

    name_a = "en_US-ryan-high"
    name_b = "en_GB-alan-low"

    # Different SHAs returned per voice. `_capture_repo_sha` calls
    # `HfApi.repo_info` — we patch it to dispatch on which voice's
    # download is currently running by inspecting the per-name dict
    # that's already been populated by us upstream. To keep that
    # ordering tractable, we instead pre-seed the dict ourselves and
    # bypass `_capture_repo_sha` entirely via the per-voice fake
    # downloader below. But `_download_voice` calls
    # `_capture_repo_sha` BEFORE writing the pin, so we have to feed
    # it the right SHA per call. Use a small lookup keyed on the
    # *next* voice scheduled.
    pending_shas: dict[str, str] = {}

    def _fake_repo_info(self, repo_id, *args, **kwargs):
        # Threads call this serialized via the lock below — first
        # caller wins the next pending SHA. We can't see voice_name
        # from here, so the test driver pushes the SHA each thread
        # needs onto a queue keyed by thread id.
        return SimpleNamespace(sha=pending_shas[threading.get_ident()])

    from voiceagent.services.tts import HfApi
    monkeypatch.setattr(HfApi, "repo_info", _fake_repo_info)

    # Both threads must reach mid-download together so each writes
    # its pin BEFORE the other reads.
    barrier = threading.Barrier(2)

    # Per-voice manifest payloads keyed by voice name.
    payloads = {
        name_a: {
            name_a: {
                "files": {
                    f"en/en_US/ryan/high/{name_a}.onnx": {
                        "size_bytes": 1,
                        "md5_digest": "aaa",
                    },
                }
            }
        },
        name_b: {
            name_b: {
                "files": {
                    f"en/en_GB/alan/low/{name_b}.onnx": {
                        "size_bytes": 2,
                        "md5_digest": "bbb",
                    },
                }
            }
        },
    }

    # Fetch returns the manifest entry for whichever voice's SHA was
    # passed in. Lets the verifier-side assertion confirm the
    # per-voice SHA actually flowed through.
    sha_to_payload = {sha_a: payloads[name_a], sha_b: payloads[name_b]}

    def _fake_voices_at_sha(cls, sha):
        return sha_to_payload[sha]

    monkeypatch.setattr(
        PiperTtsService,
        "_fetch_voices_json_at_sha",
        classmethod(_fake_voices_at_sha),
    )

    seen_shas: dict[str, str | None] = {}

    class _BarrierDownloader:
        """Records the per-voice SHA the verifier would see while BOTH
        downloads are mid-flight (i.e., both pins live in the dict at
        the same time).
        """

        def get_remote_size(self, url, headers=None):
            return 0

        def download(self, files, progress_callback=None, headers=None):
            # Identify which voice this call is for via the
            # destination filename.
            onnx_dest = next(f.destination for f in files if f.destination.suffix == ".onnx")
            voice_name = onnx_dest.stem
            # Wait until BOTH _download_voice calls have written their
            # pins, then read this voice's pin (without consuming it,
            # since the read in production happens via
            # `artifact_manifest`).
            barrier.wait(timeout=5)
            seen_shas[voice_name] = piper_service._download_sha_by_name.get(
                voice_name
            )
            for f in files:
                f.destination.parent.mkdir(parents=True, exist_ok=True)
                f.destination.write_bytes(b"")

    piper_service.downloader = _BarrierDownloader()

    def _drive_install(name: str, sha: str) -> dict:
        """Run one install and capture what the verifier sees."""
        pending_shas[threading.get_ident()] = sha
        piper_service._download_voice(name)
        manifest = piper_service.artifact_manifest(name)
        return {"manifest": manifest}

    with ThreadPoolExecutor(max_workers=2) as ex:
        fut_a = ex.submit(_drive_install, name_a, sha_a)
        fut_b = ex.submit(_drive_install, name_b, sha_b)
        result_a = fut_a.result(timeout=10)
        result_b = fut_b.result(timeout=10)

    # Mid-flight observation: each voice saw its own SHA, not the
    # other's. THIS is the concurrency invariant the per-name dict
    # exists to preserve.
    assert seen_shas[name_a] == sha_a
    assert seen_shas[name_b] == sha_b

    # End-to-end check: each voice's manifest carries the entry from
    # ITS OWN payload (which is keyed on the SHA, so the wrong SHA
    # would either KeyError or pull the wrong voice's entry).
    onnx_a = piper_service.model_root / f"{name_a}.onnx"
    onnx_b = piper_service.model_root / f"{name_b}.onnx"
    assert result_a["manifest"][onnx_a].expected_size == 1
    assert result_a["manifest"][onnx_a].expected_checksum_hex == "aaa"
    assert result_b["manifest"][onnx_b].expected_size == 2
    assert result_b["manifest"][onnx_b].expected_checksum_hex == "bbb"

    # Post-verify: both pins were popped (consume-on-read in
    # `artifact_manifest`). The dict is empty — no unbounded growth.
    assert piper_service._download_sha_by_name == {}


def test_interleaved_writes_dont_overwrite_each_other(
    piper_service, monkeypatch
):
    """Deterministic alternative to the threaded race: drive the
    writer/verifier flow by hand to prove the per-name dict isolates
    pins even when interleaved.

    Order:
        write voice A pin → write voice B pin → verify voice A
        → assert A reads A → verify voice B → assert B reads B

    On the pre-fix shared-scalar implementation, voice A's verifier
    would read voice B's SHA at step 3 because step 2 stomped the
    scalar.
    """
    sha_a = "a" * 40
    sha_b = "b" * 40
    name_a = "en_US-ryan-high"
    name_b = "en_GB-alan-low"

    payload = {
        name_a: {
            "files": {
                f"en/en_US/ryan/high/{name_a}.onnx": {
                    "size_bytes": 1,
                    "md5_digest": "aaa",
                }
            }
        },
        name_b: {
            "files": {
                f"en/en_GB/alan/low/{name_b}.onnx": {
                    "size_bytes": 2,
                    "md5_digest": "bbb",
                }
            }
        },
    }

    fetched_shas: list[str] = []

    def _fake_fetch(cls, sha):
        fetched_shas.append(sha)
        return payload

    # Same payload for any SHA — the *check* is which SHA the
    # verifier passes to the fetcher.
    monkeypatch.setattr(
        PiperTtsService,
        "_fetch_voices_json_at_sha",
        classmethod(_fake_fetch),
    )

    # Step 1: voice A writes its pin (simulating `_download_voice`).
    with piper_service._download_sha_lock:
        piper_service._download_sha_by_name[name_a] = sha_a
    # Step 2: voice B writes its pin BEFORE A's verifier runs —
    # this is the race window pre-fix.
    with piper_service._download_sha_lock:
        piper_service._download_sha_by_name[name_b] = sha_b
    # Step 3: A's verifier reads — must see A's SHA.
    manifest_a = piper_service.artifact_manifest(name_a)
    assert fetched_shas[-1] == sha_a, (
        f"voice A verifier saw SHA {fetched_shas[-1]!r}, "
        f"expected {sha_a!r}"
    )
    # Step 4: B's verifier reads — must still see B's SHA
    # (A's verifier popped only A's entry).
    manifest_b = piper_service.artifact_manifest(name_b)
    assert fetched_shas[-1] == sha_b, (
        f"voice B verifier saw SHA {fetched_shas[-1]!r}, "
        f"expected {sha_b!r}"
    )

    # And the manifests carry the right voice's data.
    onnx_a = piper_service.model_root / f"{name_a}.onnx"
    onnx_b = piper_service.model_root / f"{name_b}.onnx"
    assert manifest_a[onnx_a].expected_checksum_hex == "aaa"
    assert manifest_b[onnx_b].expected_checksum_hex == "bbb"

    # Both consumed — dict empty.
    assert piper_service._download_sha_by_name == {}
