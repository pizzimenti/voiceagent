"""Kokoro TTS engine — hexgrad/kokoro-onnx, single-bundle architecture.

Revived for v0.14 (was shelved 2026-05-06 pending Python 3.14 support).
The original shelf reason was upstream's `requires-python <3.14` cap on
`kokoro-onnx`, but a 2026-06-06 spike confirmed that cap is conservative
metadata only: `pip install --ignore-requires-python kokoro-onnx` (0.5.0)
resolves native cp314 wheels for `numpy` and `onnxruntime`, imports
cleanly, and synthesizes valid audio on Python 3.14.5. `kokoro-onnx`
phonemizes via `phonemizer-fork` (espeak-ng, bundled through
`espeakng-loader` — no system espeak-ng package needed); the `misaki`
g2p (which still caps `<3.13`) is optional and NOT used here, so its cap
does not block us. See `pyproject.toml`'s `[kokoro]` extra and the
`--ignore-requires-python` note in PKGBUILD.

Single-bundle architecture: `kokoro-v1.0.onnx` (~325 MB) + `voices-v1.0.bin`
(~28 MB) are downloaded together as one logical install. Every voice the
user can select lives inside `voices-v1.0.bin`, so per-voice download
granularity does not apply — `download_item("af_heart")` and
`download_item("am_michael")` both fetch the same two files. This is the
key difference from Piper (one file pair per voice) and the reason the
catalog reports every voice as "downloadable" / "managed" up front: the
bundle is the unit, the voice is a selection within it.

Imports of `kokoro_onnx` are deferred to `_load_kokoro()` so the service
can be instantiated (and the engine listed in the selector) without the
`[kokoro]` extras installed; only attempted synthesis raises. WAV is
written with the stdlib `wave` module (float32 → int16 PCM), mirroring
`PiperTtsService` — no `soundfile` dependency.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
import tempfile
import threading
from typing import ClassVar
import wave

from voiceagent.backends import TextToSpeechBackend
from voiceagent.downloaders import AriaDownloader, DownloadFile, DownloadProgress

# Every module `from kokoro_onnx import Kokoro` + a first synthesis
# transitively imports. The extras gate in `app.py` / `window.py` probes
# this whole set via `find_spec` (cheap — no heavy import on the startup
# path) so a partial install (`--no-deps`, a distro package that omits a
# transitive dep) is caught up front and falls back to Piper, rather than
# passing a wrapper-only probe and crashing at first synth. `kokoro_onnx`
# loads ONNX Runtime + numpy; its `tokenizer` submodule imports
# `phonemizer` (phonemizer-fork) and `espeakng_loader` for espeak-ng
# phonemization. Defined here as the single source of truth so the two
# mirrored probes can't drift apart.
KOKORO_EXTRA_MODULES: tuple[str, ...] = (
    "kokoro_onnx",
    "onnxruntime",
    "numpy",
    "phonemizer",
    "espeakng_loader",
)

# Full voice catalog for `voices-v1.0.bin` (thewh1teagle bundle,
# `model-files-v1.0` release — 54 voices spanning American/British
# English, Spanish, French, Hindi, Italian, Japanese, Brazilian
# Portuguese and Mandarin Chinese). Used pre-download so the UI can show
# the catalog before any bytes hit disk. Once the engine loads,
# `available_items()` overlays whatever the live `kokoro.voices` mapping
# actually exposes — the on-disk bundle is authoritative if it ever
# diverges from this static list.
#
# This list is NOT the stale "26 voices" figure from older upstream
# READMEs — the `model-files-v1.0` `voices-v1.0.bin` was expanded to 54.
# Verified byte-exact against the SHA-256-pinned bundle (see
# `_VOICES_SHA256`): `set(_DEFAULT_BUNDLED_VOICES) == set(kokoro.voices)`
# with zero phantom or missing entries, and every non-English voice
# (e.g. `ef_dora`) synthesizes. Because the bundle is SHA-pinned and
# immutable, this static list cannot drift out of sync with what an
# install actually delivers.
_DEFAULT_BUNDLED_VOICES = (
    "af_alloy", "af_aoede", "af_bella", "af_heart", "af_jessica",
    "af_kore", "af_nicole", "af_nova", "af_river", "af_sarah", "af_sky",
    "am_adam", "am_echo", "am_eric", "am_fenrir", "am_liam",
    "am_michael", "am_onyx", "am_puck", "am_santa",
    "bf_alice", "bf_emma", "bf_isabella", "bf_lily",
    "bm_daniel", "bm_fable", "bm_george", "bm_lewis",
    "ef_dora", "em_alex", "em_santa",
    "ff_siwis",
    "hf_alpha", "hf_beta", "hm_omega", "hm_psi",
    "if_sara", "im_nicola",
    "jf_alpha", "jf_gongitsune", "jf_nezumi", "jf_tebukuro", "jm_kumo",
    "pf_dora", "pm_alex", "pm_santa",
    "zf_xiaobei", "zf_xiaoni", "zf_xiaoxiao", "zf_xiaoyi",
    "zm_yunjian", "zm_yunxi", "zm_yunxia", "zm_yunyang",
)
_DEFAULT_VOICE = "af_heart"

# Map a voice's leading prefix character to the espeak-ng language code
# `kokoro.create(lang=...)` should use for phonemization. Kokoro encodes
# the language in the first letter of every voice id (a=American English,
# b=British, e=Spanish, f=French, h=Hindi, i=Italian, j=Japanese,
# p=Brazilian Portuguese, z=Mandarin). `phonemize()` passes `lang`
# straight through to espeak, so these are espeak voice names. Verified
# 2026-06-06 that all nine synthesize without error on the bundled
# espeak-ng. Quality is English-strongest (the proper JP/ZH g2p, misaki,
# is unavailable on 3.14); non-English voices fall back to espeak's
# coarser phonemization, which is why the README keeps the default engine
# on Piper for non-English users.
_VOICE_PREFIX_TO_LANG = {
    "a": "en-us",
    "b": "en-gb",
    "e": "es",
    "f": "fr-fr",
    "h": "hi",
    "i": "it",
    "j": "ja",
    "p": "pt-br",
    "z": "cmn",
}
_DEFAULT_LANG = "en-us"


class KokoroTtsService(TextToSpeechBackend):
    backend_name = "Kokoro"
    selection_label = "Voice"
    # Availability is bundle-wide: installing or removing ANY voice flips
    # the on-disk state for all 54 at once (they share one bundle). The
    # window consults this flag to refresh every catalog row after an
    # install/delete completes, instead of just the clicked one — Piper /
    # Chatterbox change one voice at a time and leave this unset.
    catalog_is_shared_bundle = True
    MODEL_FILENAME = "kokoro-v1.0.onnx"
    VOICES_FILENAME = "voices-v1.0.bin"
    RELEASE_URL_BASE = (
        "https://github.com/thewh1teagle/kokoro-onnx/releases/download/"
        "model-files-v1.0/"
    )
    SAMPLE_RATE = 24_000
    # Pinned SHA-256 + byte size for the two `model-files-v1.0` release
    # assets. The tag is an immutable GitHub release, so these never
    # drift. Consumed by `artifact_manifest` → the download verifier
    # (`ParallelItemLoader._verify_download`, layers 2/3) compares the
    # on-disk bytes against these before `_bundle_available()` can pass
    # and `_load_kokoro()` hands the file to ONNX Runtime — so a tampered
    # or truncated release asset fails closed at install time instead of
    # crashing cryptically on first synth.
    _MODEL_SHA256 = "7d5df8ecf7d4b1878015a32686053fd0eebe2bc377234608764cc0ef3636a6c5"
    _MODEL_SIZE_BYTES = 325_532_387
    _VOICES_SHA256 = "bca610b8308e8d99f32e6fe4197e7ec01679264efed0cac9140fe9c29f1fbf7d"
    _VOICES_SIZE_BYTES = 28_214_398

    # PROCESS-WIDE download locks, keyed by resolved bundle directory.
    # The serialization must NOT be per-instance: `_perform_tts_engine_swap`
    # replaces the live `KokoroTtsService`, and a long-running aria2 worker
    # can outlive the old loader's bounded `shutdown()`. If the user starts
    # the ~350 MB install, swaps engine away, then swaps back, the new
    # instance would otherwise get a fresh per-instance lock and — because
    # the partial files still carry `.aria2` sidecars, so `_bundle_available`
    # is False — could launch a SECOND aria2 against the same destinations
    # and corrupt the bundle. Keying a class-level lock on `model_root`
    # makes every instance pointing at the same bundle share one lock.
    _download_locks: ClassVar[dict[Path, threading.Lock]] = {}
    _download_locks_guard: ClassVar[threading.Lock] = threading.Lock()

    @classmethod
    def _bundle_download_lock(cls, model_root: Path) -> threading.Lock:
        key = Path(model_root).resolve()
        with cls._download_locks_guard:
            lock = cls._download_locks.get(key)
            if lock is None:
                lock = threading.Lock()
                cls._download_locks[key] = lock
            return lock

    def __init__(self, model_root, selected_item: str | None = None) -> None:
        self.model_root = Path(model_root)
        self._selected_item = selected_item or _DEFAULT_VOICE
        self.downloader = AriaDownloader(connections=10)
        self._logger = logging.getLogger(__name__)
        # Lazily-constructed `kokoro_onnx.Kokoro`. Guarded by a lock so a
        # download-triggered reset (which nulls it) can't race a
        # concurrent synth reading it.
        self._kokoro = None
        self._kokoro_lock = threading.Lock()

    @property
    def model_path(self) -> Path:
        return self.model_root / self.MODEL_FILENAME

    @property
    def voices_path(self) -> Path:
        return self.model_root / self.VOICES_FILENAME

    @property
    def enabled(self) -> bool:
        return True

    @property
    def is_available(self) -> bool:
        return self._bundle_available()

    @property
    def is_engine_ready(self) -> bool:
        """Whether the shared bundle is on disk and synthesizable.

        The catalog lists all 54 voices the moment Kokoro is selected
        (they all live in the not-yet-downloaded bundle), so
        `selected_item` / `selectedTtsModel` are non-empty before any
        bytes hit disk. `MainWindow.talkReady` consults this property
        (same contract as Chatterbox's shared-bundle readiness) to keep
        the mic disabled until the bundle is actually present — otherwise
        the first turn would enable, then fail at synth.
        """
        return self._bundle_available()

    @property
    def selected_item(self) -> str | None:
        return self._selected_item

    def _bundle_available(self) -> bool:
        # Both files exist AND neither carries a stale `.aria2` sidecar.
        # The sidecar guard mirrors PiperTtsService (v0.8.7): aria2 leaves
        # `<file>.aria2` on an interrupted transfer and the partial file
        # next to it crashes ONNX Runtime cryptically. Refusing to report
        # "available" while a sidecar exists surfaces it as "needs
        # re-download" instead of a baffling load error on first synth.
        for path in (self.model_path, self.voices_path):
            if not path.exists() or Path(f"{path}.aria2").exists():
                return False
        return True

    def available_items(self) -> list[str]:
        live = self._loaded_voice_names()
        return live if live is not None else list(_DEFAULT_BUNDLED_VOICES)

    def is_item_available(self, item_name: str) -> bool:
        return self._bundle_available() and item_name in self.available_items()

    def is_item_managed(self, item_name: str) -> bool:
        return item_name in self.available_items()

    def is_item_downloadable(self, item_name: str) -> bool:
        # Every catalog voice is "downloadable" because installing any one
        # of them fetches the shared bundle that contains all of them.
        return item_name in self.available_items()

    def set_selected_item(self, item_name: str | None) -> None:
        self._selected_item = item_name or _DEFAULT_VOICE

    def synthesize(self, text: str, progress_callback=None) -> Path | None:
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
                text, voice=voice, speed=1.0, lang=self._lang_for_voice(voice),
            )
            self._write_wav(output_path, samples, sample_rate)
        except Exception as exc:
            output_path.unlink(missing_ok=True)
            raise RuntimeError(str(exc) or "Kokoro synthesis failed.") from exc
        if not output_path.exists() or output_path.stat().st_size == 0:
            output_path.unlink(missing_ok=True)
            raise RuntimeError("Kokoro did not create an audio file.")
        return output_path

    def download_selected_item(self, progress_callback=None) -> None:
        self.download_item(
            self._selected_item or _DEFAULT_VOICE,
            progress_callback=progress_callback,
        )

    def download_item(self, item_name: str, progress_callback=None) -> None:
        # The bundle is the unit; any voice name maps to the same fetch.
        # Fast-path: skip the lock entirely once the bundle is on disk.
        if self._bundle_available():
            return
        # Double-checked locking on the PROCESS-WIDE, model_root-keyed lock
        # (not a per-instance one) so a service replaced mid-download by an
        # engine swap still serializes against the in-flight aria2 worker.
        # The first caller downloads; any caller blocked on the lock
        # re-checks and finds the bundle present, so only one aria2 run
        # ever touches these files.
        with self._bundle_download_lock(self.model_root):
            if self._bundle_available():
                return
            self._download_bundle(progress_callback)

    def _download_bundle(self, progress_callback=None) -> None:
        self.model_root.mkdir(parents=True, exist_ok=True)
        model_url = f"{self.RELEASE_URL_BASE}{self.MODEL_FILENAME}"
        voices_url = f"{self.RELEASE_URL_BASE}{self.VOICES_FILENAME}"
        files = [
            DownloadFile(
                url=model_url,
                destination=self.model_path,
                size_bytes=self.downloader.get_remote_size(model_url),
            ),
            DownloadFile(
                url=voices_url,
                destination=self.voices_path,
                size_bytes=self.downloader.get_remote_size(voices_url),
            ),
        ]
        callback = progress_callback or (lambda progress: None)
        callback(DownloadProgress(
            completed_bytes=0,
            total_bytes=sum(f.size_bytes for f in files),
            download_speed_bytes_per_second=0,
        ))
        self.downloader.download(files, progress_callback=callback)
        # Force the next synth to reload against the freshly-downloaded
        # bundle rather than a stale (possibly None-but-cached) handle.
        with self._kokoro_lock:
            self._kokoro = None

    def remove_item(self, item_name: str) -> None:
        # A single voice within a shared bundle is meaningless to remove;
        # deleting any catalog entry removes the whole pack (both files
        # plus any aria2 sidecars).
        for path in (self.model_path, self.voices_path):
            path.unlink(missing_ok=True)
            Path(f"{path}.aria2").unlink(missing_ok=True)
        with self._kokoro_lock:
            self._kokoro = None

    def artifact_paths(self, item_name: str) -> list[Path]:
        return [self.model_path, self.voices_path]

    def artifact_manifest(self, item_name: str) -> dict:
        # Layers 2 (size) + 3 (sha256) for both bundle files, pinned to
        # the immutable `model-files-v1.0` release assets. The verifier
        # streams each downloaded file through sha256 and rejects a
        # mismatch fail-closed, so a corrupted or tampered download never
        # reaches ONNX Runtime. Layer 1 (existence + `.aria2` sidecar
        # guard, in `_bundle_available`) and layer 4 (smoke-load on first
        # synth, in `_load_kokoro`) also apply. Keyed by the same paths
        # `artifact_paths` returns so the verifier maps entries onto the
        # on-disk files.
        from voiceagent.parallel_item_loader import ArtifactManifestEntry

        return {
            self.model_path: ArtifactManifestEntry(
                expected_size=self._MODEL_SIZE_BYTES,
                expected_checksum_hex=self._MODEL_SHA256,
                checksum_algorithm="sha256",
            ),
            self.voices_path: ArtifactManifestEntry(
                expected_size=self._VOICES_SIZE_BYTES,
                expected_checksum_hex=self._VOICES_SHA256,
                checksum_algorithm="sha256",
            ),
        }

    def _lang_for_voice(self, voice: str) -> str:
        prefix = voice[:1].lower() if voice else ""
        return _VOICE_PREFIX_TO_LANG.get(prefix, _DEFAULT_LANG)

    def _load_kokoro(self):
        with self._kokoro_lock:
            if self._kokoro is not None:
                return self._kokoro
            try:
                from kokoro_onnx import Kokoro
            except ImportError as exc:
                raise RuntimeError(
                    "Kokoro extras are not installed. Install with "
                    "`pip install --ignore-requires-python voiceagent[kokoro]`."
                ) from exc
            self._kokoro = Kokoro(str(self.model_path), str(self.voices_path))
            return self._kokoro

    def _loaded_voice_names(self) -> list[str] | None:
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
    def _write_wav(output_path: Path, samples, sample_rate: int) -> None:
        """Write float32 mono samples to a 16-bit PCM WAV via stdlib.

        Mirrors PiperTtsService's stdlib-`wave` path so Kokoro needs no
        `soundfile` dependency. `samples` is the float32 ndarray Kokoro
        returns (range roughly [-1, 1]); clip then scale to int16.
        """
        import numpy as np

        pcm = np.clip(np.asarray(samples, dtype=np.float32), -1.0, 1.0)
        pcm = (pcm * 32767.0).astype("<i2")
        with wave.open(str(output_path), "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(int(sample_rate))
            wav_file.writeframes(pcm.tobytes())
