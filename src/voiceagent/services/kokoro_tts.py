"""Kokoro TTS engine scaffolding — shelved 2026-05-06.

The implementation below was drafted for v0.12 and then shelved without
being wired into the engine selector or any user-facing UI. The reason:
upstream `kokoro-onnx` (`requires-python <3.14`) and `misaki`
(`requires-python <3.13`) do not yet support Python 3.14, which is the
current Manjaro / Arch default. `pip install voiceagent[kokoro]` would
fail on the typical target install. Shipping a UI option that doesn't
install on the user's Python was rejected as a half-working feature;
the file is kept as a starting point for whichever future cycle either
revives Kokoro (when upstream catches up) or supersedes it with
Chatterbox in v0.13.

Nothing imports this module. The class below raises immediately if
instantiated. The full prior implementation is preserved verbatim in
the docstring `_REFERENCE_IMPLEMENTATION` so the design (single-bundle
download, `.aria2` sidecar trap, lazy import of kokoro_onnx + misaki +
soundfile, static 26-voice catalog with live override post-load) is
not lost.

To resurrect: lift the body of `_REFERENCE_IMPLEMENTATION` back into
this module, re-add the `[kokoro]` extras to `pyproject.toml`, restore
the engine-selector hooks in `app.py` / `config.py` / `window.py` /
QML, and re-add the tests under `tests/test_kokoro_tts.py` and
`tests/test_engine_switching.py`. See the `kokoro-tts-engine` branch's
pre-shelf commits in this repo's history for the live versions.
"""

from __future__ import annotations


class KokoroTtsService:
    """Placeholder. Instantiation is unsupported in this release."""

    backend_name = "Kokoro"
    selection_label = "Voice"

    def __init__(self, *args, **kwargs) -> None:
        raise NotImplementedError(
            "KokoroTtsService is shelved pending upstream Python 3.14 "
            "support in `kokoro-onnx` and `misaki`. See module docstring."
        )


_REFERENCE_IMPLEMENTATION = r'''
# Single-bundle architecture: `kokoro-v1.0.onnx` + `voices-v1.0.bin` are
# downloaded together as one logical install. Every voice the user can
# select lives inside `voices-v1.0.bin`, so per-voice download
# granularity does not apply — `download_item("af_heart")` and
# `download_item("am_michael")` both fetch the same two files.
#
# Imports of `kokoro_onnx`, `misaki`, and `soundfile` are deferred to
# `_load_kokoro()` so the service can be instantiated without those
# extras installed; only attempted use raises.

from __future__ import annotations

import logging
import os
import tempfile
import threading
from pathlib import Path

from voiceagent.backends import TextToSpeechBackend
from voiceagent.downloaders import AriaDownloader, DownloadFile, DownloadProgress

# Static expected voice catalog for `voices-v1.0.bin` (thewh1teagle
# bundle, 26 voices). Used pre-download so the UI can show the catalog
# before any bytes have hit disk. After the Kokoro engine loads,
# `available_items()` overlays whatever the live `kokoro.voices`
# mapping actually exposes — the bundle is authoritative once present.
_DEFAULT_BUNDLED_VOICES = (
    "af_alloy", "af_aoede", "af_bella", "af_heart", "af_jessica",
    "af_kore", "af_nicole", "af_nova", "af_river", "af_sarah", "af_sky",
    "am_adam", "am_echo", "am_eric", "am_fenrir", "am_liam",
    "am_michael", "am_onyx", "am_puck",
    "bf_alice", "bf_emma", "bf_isabella", "bf_lily",
    "bm_daniel", "bm_fable", "bm_george",
)
_DEFAULT_VOICE = "af_heart"


class KokoroTtsService:
    backend_name = "Kokoro"
    selection_label = "Voice"
    MODEL_FILENAME = "kokoro-v1.0.onnx"
    VOICES_FILENAME = "voices-v1.0.bin"
    RELEASE_URL_BASE = (
        "https://github.com/thewh1teagle/kokoro-onnx/releases/download/"
        "model-files-v1.0/"
    )
    SAMPLE_RATE = 24_000

    def __init__(self, model_root, selected_item=None):
        self.model_root = Path(model_root)
        self._selected_item = selected_item or _DEFAULT_VOICE
        self.downloader = AriaDownloader(connections=10)
        self._logger = logging.getLogger(__name__)
        self._kokoro = None
        self._kokoro_lock = threading.Lock()

    @property
    def model_path(self): return self.model_root / self.MODEL_FILENAME
    @property
    def voices_path(self): return self.model_root / self.VOICES_FILENAME
    @property
    def enabled(self): return True
    @property
    def is_available(self): return self._bundle_available()
    @property
    def selected_item(self): return self._selected_item

    def _bundle_available(self):
        # Both files exist AND neither has a stale `.aria2` sidecar. The
        # sidecar guard mirrors v0.8.7's PiperTtsService behavior: aria2
        # leaves `<file>.aria2` on interrupted downloads and the partial
        # `.onnx` next to it crashes ONNX Runtime cryptically. Refusing
        # to report "available" while a sidecar exists surfaces it as
        # "needs re-download" instead.
        for path in (self.model_path, self.voices_path):
            if not path.exists() or Path(f"{path}.aria2").exists():
                return False
        return True

    def available_items(self):
        live = self._loaded_voice_names()
        return live if live is not None else list(_DEFAULT_BUNDLED_VOICES)

    def is_item_available(self, item_name):
        return self._bundle_available() and item_name in self.available_items()

    def is_item_managed(self, item_name):
        return item_name in self.available_items()

    def is_item_downloadable(self, item_name):
        return item_name in self.available_items()

    def set_selected_item(self, item_name):
        self._selected_item = item_name or _DEFAULT_VOICE

    def synthesize(self, text, progress_callback=None):
        if not self.is_available:
            raise RuntimeError("Kokoro voice pack is not downloaded.")
        voice = self._selected_item or _DEFAULT_VOICE
        fd, raw_path = tempfile.mkstemp(prefix="voiceagent-tts-", suffix=".wav")
        os.close(fd)
        Path(raw_path).unlink(missing_ok=True)
        output_path = Path(raw_path)
        try:
            kokoro = self._load_kokoro()
            samples, sample_rate = kokoro.create(
                text, voice=voice, speed=1.0, lang="en-us"
            )
            self._write_wav(output_path, samples, sample_rate)
        except Exception as exc:
            output_path.unlink(missing_ok=True)
            raise RuntimeError(str(exc) or "Kokoro synthesis failed.") from exc
        if not output_path.exists() or output_path.stat().st_size == 0:
            output_path.unlink(missing_ok=True)
            raise RuntimeError("Kokoro did not create an audio file.")
        return output_path

    def download_selected_item(self, progress_callback=None):
        self.download_item(
            self._selected_item or _DEFAULT_VOICE,
            progress_callback=progress_callback,
        )

    def download_item(self, item_name, progress_callback=None):
        # Bundle is the unit; any voice name maps to the same fetch.
        if self._bundle_available():
            return
        self.model_root.mkdir(parents=True, exist_ok=True)
        files = [
            DownloadFile(
                url=f"{self.RELEASE_URL_BASE}{self.MODEL_FILENAME}",
                destination=self.model_path,
                size_bytes=self.downloader.get_remote_size(
                    f"{self.RELEASE_URL_BASE}{self.MODEL_FILENAME}"
                ),
            ),
            DownloadFile(
                url=f"{self.RELEASE_URL_BASE}{self.VOICES_FILENAME}",
                destination=self.voices_path,
                size_bytes=self.downloader.get_remote_size(
                    f"{self.RELEASE_URL_BASE}{self.VOICES_FILENAME}"
                ),
            ),
        ]
        callback = progress_callback or (lambda progress: None)
        callback(DownloadProgress(
            completed_bytes=0,
            total_bytes=sum(f.size_bytes for f in files),
            download_speed_bytes_per_second=0,
        ))
        self.downloader.download(files, progress_callback=callback)
        with self._kokoro_lock:
            self._kokoro = None

    def remove_item(self, item_name):
        # One voice in a bundle is meaningless; remove the whole pack.
        for path in (self.model_path, self.voices_path):
            path.unlink(missing_ok=True)
            Path(f"{path}.aria2").unlink(missing_ok=True)
        with self._kokoro_lock:
            self._kokoro = None

    def artifact_paths(self, item_name):
        return [self.model_path, self.voices_path]

    def artifact_manifest(self, item_name):
        # Layer-2/3 verification skipped for v1: GitHub release assets
        # don't ship per-file checksums in a machine-readable manifest,
        # and the tag is stable. Layer-1 (existence + sidecar) and
        # layer-4 (smoke-load on first synth) still apply.
        return {}

    def _load_kokoro(self):
        with self._kokoro_lock:
            if self._kokoro is not None:
                return self._kokoro
            try:
                from kokoro_onnx import Kokoro
            except ImportError as exc:
                raise RuntimeError(
                    "Kokoro extras are not installed. Install with "
                    "`pip install voiceagent[kokoro]` (Python 3.10-3.12)."
                ) from exc
            self._kokoro = Kokoro(str(self.model_path), str(self.voices_path))
            return self._kokoro

    def _loaded_voice_names(self):
        with self._kokoro_lock:
            kokoro = self._kokoro
        if kokoro is None:
            return None
        voices = getattr(kokoro, "voices", None)
        if voices is None:
            return None
        try:
            return sorted(voices)
        except Exception:
            return None

    @staticmethod
    def _write_wav(output_path, samples, sample_rate):
        try:
            import soundfile
        except ImportError as exc:
            raise RuntimeError(
                "soundfile is required to write Kokoro audio."
            ) from exc
        soundfile.write(str(output_path), samples, sample_rate)
'''
