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
import wave

from voiceagent.backends import TextToSpeechBackend
from voiceagent.downloaders import AriaDownloader, DownloadFile, DownloadProgress

# Full voice catalog for `voices-v1.0.bin` (thewh1teagle bundle,
# `model-files-v1.0` release — 54 voices spanning American/British
# English, Spanish, French, Hindi, Italian, Japanese, Brazilian
# Portuguese and Mandarin Chinese). Used pre-download so the UI can show
# the catalog before any bytes hit disk. Once the engine loads,
# `available_items()` overlays whatever the live `kokoro.voices` mapping
# actually exposes — the on-disk bundle is authoritative if it ever
# diverges from this static list.
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
    MODEL_FILENAME = "kokoro-v1.0.onnx"
    VOICES_FILENAME = "voices-v1.0.bin"
    RELEASE_URL_BASE = (
        "https://github.com/thewh1teagle/kokoro-onnx/releases/download/"
        "model-files-v1.0/"
    )
    SAMPLE_RATE = 24_000

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
        if self._bundle_available():
            return
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
        # Layer-2/3 verification skipped: GitHub release assets don't ship
        # per-file checksums in a machine-readable manifest and the
        # `model-files-v1.0` tag is immutable. Layer 1 (existence +
        # sidecar guard, in `_bundle_available`) and layer 4 (smoke-load
        # on first synth, in `_load_kokoro`) still apply.
        return {}

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
