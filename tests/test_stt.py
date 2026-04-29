"""Direct unit tests for `services/stt.py` (WhisperTranscriber).

These complement `test_custom_stt_path.py` (which already exercises
the custom-vs-managed path logic) by covering:

* model-path fallback logic in `is_model_available` / `_prepare_model_source`
  — when a custom path exists, when it's missing, when a managed dir is
  partially populated.
* lifecycle: `set_model_name` resets the loaded model handle; `ensure_loaded`
  / `download_and_load` route through the mocked `WhisperModel` constructor.
* `transcribe` happy-path + empty-transcript no-op (returns "" with a
  WARNING log entry instead of raising — v0.9.14 fix).
* `remove_item` semantics — only managed items are removed, custom paths raise.
* `artifact_paths` / `artifact_manifest` for both managed and unmanaged
  selections, including HF metadata fall-soft on network failure.
* Download headers — `HF_TOKEN` env var presence/absence.

The Whisper backend imports `faster_whisper.WhisperModel` lazily inside
`download_and_load`, so tests inject a fake by monkeypatching
`sys.modules["faster_whisper"]` BEFORE the lazy import fires.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from voiceagent.services.stt import WhisperTranscriber


# --- helpers -------------------------------------------------------------


def _populate_model_dir(local_dir: Path, *, with_vocab: bool = True) -> None:
    """Create the required + vocabulary files faster-whisper expects."""
    local_dir.mkdir(parents=True, exist_ok=True)
    for filename in WhisperTranscriber.REQUIRED_MODEL_FILES:
        (local_dir / filename).write_text("payload")
    if with_vocab:
        (local_dir / "vocabulary.json").write_text("[]")


def _install_fake_faster_whisper(monkeypatch, recorded: list[dict]):
    """Install a `faster_whisper` module with a `WhisperModel` mock.

    The fake records constructor kwargs into `recorded` and returns an
    object whose `.transcribe(...)` yields a controllable segment list.
    Default behavior: a single segment with text "hello world".
    """

    class _FakeWhisperModel:
        def __init__(self, model_source, device, compute_type, **kwargs):
            recorded.append(
                {
                    "model_source": model_source,
                    "device": device,
                    "compute_type": compute_type,
                    "kwargs": kwargs,
                }
            )

        def transcribe(self, audio_path, beam_size=1, vad_filter=True):
            segments = [SimpleNamespace(text="hello world")]
            info = SimpleNamespace(language="en")
            return iter(segments), info

    fake_module = SimpleNamespace(WhisperModel=_FakeWhisperModel)
    monkeypatch.setitem(sys.modules, "faster_whisper", fake_module)
    return _FakeWhisperModel


# --- fixtures ------------------------------------------------------------


@pytest.fixture
def model_root(tmp_path: Path) -> Path:
    return tmp_path / "stt-models"


@pytest.fixture
def transcriber(monkeypatch, model_root: Path) -> WhisperTranscriber:
    monkeypatch.setattr(
        "voiceagent.services.stt.default_stt_model_root", lambda: model_root
    )
    return WhisperTranscriber("tiny.en")


# --- available_model_names / available_items ----------------------------


def test_available_model_names_returns_all_keys():
    names = WhisperTranscriber.available_model_names()
    assert "tiny.en" in names
    assert "large-v3" in names
    assert names == list(WhisperTranscriber.MODEL_REPOSITORIES.keys())


def test_available_items_includes_managed_only_when_no_custom(transcriber):
    assert transcriber.available_items() == list(
        WhisperTranscriber.MODEL_REPOSITORIES.keys()
    )


def test_selected_item_returns_model_name(transcriber):
    assert transcriber.selected_item == "tiny.en"


# --- is_model_available / _directory_has_model_files --------------------


def test_is_model_available_managed_with_complete_dir(transcriber, model_root):
    _populate_model_dir(model_root / "tiny.en")
    assert WhisperTranscriber.is_model_available(model_root, "tiny.en") is True


def test_is_model_available_managed_missing_file(transcriber, model_root):
    target = model_root / "tiny.en"
    _populate_model_dir(target)
    # Strip one required file → no longer "available".
    (target / "model.bin").unlink()
    assert WhisperTranscriber.is_model_available(model_root, "tiny.en") is False


def test_is_model_available_managed_zero_byte_file(transcriber, model_root):
    target = model_root / "tiny.en"
    _populate_model_dir(target)
    (target / "model.bin").write_text("")  # zero bytes — invalid
    assert WhisperTranscriber.is_model_available(model_root, "tiny.en") is False


def test_is_model_available_requires_a_vocabulary_variant(transcriber, model_root):
    target = model_root / "tiny.en"
    _populate_model_dir(target, with_vocab=False)
    assert WhisperTranscriber.is_model_available(model_root, "tiny.en") is False
    (target / "vocabulary.txt").write_text("[]")
    assert WhisperTranscriber.is_model_available(model_root, "tiny.en") is True


def test_is_model_available_custom_path_directory(model_root, tmp_path):
    custom_dir = tmp_path / "custom-whisper"
    _populate_model_dir(custom_dir)
    assert (
        WhisperTranscriber.is_model_available(model_root, str(custom_dir)) is True
    )


def test_is_model_available_custom_directory_without_files(model_root, tmp_path):
    custom_dir = tmp_path / "custom-whisper"
    custom_dir.mkdir()
    assert (
        WhisperTranscriber.is_model_available(model_root, str(custom_dir)) is False
    )


def test_is_model_available_custom_path_file_assumed_packed(model_root, tmp_path):
    """A non-directory custom path defers to faster-whisper's own validation."""
    packed = tmp_path / "model.bin"
    packed.write_bytes(b"\x00")
    assert WhisperTranscriber.is_model_available(model_root, str(packed)) is True


def test_is_model_available_unknown_managed_name_returns_false(model_root):
    assert (
        WhisperTranscriber.is_model_available(model_root, "not-a-real-key") is False
    )


# --- is_available property ----------------------------------------------


def test_is_available_true_when_loaded(transcriber):
    transcriber._model = object()  # any non-None
    assert transcriber.is_available is True


def test_is_available_falls_back_to_disk_check(transcriber, model_root):
    assert transcriber.is_available is False
    _populate_model_dir(model_root / "tiny.en")
    assert transcriber.is_available is True


# --- set_model_name lifecycle -------------------------------------------


def test_set_model_name_no_change_keeps_handle(transcriber):
    transcriber._model = object()
    transcriber.set_model_name("tiny.en")
    assert transcriber._model is not None  # unchanged


def test_set_model_name_resets_loaded_handle(transcriber):
    transcriber._model = object()
    transcriber.set_model_name("base.en")
    assert transcriber._model is None
    assert transcriber.model_name == "base.en"


def test_set_selected_item_delegates_to_set_model_name(transcriber):
    transcriber.set_selected_item("base.en")
    assert transcriber.model_name == "base.en"


# --- download_and_load + WhisperModel mock ------------------------------


def test_download_and_load_invokes_whisper_model_with_compute_type(
    monkeypatch, model_root
):
    """Configured `compute_type` is forwarded to `WhisperModel(...)`."""
    monkeypatch.setattr(
        "voiceagent.services.stt.default_stt_model_root", lambda: model_root
    )
    transcriber = WhisperTranscriber(
        "tiny.en", device="cuda", compute_type="float16"
    )

    # Disk already has the managed model — `_prepare_model_source` skips download.
    _populate_model_dir(model_root / "tiny.en")
    # `_prepare_model_source` for managed names goes through `HfApi.repo_info`
    # to compute file diffs. Stub to a no-op (no missing files since on-disk
    # already valid).
    monkeypatch.setattr(
        "voiceagent.services.stt.HfApi",
        lambda: SimpleNamespace(
            repo_info=lambda *a, **k: SimpleNamespace(siblings=[])
        ),
    )

    recorded: list[dict] = []
    _install_fake_faster_whisper(monkeypatch, recorded)

    transcriber.download_and_load()

    assert len(recorded) == 1
    assert recorded[0]["device"] == "cuda"
    assert recorded[0]["compute_type"] == "float16"
    # Source path resolved to the on-disk managed dir.
    assert recorded[0]["model_source"] == str(model_root / "tiny.en")
    assert transcriber.is_loaded is True


def test_download_and_load_idempotent_after_first_load(monkeypatch, model_root):
    monkeypatch.setattr(
        "voiceagent.services.stt.default_stt_model_root", lambda: model_root
    )
    _populate_model_dir(model_root / "tiny.en")
    monkeypatch.setattr(
        "voiceagent.services.stt.HfApi",
        lambda: SimpleNamespace(
            repo_info=lambda *a, **k: SimpleNamespace(siblings=[])
        ),
    )
    recorded: list[dict] = []
    _install_fake_faster_whisper(monkeypatch, recorded)

    transcriber = WhisperTranscriber("tiny.en")
    transcriber.download_and_load()
    transcriber.download_and_load()  # second call must not re-instantiate

    assert len(recorded) == 1


def test_ensure_loaded_calls_download_and_load(monkeypatch, model_root):
    monkeypatch.setattr(
        "voiceagent.services.stt.default_stt_model_root", lambda: model_root
    )
    _populate_model_dir(model_root / "tiny.en")
    monkeypatch.setattr(
        "voiceagent.services.stt.HfApi",
        lambda: SimpleNamespace(
            repo_info=lambda *a, **k: SimpleNamespace(siblings=[])
        ),
    )
    recorded: list[dict] = []
    _install_fake_faster_whisper(monkeypatch, recorded)

    transcriber = WhisperTranscriber("tiny.en")
    assert transcriber.is_loaded is False
    transcriber.ensure_loaded()
    assert transcriber.is_loaded is True
    assert len(recorded) == 1


# --- transcribe ----------------------------------------------------------


def test_transcribe_returns_concatenated_segment_text(monkeypatch, model_root):
    monkeypatch.setattr(
        "voiceagent.services.stt.default_stt_model_root", lambda: model_root
    )
    _populate_model_dir(model_root / "tiny.en")
    monkeypatch.setattr(
        "voiceagent.services.stt.HfApi",
        lambda: SimpleNamespace(
            repo_info=lambda *a, **k: SimpleNamespace(siblings=[])
        ),
    )

    class _MultiSegmentModel:
        def __init__(self, *args, **kwargs):
            pass

        def transcribe(self, audio_path, beam_size=1, vad_filter=True):
            segments = [
                SimpleNamespace(text="  hello "),
                SimpleNamespace(text=""),  # filtered
                SimpleNamespace(text=" world  "),
            ]
            info = SimpleNamespace(language="en")
            return iter(segments), info

    monkeypatch.setitem(
        sys.modules,
        "faster_whisper",
        SimpleNamespace(WhisperModel=_MultiSegmentModel),
    )

    transcriber = WhisperTranscriber("tiny.en")
    result = transcriber.transcribe(Path("/tmp/fake.wav"))
    assert result == "hello world"


def test_transcribe_returns_empty_on_empty_segments(monkeypatch, model_root, caplog):
    """No-speech / silence yields an empty transcript, not a raise.

    Whisper produces no segments for silence or VAD-rejected audio. The
    transcriber must surface that as an empty string so the caller can
    short-circuit the turn cleanly; raising would surface as a red error
    row in the UI for what is in fact a graceful no-op.
    """
    import logging

    monkeypatch.setattr(
        "voiceagent.services.stt.default_stt_model_root", lambda: model_root
    )
    _populate_model_dir(model_root / "tiny.en")
    monkeypatch.setattr(
        "voiceagent.services.stt.HfApi",
        lambda: SimpleNamespace(
            repo_info=lambda *a, **k: SimpleNamespace(siblings=[])
        ),
    )

    class _EmptyModel:
        def __init__(self, *args, **kwargs):
            pass

        def transcribe(self, audio_path, beam_size=1, vad_filter=True):
            return iter([]), SimpleNamespace(language="es")

    monkeypatch.setitem(
        sys.modules,
        "faster_whisper",
        SimpleNamespace(WhisperModel=_EmptyModel),
    )

    transcriber = WhisperTranscriber("tiny.en")
    with caplog.at_level(logging.INFO, logger="voiceagent.services.stt"):
        result = transcriber.transcribe(Path("/tmp/fake.wav"))
    assert result == ""
    # INFO not WARNING — empty transcripts fire on every silent
    # partial probe, so WARNING would flood stderr (v0.10.1 walked
    # back v0.9.14's level bump for that reason).
    assert any(
        "Whisper returned empty transcript" in record.message
        and record.levelno == logging.INFO
        for record in caplog.records
    )


# --- _prepare_model_source ----------------------------------------------


def test_prepare_model_source_returns_existing_custom_path(
    monkeypatch, model_root, tmp_path
):
    monkeypatch.setattr(
        "voiceagent.services.stt.default_stt_model_root", lambda: model_root
    )
    custom_dir = tmp_path / "custom"
    _populate_model_dir(custom_dir)
    transcriber = WhisperTranscriber(str(custom_dir))

    # HfApi must not be touched — custom path is honored as-is.
    def _boom(*args, **kwargs):
        raise AssertionError("HfApi must not be touched for custom paths")

    monkeypatch.setattr("voiceagent.services.stt.HfApi", _boom)

    source = transcriber._prepare_model_source(item_name=str(custom_dir))
    assert source == str(custom_dir)


def test_prepare_model_source_returns_unknown_name_unchanged(
    monkeypatch, model_root
):
    monkeypatch.setattr(
        "voiceagent.services.stt.default_stt_model_root", lambda: model_root
    )
    transcriber = WhisperTranscriber("tiny.en")

    # Unknown bare name (not a path, not in MODEL_REPOSITORIES) — passed
    # straight through, since faster-whisper might still resolve it.
    source = transcriber._prepare_model_source(item_name="not-a-real-key")
    assert source == "not-a-real-key"


def test_prepare_model_source_skips_already_complete_managed_files(
    monkeypatch, model_root
):
    """When local files match HF-reported sizes, no aria2 download fires."""
    monkeypatch.setattr(
        "voiceagent.services.stt.default_stt_model_root", lambda: model_root
    )
    transcriber = WhisperTranscriber("tiny.en")

    target = model_root / "tiny.en"
    target.mkdir(parents=True, exist_ok=True)
    sibling_specs = [
        ("config.json", b"{}"),
        ("model.bin", b"\x00\x01\x02"),
        ("vocabulary.json", b"[]"),
    ]
    siblings = []
    for filename, payload in sibling_specs:
        (target / filename).write_bytes(payload)
        siblings.append(SimpleNamespace(rfilename=filename, size=len(payload)))

    monkeypatch.setattr(
        "voiceagent.services.stt.HfApi",
        lambda: SimpleNamespace(
            repo_info=lambda *a, **k: SimpleNamespace(siblings=siblings)
        ),
    )
    monkeypatch.setattr(
        "voiceagent.services.stt.hf_hub_url",
        lambda repo, filename, **k: f"https://hf/{repo}/{filename}",
    )

    download_calls: list = []
    transcriber.downloader = SimpleNamespace(
        get_remote_size=lambda url, headers=None: 0,
        download=lambda files, progress_callback=None, headers=None: download_calls.append(files),
    )

    source = transcriber._prepare_model_source(item_name="tiny.en")
    assert source == str(target)
    assert download_calls == []  # no missing files → no transfer


def test_prepare_model_source_downloads_missing_managed_files(
    monkeypatch, model_root
):
    monkeypatch.setattr(
        "voiceagent.services.stt.default_stt_model_root", lambda: model_root
    )
    transcriber = WhisperTranscriber("tiny.en")

    siblings = [
        SimpleNamespace(rfilename="config.json", size=2),
        SimpleNamespace(rfilename="model.bin", size=10),
        SimpleNamespace(rfilename="nested/skip.bin", size=99),  # filtered: has "/"
    ]

    monkeypatch.setattr(
        "voiceagent.services.stt.HfApi",
        lambda: SimpleNamespace(
            repo_info=lambda *a, **k: SimpleNamespace(siblings=siblings)
        ),
    )
    monkeypatch.setattr(
        "voiceagent.services.stt.hf_hub_url",
        lambda repo, filename, **k: f"https://hf/{repo}/{filename}",
    )

    captured_downloads: list[list] = []

    def _download(files, progress_callback=None, headers=None):
        captured_downloads.append(list(files))
        # Simulate aria2 producing the bytes so a follow-up call would
        # short-circuit (not exercised here, but defensive).
        for f in files:
            f.destination.parent.mkdir(parents=True, exist_ok=True)
            f.destination.write_bytes(b"x")

    transcriber.downloader = SimpleNamespace(
        get_remote_size=lambda url, headers=None: 0,
        download=_download,
    )

    transcriber._prepare_model_source(item_name="tiny.en")
    assert len(captured_downloads) == 1
    queued = captured_downloads[0]
    queued_names = {f.destination.name for f in queued}
    assert queued_names == {"config.json", "model.bin"}  # nested filtered out


# --- remove_item --------------------------------------------------------


def test_remove_item_removes_managed_dir(monkeypatch, model_root):
    monkeypatch.setattr(
        "voiceagent.services.stt.default_stt_model_root", lambda: model_root
    )
    transcriber = WhisperTranscriber("tiny.en")
    target = model_root / "tiny.en"
    _populate_model_dir(target)
    assert target.exists()

    transcriber.remove_item("tiny.en")
    assert not target.exists()


def test_remove_item_clears_handle_when_active_model_removed(
    monkeypatch, model_root
):
    monkeypatch.setattr(
        "voiceagent.services.stt.default_stt_model_root", lambda: model_root
    )
    transcriber = WhisperTranscriber("tiny.en")
    _populate_model_dir(model_root / "tiny.en")
    transcriber._model = object()
    transcriber.remove_item("tiny.en")
    assert transcriber._model is None


def test_remove_item_keeps_handle_when_other_model_removed(
    monkeypatch, model_root
):
    monkeypatch.setattr(
        "voiceagent.services.stt.default_stt_model_root", lambda: model_root
    )
    transcriber = WhisperTranscriber("tiny.en")
    _populate_model_dir(model_root / "base.en")
    sentinel = object()
    transcriber._model = sentinel
    transcriber.remove_item("base.en")
    assert transcriber._model is sentinel


def test_remove_item_noop_when_dir_absent(monkeypatch, model_root):
    monkeypatch.setattr(
        "voiceagent.services.stt.default_stt_model_root", lambda: model_root
    )
    transcriber = WhisperTranscriber("tiny.en")
    transcriber.remove_item("tiny.en")  # must not raise


def test_remove_item_raises_for_unmanaged_name(transcriber):
    with pytest.raises(RuntimeError, match="cannot be removed"):
        transcriber.remove_item("/some/custom/path")


# --- artifact_paths -----------------------------------------------------


def test_artifact_paths_managed_returns_required_plus_vocab(transcriber, model_root):
    paths = transcriber.artifact_paths("tiny.en")
    names = [p.name for p in paths]
    for required in WhisperTranscriber.REQUIRED_MODEL_FILES:
        assert required in names
    for vocab in WhisperTranscriber.VOCABULARY_FILES:
        assert vocab in names
    # All paths under <model_root>/<item>/.
    for path in paths:
        assert path.parent == model_root / "tiny.en"


def test_artifact_paths_unmanaged_returns_empty(transcriber):
    assert transcriber.artifact_paths("/some/custom/path") == []


# --- artifact_manifest --------------------------------------------------


def test_artifact_manifest_unmanaged_returns_empty(transcriber):
    assert transcriber.artifact_manifest("/some/custom/path") == {}


def test_artifact_manifest_returns_size_and_lfs_sha(transcriber, monkeypatch):
    siblings = [
        SimpleNamespace(
            rfilename="config.json",
            size=42,
            lfs=None,
        ),
        SimpleNamespace(
            rfilename="model.bin",
            size=1024,
            lfs=SimpleNamespace(sha256="deadbeef"),
        ),
        SimpleNamespace(
            rfilename="nested/skip.bin",  # filtered: has "/"
            size=99,
            lfs=None,
        ),
    ]
    monkeypatch.setattr(
        "voiceagent.services.stt.HfApi",
        lambda: SimpleNamespace(
            repo_info=lambda *a, **k: SimpleNamespace(siblings=siblings)
        ),
    )

    manifest = transcriber.artifact_manifest("tiny.en")
    config_entry = manifest[transcriber.model_root / "tiny.en" / "config.json"]
    bin_entry = manifest[transcriber.model_root / "tiny.en" / "model.bin"]
    nested_path = transcriber.model_root / "tiny.en" / "skip.bin"

    assert config_entry.expected_size == 42
    assert config_entry.expected_checksum_hex is None
    assert config_entry.checksum_algorithm is None

    assert bin_entry.expected_size == 1024
    assert bin_entry.expected_checksum_hex == "deadbeef"
    assert bin_entry.checksum_algorithm == "sha256"

    assert nested_path not in manifest


def test_artifact_manifest_network_failure_returns_empty(transcriber, monkeypatch):
    """Fail-soft: HfApi outage degrades to layer-1 verification only."""

    def _raise(*args, **kwargs):
        raise RuntimeError("HF outage")

    monkeypatch.setattr(
        "voiceagent.services.stt.HfApi",
        lambda: SimpleNamespace(repo_info=_raise),
    )
    assert transcriber.artifact_manifest("tiny.en") == {}


# --- _download_headers --------------------------------------------------


def test_download_headers_empty_without_token(monkeypatch, transcriber):
    monkeypatch.delenv("HF_TOKEN", raising=False)
    assert transcriber._download_headers() == {}


def test_download_headers_includes_bearer_token(monkeypatch, transcriber):
    monkeypatch.setenv("HF_TOKEN", "secret-token")
    assert transcriber._download_headers() == {
        "Authorization": "Bearer secret-token"
    }


def test_download_headers_treats_whitespace_as_empty(monkeypatch, transcriber):
    monkeypatch.setenv("HF_TOKEN", "   ")
    assert transcriber._download_headers() == {}


# --- compute-type / device hydration -----------------------------------


def test_constructor_threads_compute_type_into_state():
    """`AppState.STT_COMPUTE_TYPE` reaches the constructor verbatim."""
    transcriber = WhisperTranscriber(
        "tiny.en", device="cpu", compute_type="int8"
    )
    assert transcriber.device == "cpu"
    assert transcriber.compute_type == "int8"


def test_constructor_defaults_to_auto_auto():
    """Default config (no override) yields device=auto, compute_type=auto."""
    transcriber = WhisperTranscriber("tiny.en")
    assert transcriber.device == "auto"
    assert transcriber.compute_type == "auto"


# --- download_item dispatches to _prepare_model_source -----------------


def test_download_item_routes_to_prepare(monkeypatch, transcriber):
    captured: dict[str, Any] = {}

    def _fake_prepare(item_name, progress_callback=None):
        captured["item"] = item_name
        captured["cb"] = progress_callback
        return "/fake/source"

    monkeypatch.setattr(transcriber, "_prepare_model_source", _fake_prepare)

    cb = MagicMock()
    transcriber.download_item("base.en", progress_callback=cb)
    assert captured == {"item": "base.en", "cb": cb}
