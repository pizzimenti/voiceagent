"""Chatterbox-Turbo-ONNX TTS engine (voice-cloning).

Wraps `ResembleAI/chatterbox-turbo-ONNX` — a 4-component ONNX model
(speech_encoder, embed_tokens, language_model, conditional_decoder)
with quantized variants. The synthesis path is adapted from the
`/tmp/chatterbox_probe.py` probe that validated CPU performance:
KV-cached LM autoregression to STOP_SPEECH_TOKEN, then
conditional-decoder pass to PCM samples.

Architectural divergence from Kokoro: Chatterbox ships no built-in
voices. The "voice" is a user-supplied reference WAV clip. The voice
catalog is therefore the contents of `references_root` (one entry
per `*.wav` file). The downloaded artifact is the model itself,
shared across every voice — `download_item("alice")` and
`download_item("bob")` both trigger the same one-time model fetch.
`remove_item(name)` deletes only the reference clip, never the
model.

Imports of `onnxruntime`, `transformers`, `librosa`, `soundfile`,
`huggingface_hub`, and `numpy` are deferred to method bodies so the
module can be imported without the `[chatterbox]` extras installed.
First synthesis caches the four ONNX sessions and the tokenizer on
the instance under `_load_lock` — initial load is several seconds
and dominates short-utterance RTF without the cache.
"""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from voiceagent.backends import TextToSpeechBackend
from voiceagent.downloaders import DownloadProgress

if TYPE_CHECKING:
    from voiceagent.parallel_item_loader import ArtifactManifestEntry  # noqa: F401


_logger = logging.getLogger(__name__)


# The 4 ONNX components published by `ResembleAI/chatterbox-turbo-ONNX`
# under the `onnx/` subfolder. Each name appears with a per-dtype
# suffix (see `_filename_for`).
_COMPONENTS = (
    "conditional_decoder",
    "speech_encoder",
    "embed_tokens",
    "language_model",
)

# Approximate q4 bundle size (4 components, sums to ~700 MiB on disk).
# Used as a hardcoded total for progress reporting because
# `huggingface_hub.hf_hub_download` does not surface per-byte progress
# in a stable callback API.
_APPROX_TOTAL_BYTES_Q4 = 700 * 1024 * 1024

# Approximate full-bundle sizes per quantization variant. Sourced from
# the file listing on `ResembleAI/chatterbox-turbo-ONNX` as of May 2026
# — the four ONNX components plus their `_data` weight sidecars.
# Used as the denominator for the download progress UI; per-component
# completion reads the actual on-disk file size from the HF cache so
# progress numbers stay correct even if a future repo update shifts a
# variant's footprint.
_APPROX_BUNDLE_BYTES: dict[str, int] = {
    "q4":    700 * 1024 * 1024,
    "q4f16": 750 * 1024 * 1024,
    "fp16": 1400 * 1024 * 1024,
    "fp32": 2500 * 1024 * 1024,
}


class ChatterboxTtsService(TextToSpeechBackend):
    backend_name = "Chatterbox"
    selection_label = "Voice"
    HF_REPO = "ResembleAI/chatterbox-turbo-ONNX"
    SAMPLE_RATE = 24_000
    DEFAULT_DTYPE = "q4"

    # Token IDs from the model card.
    START_SPEECH_TOKEN = 6561
    STOP_SPEECH_TOKEN = 6562
    SILENCE_TOKEN = 4299
    NUM_KV_HEADS = 16
    HEAD_DIM = 64

    # Generation cap. Mirrors the probe; ~1024 tokens covers ~30s of
    # speech at the model's frame rate. Hitting the cap without a
    # STOP_SPEECH_TOKEN is rare in practice and surfaces as a
    # truncated waveform rather than a hang.
    MAX_NEW_TOKENS = 1024

    # User-selectable model precision variants. Higher precision = better
    # tonal fidelity (q4 quantization rounds away high-frequency tail of
    # speaker embeddings, where pitch lives) at the cost of disk + RAM.
    SUPPORTED_DTYPES: tuple[str, ...] = ("q4", "q4f16", "fp16", "fp32")

    def __init__(
        self,
        model_root: Path,
        references_root: Path,
        selected_item: str | None = None,
        dtype: str | None = None,
    ) -> None:
        self.model_root = Path(model_root)
        self.references_root = Path(references_root)
        self._selected_item = selected_item
        self._dtype = dtype if dtype in self.SUPPORTED_DTYPES else self.DEFAULT_DTYPE
        self._logger = _logger
        # Cached ONNX sessions + tokenizer. `None` until first synth
        # (or first explicit `_load_models` call). The lock serializes
        # concurrent loads; warm reads are fast-path.
        self._sessions: dict[str, Any] | None = None
        self._tokenizer: Any | None = None
        self._load_lock = threading.Lock()
        self.references_root.mkdir(parents=True, exist_ok=True)

    @property
    def dtype(self) -> str:
        return self._dtype

    def set_dtype(self, value: str) -> None:
        """Switch the active model variant. Invalidates the cached
        sessions (different .onnx graph at the new precision) and the
        tokenizer (per-dtype cache key). Subsequent synth calls
        re-resolve against the new variant; if it isn't downloaded the
        synth raises and the engine-download UI fires.
        """
        if value not in self.SUPPORTED_DTYPES:
            self._logger.warning(
                "ignoring unsupported chatterbox dtype %r (allowed: %s)",
                value, ", ".join(self.SUPPORTED_DTYPES),
            )
            return
        if value == self._dtype:
            return
        self._dtype = value
        with self._load_lock:
            self._sessions = None
            self._tokenizer = None

    # ------------------------------------------------------------------ #
    # Backend protocol — properties                                       #
    # ------------------------------------------------------------------ #

    @property
    def enabled(self) -> bool:
        return True

    @property
    def is_available(self) -> bool:
        # "Synthesizable right now" — both the user-supplied reference
        # clip AND the shared engine model must be present. Used by
        # the replay path / talk-ready calculation.
        name = self._selected_item
        if not name:
            return False
        return self._reference_path(name).exists() and self._model_present()

    @property
    def is_engine_ready(self) -> bool:
        """The shared 4-component ONNX model bundle is on disk. Distinct
        from voice (reference clip) presence — engine readiness applies
        to all voices in the catalog at once. Drives the UI banner that
        prompts the user to download the model, separate from the per-
        voice catalog rows.
        """
        return self._model_present()

    @property
    def selected_item(self) -> str | None:
        return self._selected_item

    # ------------------------------------------------------------------ #
    # Backend protocol — catalog                                          #
    # ------------------------------------------------------------------ #
    #
    # Two-layer state model: per-voice (reference clip) and per-engine
    # (model bundle). The CatalogList delegate's `installed` /
    # `downloadable` / `managed` roles describe per-voice state only —
    # a voice is "installed" iff its reference clip is on disk,
    # regardless of whether the shared engine model has been
    # downloaded. The engine state surfaces separately through the
    # `is_engine_ready` property and the engine-download UI in
    # `ChatterboxTtsConfigPane.qml`.
    #
    # Why split: a freshly-imported voice is fully "owned" by the user
    # (file is on disk, ready to be cloned) but cannot synthesize until
    # the 700 MB model arrives. Conflating both into `is_item_available`
    # made the catalog misreport imported voices as "Available to
    # download" with an Install button that confusingly fetched the
    # engine model.

    def available_items(self) -> list[str]:
        self.references_root.mkdir(parents=True, exist_ok=True)
        try:
            entries = [p.stem for p in self.references_root.glob("*.wav") if p.is_file()]
        except OSError:
            return []
        return sorted(entries, key=str.lower)

    def is_item_available(self, item_name: str) -> bool:
        # Per-voice readiness only (reference clip on disk). Engine
        # state is checked separately at synth time.
        if not item_name:
            return False
        return self._reference_path(item_name).exists()

    def is_item_managed(self, item_name: str) -> bool:
        return self.is_item_available(item_name)

    def is_item_downloadable(self, item_name: str) -> bool:
        # Voices are user-supplied (mic record / file import) — never
        # downloads. The shared engine-model download surfaces through
        # `download_engine_model` and the QML engine-state banner.
        return False

    def set_selected_item(self, item_name: str | None) -> None:
        if not item_name:
            self._selected_item = None
            return
        candidates = self.available_items()
        if item_name in candidates:
            self._selected_item = item_name
            return
        # Silent fallback to first available; matches the docstring
        # contract for non-existent selections.
        self._selected_item = candidates[0] if candidates else item_name

    # ------------------------------------------------------------------ #
    # Backend protocol — synthesis                                        #
    # ------------------------------------------------------------------ #

    def synthesize(
        self,
        text: str,
        progress_callback: Callable[[DownloadProgress], None] | None = None,
    ) -> Path | None:
        del progress_callback  # synth itself emits no byte-progress events
        name = self._selected_item
        if not name:
            raise RuntimeError("No Chatterbox voice selected.")
        ref_path = self._reference_path(name)
        if not ref_path.exists():
            raise RuntimeError(
                f"Chatterbox reference clip is missing: {ref_path}"
            )
        if not self._model_present():
            raise RuntimeError(
                "Chatterbox model is not downloaded. Trigger download first."
            )

        np, ort, transformers_mod, librosa, soundfile = self._import_extras()
        sessions, tokenizer = self._load_models()

        input_ids = tokenizer(text, return_tensors="np")["input_ids"].astype(np.int64)

        rep_penalty = _RepetitionPenaltyLogitsProcessor(np, penalty=1.2)
        generate_tokens = np.array([[self.START_SPEECH_TOKEN]], dtype=np.int64)

        embed_tokens_session = sessions["embed_tokens"]
        speech_encoder_session = sessions["speech_encoder"]
        language_model_session = sessions["language_model"]
        cond_decoder_session = sessions["conditional_decoder"]

        # Speaker-features cache. The output of `speech_encoder.run()` is
        # deterministic for a given (reference clip, dtype) pair, but the
        # encoder pass scales linearly with reference duration — at the
        # 60 s recording max the per-synth encoder cost would otherwise
        # be ~1 s every call. Cache the 4-tuple on disk as `<voice>.<dtype>.npz`
        # next to the WAV; subsequent synths skip the encoder entirely.
        # Cache invalidates automatically when dtype changes (the filename
        # is dtype-keyed) and explicitly when the voice is removed or
        # re-imported (see `remove_item` / `import_reference_clip`).
        cond_emb, prompt_token, speaker_embeddings, speaker_features = (
            self._load_or_compute_speaker_features(
                name, ref_path, np, librosa, speech_encoder_session,
            )
        )

        past_key_values: dict[str, Any] = {}
        attention_mask = None
        position_ids = None

        for i in range(self.MAX_NEW_TOKENS):
            inputs_embeds = embed_tokens_session.run(None, {"input_ids": input_ids})[0]

            if i == 0:
                # Speaker features were precomputed via the cache above;
                # only the per-iteration LM-input wiring belongs here.
                inputs_embeds = np.concatenate((cond_emb, inputs_embeds), axis=1)

                batch_size, seq_len, _ = inputs_embeds.shape
                past_key_values = {
                    inp.name: np.zeros(
                        [batch_size, self.NUM_KV_HEADS, 0, self.HEAD_DIM],
                        dtype=np.float16
                        if inp.type == "tensor(float16)"
                        else np.float32,
                    )
                    for inp in language_model_session.get_inputs()
                    if "past_key_values" in inp.name
                }
                attention_mask = np.ones((batch_size, seq_len), dtype=np.int64)
                position_ids = (
                    np.arange(seq_len, dtype=np.int64)
                    .reshape(1, -1)
                    .repeat(batch_size, axis=0)
                )

            logits, *present_key_values = language_model_session.run(
                None,
                dict(
                    inputs_embeds=inputs_embeds,
                    attention_mask=attention_mask,
                    position_ids=position_ids,
                    **past_key_values,
                ),
            )
            logits = logits[:, -1, :]
            next_token_logits = rep_penalty(generate_tokens, logits)
            input_ids = np.argmax(next_token_logits, axis=-1, keepdims=True).astype(
                np.int64
            )
            generate_tokens = np.concatenate((generate_tokens, input_ids), axis=-1)

            if (input_ids.flatten() == self.STOP_SPEECH_TOKEN).all():
                break

            attention_mask = np.concatenate(
                [
                    attention_mask,
                    np.ones((attention_mask.shape[0], 1), dtype=np.int64),
                ],
                axis=1,
            )
            position_ids = position_ids[:, -1:] + 1
            for j, key in enumerate(past_key_values):
                past_key_values[key] = present_key_values[j]

        # Strip leading START and trailing STOP, append silence pad,
        # prepend the speaker prompt token; matches the probe.
        speech_tokens = generate_tokens[:, 1:-1]
        silence_tokens = np.full(
            (speech_tokens.shape[0], 3), self.SILENCE_TOKEN, dtype=np.int64
        )
        speech_tokens = np.concatenate(
            [prompt_token, speech_tokens, silence_tokens], axis=1
        )

        wav = cond_decoder_session.run(
            None,
            dict(
                speech_tokens=speech_tokens,
                speaker_embeddings=speaker_embeddings,
                speaker_features=speaker_features,
            ),
        )[0].squeeze(axis=0)

        # Use a tempfile that callers (playback / chat) own and unlink.
        # Mirrors the Piper / Kokoro contract.
        fd, raw_path = tempfile.mkstemp(prefix="voiceagent-tts-", suffix=".wav")
        os.close(fd)
        output_path = Path(raw_path)
        try:
            soundfile.write(str(output_path), wav, self.SAMPLE_RATE)
        except Exception as exc:
            output_path.unlink(missing_ok=True)
            raise RuntimeError(
                str(exc) or "Chatterbox synthesis failed to write audio."
            ) from exc

        if not output_path.exists() or output_path.stat().st_size == 0:
            output_path.unlink(missing_ok=True)
            raise RuntimeError("Chatterbox did not create an audio file.")
        return output_path

    # ------------------------------------------------------------------ #
    # Backend protocol — download / removal                               #
    # ------------------------------------------------------------------ #

    def download_selected_item(
        self, progress_callback: Callable[[DownloadProgress], None] | None = None
    ) -> None:
        name = self._selected_item
        if not name:
            return
        self.download_item(name, progress_callback=progress_callback)

    def download_item(
        self,
        item_name: str,
        progress_callback: Callable[[DownloadProgress], None] | None = None,
    ) -> None:
        # Voices are user-supplied (mic record / file import); the
        # `download_item` Protocol entry exists for the
        # `_ItemBackend` interface. Defer to `download_engine_model`
        # so any latent caller still triggers the right fetch — but
        # `is_item_downloadable` returns False for all voices, so the
        # CatalogList Install button is hidden and this path is no
        # longer reachable from the UI under normal flow.
        del item_name
        self.download_engine_model(progress_callback=progress_callback)

    def download_engine_model(
        self,
        progress_callback: Callable[[DownloadProgress], None] | None = None,
    ) -> None:
        """Fetch the shared 4-component q4 ONNX model bundle from
        HuggingFace. Idempotent — short-circuits if the components are
        already cached. Wired up by the QML engine-state banner via
        `MainWindow.downloadChatterboxModel()`.
        """
        if self._model_present():
            return

        try:
            from huggingface_hub import hf_hub_download
        except ImportError as exc:
            raise RuntimeError(
                "Chatterbox extras are not installed. Install with "
                "`pip install voiceagent[chatterbox]`"
            ) from exc

        callback = progress_callback or (lambda progress: None)
        total_bytes = _APPROX_BUNDLE_BYTES.get(
            self._dtype, _APPROX_TOTAL_BYTES_Q4,
        )
        # Initial tick so progress UIs render before the first
        # component returns.
        callback(
            DownloadProgress(
                completed_bytes=0,
                total_bytes=total_bytes,
                download_speed_bytes_per_second=0,
            )
        )

        self.model_root.mkdir(parents=True, exist_ok=True)
        completed_bytes = 0
        for component in _COMPONENTS:
            filename = self._filename_for(component, self._dtype)
            try:
                graph_path = hf_hub_download(
                    self.HF_REPO, subfolder="onnx", filename=filename
                )
            except Exception as exc:
                raise RuntimeError(
                    f"Chatterbox download failed for {filename}: {exc}"
                ) from exc
            try:
                completed_bytes += Path(graph_path).stat().st_size
            except OSError:
                pass
            # The external-data sidecar is optional for some dtypes;
            # absence is not an error.
            try:
                data_path = hf_hub_download(
                    self.HF_REPO,
                    subfolder="onnx",
                    filename=f"{filename}_data",
                )
                try:
                    completed_bytes += Path(data_path).stat().st_size
                except OSError:
                    pass
            except Exception as exc:
                self._logger.debug(
                    "no _data sidecar for %s (%s)", filename, exc
                )
            # Keep total honest if our approximation underestimated:
            # the user shouldn't see "750 MB / 700 MB" mid-download.
            running_total = max(total_bytes, completed_bytes)
            callback(
                DownloadProgress(
                    completed_bytes=completed_bytes,
                    total_bytes=running_total,
                    download_speed_bytes_per_second=0,
                )
            )

        # Invalidate any previously cached sessions/tokenizer so the
        # next synth re-resolves against the freshly populated cache.
        with self._load_lock:
            self._sessions = None
            self._tokenizer = None

    def remove_item(self, item_name: str) -> None:
        # Only the reference clip is per-voice; the model bundle is
        # shared and stays put. (The HF cache itself is managed by
        # huggingface_hub and outside our remit.)
        if not item_name:
            return
        ref = self._reference_path(item_name)
        try:
            ref.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            self._logger.warning("failed to remove reference %s: %s", ref, exc)
        # Wipe any speaker-features cache files for this voice across
        # all dtypes (the cache filename is dtype-keyed, so a single
        # remove must sweep them all).
        for cache_path in self._speaker_features_cache_paths(item_name):
            try:
                cache_path.unlink()
            except FileNotFoundError:
                pass
            except OSError as exc:
                self._logger.warning(
                    "failed to remove cache %s: %s", cache_path, exc
                )

    def artifact_paths(self, item_name: str) -> list[Path]:
        paths: list[Path] = []
        if item_name:
            paths.append(self._reference_path(item_name))
        for component in _COMPONENTS:
            resolved = self._resolved_component_path(component)
            if resolved is not None:
                paths.append(resolved)
        return paths

    def artifact_manifest(
        self, item_name: str
    ) -> dict[Path, "ArtifactManifestEntry"]:
        # No machine-readable per-file checksums are published for
        # `ResembleAI/chatterbox-turbo-ONNX`. Layer-1 (existence) and
        # layer-4 (smoke-load on first synth) still apply via
        # `_model_present` and `_load_models` respectively.
        del item_name
        return {}

    # ------------------------------------------------------------------ #
    # Internals                                                            #
    # ------------------------------------------------------------------ #

    def _reference_path(self, item_name: str) -> Path:
        return self.references_root / f"{item_name}.wav"

    def _speaker_features_path(self, item_name: str) -> Path:
        """Cache filename for the speaker-features tuple at the active
        dtype. Per-(voice, dtype) keying so dtype switches naturally
        invalidate the old cache (the new one is computed on first
        synth at the new dtype) without trashing the previous variant
        — useful when the user flips back and forth.
        """
        return self.references_root / f"{item_name}.{self._dtype}.npz"

    def _speaker_features_cache_paths(self, item_name: str) -> list[Path]:
        """All speaker-features cache files for `item_name`, across
        every dtype. Used by `remove_item`, `invalidate_speaker_features_cache`
        and re-import to wipe stale embeddings when the underlying
        reference clip changes.
        """
        return [
            self.references_root / f"{item_name}.{dtype}.npz"
            for dtype in self.SUPPORTED_DTYPES
        ]

    def invalidate_speaker_features_cache(self, item_name: str) -> None:
        """Remove every cached speaker-features file for `item_name`,
        across all supported dtypes. Public surface for callers (the
        mic-record worker in window.py) that write the reference clip
        outside `import_reference_clip`'s codepath.
        """
        if not item_name:
            return
        for cache_path in self._speaker_features_cache_paths(item_name):
            try:
                cache_path.unlink()
            except FileNotFoundError:
                pass
            except OSError as exc:
                self._logger.warning(
                    "failed to invalidate cache %s: %s", cache_path, exc
                )

    def _load_or_compute_speaker_features(
        self,
        item_name: str,
        ref_path: Path,
        np,
        librosa,
        speech_encoder_session,
    ):
        """Return `(cond_emb, prompt_token, speaker_embeddings,
        speaker_features)` for `item_name`. Loads from the
        per-(voice, dtype) cache file if present and newer than the
        reference WAV; else runs the speech encoder, caches, and
        returns. Cache miss path matches the original inline encoder
        pass byte-for-byte; cache hit path skips the encoder entirely.
        """
        cache_path = self._speaker_features_path(item_name)
        if cache_path.exists():
            try:
                ref_mtime = ref_path.stat().st_mtime
                cache_mtime = cache_path.stat().st_mtime
                if cache_mtime >= ref_mtime:
                    with np.load(str(cache_path)) as data:
                        return (
                            data["cond_emb"],
                            data["prompt_token"],
                            data["speaker_embeddings"],
                            data["speaker_features"],
                        )
                self._logger.info(
                    "speaker-features cache stale for %s (ref newer); "
                    "recomputing",
                    item_name,
                )
            except Exception as exc:  # noqa: BLE001
                self._logger.warning(
                    "speaker-features cache load failed for %s (%s); "
                    "recomputing",
                    item_name, exc,
                )

        # Cache miss / stale / load failure: run the encoder.
        audio_values, _ = librosa.load(str(ref_path), sr=self.SAMPLE_RATE)
        audio_values = audio_values[np.newaxis, :].astype(np.float32)
        cond_emb, prompt_token, speaker_embeddings, speaker_features = (
            speech_encoder_session.run(None, {"audio_values": audio_values})
        )
        # Best-effort cache write — failure is non-fatal, just means
        # next synth pays the encoder cost again.
        try:
            np.savez_compressed(
                str(cache_path),
                cond_emb=cond_emb,
                prompt_token=prompt_token,
                speaker_embeddings=speaker_embeddings,
                speaker_features=speaker_features,
            )
        except Exception as exc:  # noqa: BLE001
            self._logger.warning(
                "speaker-features cache save failed for %s (%s); "
                "next synth will recompute",
                item_name, exc,
            )
        return cond_emb, prompt_token, speaker_embeddings, speaker_features

    # ------------------------------------------------------------------ #
    # Reference-clip management (used by window.py voice-management UI)   #
    # ------------------------------------------------------------------ #

    def import_reference_clip(self, source_path: Path, name: str) -> Path:
        """Copy a user-supplied audio file into references_root.

        `source_path` may be any format `librosa.load` accepts (WAV,
        FLAC, MP3, etc.). For consistency with the rest of the catalog
        (which globs `*.wav`) we always store the destination as
        `<name>.wav` and resample/convert via soundfile when the
        source is non-WAV or non-24kHz mono.
        """
        if not source_path.exists():
            raise FileNotFoundError(source_path)
        clean_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in name).strip("_")
        if not clean_name:
            raise ValueError("Reference voice name must contain alphanumerics.")
        target = self._reference_path(clean_name)
        self.references_root.mkdir(parents=True, exist_ok=True)
        if source_path.suffix.lower() == ".wav":
            shutil.copy2(source_path, target)
        else:
            # Convert non-WAV to mono 24 kHz WAV via librosa+soundfile.
            try:
                import librosa  # type: ignore[import-not-found]
                import soundfile  # type: ignore[import-not-found]
            except ImportError as exc:
                raise RuntimeError(
                    "Chatterbox extras are required to import non-WAV "
                    "reference clips. Install with `pip install voiceagent[chatterbox]`."
                ) from exc
            samples, _ = librosa.load(str(source_path), sr=self.SAMPLE_RATE, mono=True)
            soundfile.write(str(target), samples, self.SAMPLE_RATE)
        # Wipe any stale speaker-features caches for this name (a prior
        # import / recording at the same name would have produced them
        # against different audio).
        for cache_path in self._speaker_features_cache_paths(clean_name):
            try:
                cache_path.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                pass
        self._logger.info("imported reference clip %s -> %s", source_path, target)
        return target

    @staticmethod
    def _filename_for(component: str, dtype: str) -> str:
        if dtype == "fp32":
            suffix = ""
        elif dtype == "q8":
            suffix = "_quantized"
        else:
            suffix = f"_{dtype}"
        return f"{component}{suffix}.onnx"

    def _resolved_component_path(self, component: str) -> Path | None:
        """Resolve an HF-cached component path, or `None` if absent.

        We do not copy artifacts into `model_root`; HF's cache is the
        source of truth (cf. the module docstring). `try_to_load_from_cache`
        returns the resolved blob path if the file is fully present in
        the local cache, the sentinel string `_CACHED_NO_EXIST` if HF
        recorded a 404, or `None` if the file has never been fetched.
        """
        try:
            from huggingface_hub import try_to_load_from_cache
            from huggingface_hub.constants import HF_HUB_CACHE  # noqa: F401
        except ImportError:
            return None
        filename = f"onnx/{self._filename_for(component, self._dtype)}"
        cached = try_to_load_from_cache(self.HF_REPO, filename=filename)
        if isinstance(cached, str) and cached and Path(cached).exists():
            return Path(cached)
        return None

    def _model_present(self) -> bool:
        for component in _COMPONENTS:
            if self._resolved_component_path(component) is None:
                return False
        return True

    def _load_models(self) -> tuple[dict[str, Any], Any]:
        with self._load_lock:
            if self._sessions is not None and self._tokenizer is not None:
                return self._sessions, self._tokenizer

            try:
                import onnxruntime
                from transformers import AutoTokenizer
            except ImportError as exc:
                raise RuntimeError(
                    "Chatterbox extras are not installed. Install with "
                    "`pip install voiceagent[chatterbox]`"
                ) from exc

            sessions: dict[str, Any] = {}
            for component in _COMPONENTS:
                path = self._resolved_component_path(component)
                if path is None:
                    raise RuntimeError(
                        f"Chatterbox component is not in the HuggingFace "
                        f"cache: {component}"
                    )
                sessions[component] = onnxruntime.InferenceSession(str(path))
            tokenizer = AutoTokenizer.from_pretrained(self.HF_REPO)
            self._sessions = sessions
            self._tokenizer = tokenizer
            return sessions, tokenizer

    @staticmethod
    def _import_extras() -> tuple[Any, Any, Any, Any, Any]:
        try:
            import numpy as np
            import onnxruntime as ort
            import transformers as transformers_mod
            import librosa
            import soundfile
        except ImportError as exc:
            raise RuntimeError(
                "Chatterbox extras are not installed. Install with "
                "`pip install voiceagent[chatterbox]`"
            ) from exc
        return np, ort, transformers_mod, librosa, soundfile

    # Convenience for tests / callers that want to copy a freshly
    # recorded clip into the references root. Not part of the
    # backend protocol; kept here so the policy ("WAV, stem becomes
    # the voice name") stays in one place.
    def import_reference(self, source: Path, item_name: str) -> Path:
        source = Path(source)
        if not source.exists():
            raise FileNotFoundError(source)
        self.references_root.mkdir(parents=True, exist_ok=True)
        destination = self._reference_path(item_name)
        shutil.copyfile(source, destination)
        return destination


class _RepetitionPenaltyLogitsProcessor:
    """Apply HuggingFace-style repetition penalty to numpy logits.

    Hoisted out of `synthesize` so the numpy module reference can be
    bound once at construction; the inner loop calls this 1000+ times
    per utterance.
    """

    def __init__(self, np_module: Any, penalty: float) -> None:
        self._np = np_module
        self.penalty = penalty

    def __call__(self, input_ids: Any, scores: Any) -> Any:
        np = self._np
        score = np.take_along_axis(scores, input_ids, axis=1)
        score = np.where(score < 0, score * self.penalty, score / self.penalty)
        scores_processed = scores.copy()
        np.put_along_axis(scores_processed, input_ids, score, axis=1)
        return scores_processed
