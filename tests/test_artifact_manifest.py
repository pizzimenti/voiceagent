"""Per-backend `artifact_manifest` constructions for verification layers 2 + 3.

The base `ParallelItemLoader._verify_download` reads each backend's
`artifact_manifest(name)` to drive the size + checksum checks. These
tests exercise the construction itself: that the data shapes published
by Piper's voices.json and HuggingFace's `repo_info` are translated
correctly into `ArtifactManifestEntry` objects keyed by local artifact
paths, and that all the failure modes (missing cache, missing voice,
malformed JSON, network error, custom path, non-LFS sha256-less files)
fall through to an empty manifest rather than raising.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

# Ensure the worktree's `src/` resolves before any editable install.
_HERE = Path(__file__).resolve().parent
_SRC = _HERE.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

# Headless Qt for any environment that didn't already set this.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from voiceagent.parallel_item_loader import ArtifactManifestEntry
from voiceagent.services.stt import WhisperTranscriber
from voiceagent.services.tts import PiperTtsService


# --- Piper -----------------------------------------------------------------


_FAKE_SHA = "deadbeefcafef00ddeadbeefcafef00ddeadbeef"


@pytest.fixture
def piper_service(tmp_path: Path) -> PiperTtsService:
    service = PiperTtsService(command=["piper"], model_path=None)
    service.model_root = tmp_path
    # Production sets this in `_download_voice` before `artifact_manifest`
    # runs as part of `_verify_download`. Tests stand in for that.
    service._current_download_sha = _FAKE_SHA
    return service


def _write_voices_json(model_root: Path, payload: dict) -> None:
    (model_root / "voices.json").write_text(json.dumps(payload), encoding="utf-8")


def _stub_voices_json_at_sha(monkeypatch, payload: dict | None) -> None:
    """Make `artifact_manifest`'s SHA-pinned fetch return `payload`.

    The production path now resolves `voices.json` against the
    pinned upstream commit SHA captured at download start (closes
    the TOCTOU window where layer 2/3 verification could
    false-positive against an upstream-republished voice). Tests
    monkeypatch the classmethod fetcher to return the fixture
    directly — no network, no on-disk cache write (the SHA-pinned
    fetcher intentionally does not write the cache).
    """

    def _fake_fetch(cls, sha):
        return payload

    monkeypatch.setattr(
        PiperTtsService,
        "_fetch_voices_json_at_sha",
        classmethod(_fake_fetch),
    )


def test_piper_manifest_maps_basenames_to_local_paths(piper_service, monkeypatch):
    """The voices.json keys are repo-relative paths (`ar/ar_JO/.../X.onnx`);
    the manifest must map by basename to local `<model_root>/X.onnx`.
    """
    name = "ar_JO-kareem-low"
    _stub_voices_json_at_sha(
        monkeypatch,
        {
            name: {
                "files": {
                    f"ar/ar_JO/kareem/low/{name}.onnx": {
                        "size_bytes": 63201294,
                        "md5_digest": "d335cd06fe4045a7ee9d8fb0712afaa9",
                    },
                    f"ar/ar_JO/kareem/low/{name}.onnx.json": {
                        "size_bytes": 5022,
                        "md5_digest": "465724f7d2d5f2ff061b53acb8e7f7cc",
                    },
                    "ar/ar_JO/kareem/low/MODEL_CARD": {
                        "size_bytes": 274,
                        "md5_digest": "b6f0eaf5a7fd094be22a1bcb162173fb",
                    },
                },
            }
        },
    )

    manifest = piper_service.artifact_manifest(name)

    onnx_path = piper_service.model_root / f"{name}.onnx"
    json_path = piper_service.model_root / f"{name}.onnx.json"
    card_path = piper_service.model_root / "MODEL_CARD"

    assert manifest[onnx_path] == ArtifactManifestEntry(
        expected_size=63201294,
        expected_checksum_hex="d335cd06fe4045a7ee9d8fb0712afaa9",
        checksum_algorithm="md5",
    )
    assert manifest[json_path].expected_size == 5022
    assert manifest[json_path].checksum_algorithm == "md5"
    # Extra entries (MODEL_CARD) are harmless — verifier only checks
    # the intersection with `artifact_paths(name)`. The presence of
    # the entry shouldn't be lost just because the verifier doesn't
    # query it.
    assert card_path in manifest


def test_piper_manifest_returns_empty_when_sha_fetch_fails(
    piper_service, monkeypatch, caplog,
):
    """Network failure during the SHA-pinned manifest fetch degrades
    to layer 1 + 4 — we must NOT fall back to `main` (which would
    re-open the TOCTOU window) or to a stale on-disk cache.
    """
    # `_fetch_voices_json_at_sha` returns `None` on any failure
    # (network, JSON parse, non-dict payload). Mirror that contract.
    monkeypatch.setattr(
        PiperTtsService,
        "_fetch_voices_json_at_sha",
        classmethod(lambda cls, sha: None),
    )

    import logging
    with caplog.at_level(logging.WARNING):
        result = piper_service.artifact_manifest("any-voice")

    assert result == {}
    assert any(
        "sha-pinned fetch" in r.message.lower()
        or "skipping layers 2/3" in r.message.lower()
        for r in caplog.records
    )


def test_piper_manifest_returns_empty_when_no_sha_pinned(
    piper_service, monkeypatch, caplog,
):
    """`_current_download_sha` is `None` outside an active download.
    The verifier MUST NOT fall back to `main` because that's the
    exact TOCTOU window SHA pinning closes — degrade to layers 1 + 4
    instead.
    """
    piper_service._current_download_sha = None

    # Belt-and-braces: even if the SHA branch were skipped, the
    # SHA-pinned fetcher must not be invoked.
    def _must_not_be_called(cls, sha):  # pragma: no cover - guard
        raise AssertionError("SHA-pinned fetcher invoked without a SHA")

    monkeypatch.setattr(
        PiperTtsService,
        "_fetch_voices_json_at_sha",
        classmethod(_must_not_be_called),
    )

    import logging
    with caplog.at_level(logging.WARNING):
        result = piper_service.artifact_manifest("any-voice")

    assert result == {}
    assert any(
        "without a pinned sha" in r.message.lower()
        for r in caplog.records
    )


def test_piper_manifest_does_not_fall_back_to_stale_cache(
    piper_service, monkeypatch,
):
    """The P1 regression: a stale on-disk cache must never be used as
    the authoritative manifest source for fail-closed verification.

    Pre-existing `voices.json` cache on disk + SHA-pinned fetch fails
    → empty manifest (NOT the cached data, which could be arbitrarily
    old and would mass-reject healthy upstream-republished voices).
    """
    name = "stale-voice"
    _write_voices_json(
        piper_service.model_root,
        {
            name: {
                "files": {
                    f"x/y/z/{name}.onnx": {
                        "size_bytes": 1,
                        "md5_digest": "00000000000000000000000000000000",
                    }
                }
            }
        },
    )

    monkeypatch.setattr(
        PiperTtsService,
        "_fetch_voices_json_at_sha",
        classmethod(lambda cls, sha: None),
    )

    assert piper_service.artifact_manifest(name) == {}


def test_piper_manifest_returns_empty_when_voice_not_in_refreshed_payload(
    piper_service, monkeypatch,
):
    _stub_voices_json_at_sha(monkeypatch, {"other-voice": {"files": {}}})
    assert piper_service.artifact_manifest("missing-voice") == {}


def test_piper_manifest_handles_missing_metadata_fields(piper_service, monkeypatch):
    """A voice entry whose `files` map is missing one of size/md5 should
    yield an entry where the corresponding field is `None` (the verifier
    skips that layer for that file rather than failing closed).
    """
    name = "en_US-ryan-high"
    _stub_voices_json_at_sha(
        monkeypatch,
        {
            name: {
                "files": {
                    f"en/en_US/ryan/high/{name}.onnx": {
                        # size only — no md5_digest
                        "size_bytes": 100,
                    }
                }
            }
        },
    )

    manifest = piper_service.artifact_manifest(name)
    onnx_path = piper_service.model_root / f"{name}.onnx"
    entry = manifest[onnx_path]
    assert entry.expected_size == 100
    assert entry.expected_checksum_hex is None
    assert entry.checksum_algorithm is None


# --- Whisper ---------------------------------------------------------------


class _FakeLfs:
    def __init__(self, sha256: str | None = None) -> None:
        self.sha256 = sha256


class _FakeSibling:
    def __init__(
        self,
        rfilename: str,
        size: int | None = None,
        lfs: _FakeLfs | None = None,
    ) -> None:
        self.rfilename = rfilename
        self.size = size
        self.lfs = lfs


class _FakeRepoInfo:
    def __init__(self, siblings: list[_FakeSibling]) -> None:
        self.siblings = siblings


@pytest.fixture
def whisper_transcriber(tmp_path: Path, monkeypatch) -> WhisperTranscriber:
    monkeypatch.setattr(
        "voiceagent.services.stt.default_stt_model_root", lambda: tmp_path
    )
    return WhisperTranscriber(model_name="tiny.en")


def _patch_repo_info(monkeypatch, siblings: list[_FakeSibling]) -> None:
    def _fake_repo_info(self, repo_id, *, files_metadata=False, token=None):
        return _FakeRepoInfo(siblings)

    from voiceagent.services.stt import HfApi

    monkeypatch.setattr(HfApi, "repo_info", _fake_repo_info)


def test_whisper_manifest_extracts_size_and_sha256_from_lfs(
    whisper_transcriber, monkeypatch,
):
    _patch_repo_info(
        monkeypatch,
        [
            _FakeSibling(rfilename="config.json", size=1937, lfs=None),
            _FakeSibling(
                rfilename="model.bin",
                size=151093491,
                lfs=_FakeLfs(sha256="8548de43c352b2c4f327aacbd8291c893d788a7d6ba0a2ec0ef35c09e51dce8a"),
            ),
            _FakeSibling(rfilename="vocabulary.json", size=798156, lfs=None),
        ],
    )

    manifest = whisper_transcriber.artifact_manifest("tiny.en")

    local_dir = whisper_transcriber.model_root / "tiny.en"
    config_entry = manifest[local_dir / "config.json"]
    model_entry = manifest[local_dir / "model.bin"]
    vocab_entry = manifest[local_dir / "vocabulary.json"]

    # Non-LFS files: size only, no checksum.
    assert config_entry.expected_size == 1937
    assert config_entry.expected_checksum_hex is None
    assert config_entry.checksum_algorithm is None

    # LFS file: both size and sha256.
    assert model_entry.expected_size == 151093491
    assert model_entry.expected_checksum_hex == (
        "8548de43c352b2c4f327aacbd8291c893d788a7d6ba0a2ec0ef35c09e51dce8a"
    )
    assert model_entry.checksum_algorithm == "sha256"

    assert vocab_entry.expected_size == 798156
    assert vocab_entry.expected_checksum_hex is None


def test_whisper_manifest_skips_nested_paths(
    whisper_transcriber, monkeypatch,
):
    """`_prepare_model_source` ignores siblings whose rfilename contains
    `/` (nested directories). `artifact_manifest` mirrors the same rule
    so the two views of the repo stay consistent.
    """
    _patch_repo_info(
        monkeypatch,
        [
            _FakeSibling(rfilename="config.json", size=10),
            _FakeSibling(rfilename="subdir/nested.bin", size=20),
        ],
    )

    manifest = whisper_transcriber.artifact_manifest("tiny.en")

    local_dir = whisper_transcriber.model_root / "tiny.en"
    assert (local_dir / "config.json") in manifest
    assert not any(
        "nested.bin" in str(p) or "subdir" in str(p) for p in manifest
    )


def test_whisper_manifest_returns_empty_on_network_failure(
    whisper_transcriber, monkeypatch, caplog,
):
    """`HfApi.repo_info` raising mid-install must NOT abort the install.
    Layer 1 already passed during the download flow; layers 2/3 simply
    skip with a warning log so the user sees a successful install.
    """
    from voiceagent.services.stt import HfApi

    def _boom(self, *args, **kwargs):
        raise RuntimeError("simulated HF API outage")

    monkeypatch.setattr(HfApi, "repo_info", _boom)

    import logging
    with caplog.at_level(logging.WARNING):
        manifest = whisper_transcriber.artifact_manifest("tiny.en")

    assert manifest == {}
    assert any(
        "HfApi.repo_info failed" in r.message for r in caplog.records
    )


def test_whisper_manifest_returns_empty_for_unmanaged_custom_path(
    whisper_transcriber,
):
    """A custom-path Whisper selection has no upstream manifest source.
    `artifact_manifest` must short-circuit to an empty dict so the
    verifier degrades to layer 1 only.
    """
    assert whisper_transcriber.artifact_manifest("/some/local/whisper.bin") == {}


def test_whisper_manifest_passes_hf_token_when_present(
    whisper_transcriber, monkeypatch,
):
    """HF private repos require a token; the same env-var path that
    `_prepare_model_source` reads must also feed `repo_info`. A missing
    token here would silently 401 on private repos and the manifest
    would be empty even when the download succeeded with a token.
    """
    captured: dict = {}
    monkeypatch.setenv("HF_TOKEN", "test-token-abc")

    def _capture(self, repo_id, *, files_metadata=False, token=None):
        captured["repo_id"] = repo_id
        captured["files_metadata"] = files_metadata
        captured["token"] = token
        return _FakeRepoInfo([])

    from voiceagent.services.stt import HfApi
    monkeypatch.setattr(HfApi, "repo_info", _capture)

    whisper_transcriber.artifact_manifest("tiny.en")

    assert captured["repo_id"] == "Systran/faster-whisper-tiny.en"
    assert captured["files_metadata"] is True
    assert captured["token"] == "test-token-abc"
