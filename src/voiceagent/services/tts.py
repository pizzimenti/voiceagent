from __future__ import annotations

import logging
import os
from pathlib import Path
import shutil
import tempfile
import threading
from typing import TYPE_CHECKING
import urllib.request
import wave

from huggingface_hub import HfApi, hf_hub_url
from piper import PiperVoice

from voiceagent.backends import TextToSpeechBackend
from voiceagent.downloaders import AriaDownloader, DownloadFile, DownloadProgress
from voiceagent.paths import default_tts_model_root

if TYPE_CHECKING:
    from voiceagent.parallel_item_loader import ArtifactManifestEntry


class PiperTtsService(TextToSpeechBackend):
    backend_name = "Piper"
    selection_label = "Voice"
    VOICE_REPOSITORY = "rhasspy/piper-voices"
    # `main`-anchored URL is used by the eager catalog-refresh path
    # (`refresh_remote_catalog` / `_fetch_and_cache_voice_names`). The
    # catalog wants the latest list of voices the user could browse to,
    # so resolving against `main` is correct there.
    #
    # The download-verification path (`artifact_manifest`) does NOT use
    # this constant — it pins to the upstream commit SHA captured at
    # download start (see `_voices_json_url_for_sha`,
    # `_download_sha_by_name`) so layer 2 (size) and layer 3 (md5)
    # compare the on-disk bytes against the manifest entry that
    # describes those exact bytes. Closes the TOCTOU window where
    # upstream republished a voice between aria2 fetching it and the
    # verifier reading the manifest.
    VOICES_JSON_URL = "https://huggingface.co/rhasspy/piper-voices/resolve/main/voices.json?download=true"

    def __init__(self, command: list[str], model_path: str | None, extra_args: list[str] | None = None) -> None:
        self.command = command
        self.model_path = model_path
        self.extra_args = extra_args or []
        self.model_root = default_tts_model_root()
        self.downloader = AriaDownloader(connections=10)
        self._logger = logging.getLogger(__name__)
        self._loaded_voice_path: Path | None = None
        self._voice: PiperVoice | None = None
        # Per-instance cache for `known_voice_names()`. Each `is_item_managed`
        # call (and via the catalog adapter, every per-row state read) used
        # to re-glob `model_root` and re-parse `voices.json`. The cache is
        # invalidated on every event that mutates the underlying disk state:
        # remote-catalog refresh, voice download, voice delete.
        #
        # The lock serializes reads (GUI-thread `is_item_managed` calls)
        # against invalidations from the worker thread that runs
        # `refresh_catalog`. Without it, a GUI read could re-populate the
        # cache from disk concurrently with an invalidation that's about
        # to set it back to `None`, leaving stale data until the next
        # event.
        self._known_voice_names_cache: set[str] | None = None
        self._known_voice_names_lock = threading.Lock()
        # Per-voice SHA pins of `rhasspy/piper-voices` captured at the
        # start of `_download_voice` and consumed by `artifact_manifest`
        # during the post-download verification pass. Both pin to the
        # same upstream commit so layers 2/3 compare the downloaded
        # bytes against the manifest entry that describes those exact
        # bytes. Missing key for a voice → `artifact_manifest` skips
        # layers 2/3 (returns empty) rather than falling back to `main`,
        # which would re-open the TOCTOU window this is closing.
        #
        # Per-name dict (rather than a single shared scalar) because
        # `TtsVoiceLoader` runs downloads with `max_workers=3` (see
        # `voiceagent.tts_loader.TtsVoiceLoader.__init__`). Two concurrent
        # `_download_voice` calls would have stomped each other's pin on
        # a shared scalar — voice A's verifier could read voice B's SHA
        # and silently pass/fail the wrong artifact. Keyed by voice name
        # so each in-flight install carries its own pin. Entries are
        # popped on success/failure so the dict never grows unbounded.
        #
        # `_download_sha_lock` serializes the dict's read/write/delete
        # operations across worker threads. We deliberately do NOT extend
        # `_known_voice_names_lock` for this — that lock guards the
        # voice-names cache, an unrelated invariant, and conflating the
        # two would risk lock-ordering hazards when one critical section
        # ends up needing both. Both locks are short-held leaf locks (no
        # nested acquisitions, no callbacks under lock).
        self._download_sha_by_name: dict[str, str] = {}
        self._download_sha_lock = threading.Lock()
        # Backward-compatible fallback pin for `artifact_manifest`. The
        # production verifier path always lands here via the per-name
        # entry written by `_download_voice` (the per-name lookup wins
        # whenever it has a hit), so this field is only consulted by
        # callers that pre-date the per-name dict — primarily
        # `tests/test_artifact_manifest.py`, which sets the legacy
        # `_current_download_sha` attribute directly via the property
        # below. Concurrent installs cannot race through this field
        # because `_download_voice` never writes here.
        self._default_download_sha: str | None = None

    @property
    def _current_download_sha(self) -> str | None:
        """Backward-compatible view of the pinned SHA.

        Pre-dates the per-voice `_download_sha_by_name` dict added to
        guard against the cross-install bleed that
        `tts_loader.TtsVoiceLoader`'s `max_workers=3` executor would
        otherwise exhibit. Kept as a property so existing tests that
        set/clear the pin via this attribute still work without having
        to know about the per-name dict.

        Production code must NOT use this attribute — it doesn't
        carry the per-voice keying that the concurrency fix requires.
        Use `_download_sha_by_name` directly.
        """
        return self._default_download_sha

    @_current_download_sha.setter
    def _current_download_sha(self, value: str | None) -> None:
        self._default_download_sha = value

    @property
    def enabled(self) -> bool:
        return bool(self.command and self.model_path)

    @property
    def is_available(self) -> bool:
        if not self.model_path:
            return False
        # Stay in lockstep with `is_item_available(selected)`: a voice is
        # only "available" when BOTH the `.onnx` and its paired
        # `.onnx.json` config exist. Previously this returned True as
        # soon as any `.onnx` candidate resolved, which let a partial
        # download (onnx only, no json) masquerade as ready and drove
        # the loader to emit `load_completed` for a voice that would
        # crash on first synthesis.
        return self.is_item_available(self.model_path)

    @property
    def can_download(self) -> bool:
        return bool(self.model_path and self._looks_like_voice_name(self.model_path))

    @classmethod
    def known_voice_names(cls, model_root: Path) -> set[str]:
        """Union of on-disk installed voices and remote-cache catalog.

        Single source of truth for "is this name a Piper voice we know
        about" — used by `available_voice_names` (sorted listing) and by
        `is_item_managed` (membership check). `_looks_like_voice_name`
        is a syntactic guess on user-typed strings and is intentionally
        NOT folded in here.
        """
        return cls._cached_voice_names(model_root) | cls._voice_names_from_cache_file(model_root)

    @classmethod
    def available_voice_names(cls, model_root: Path, configured_model: str | None = None) -> list[str]:
        """Return the eager on-disk catalog (installed + cached + configured).

        This path must never touch the network — see AGENTS.md's "keep
        network/model refreshes off the first paint path" rule. The
        asynchronous refresh that adds remote-only entries is driven by
        `refresh_remote_catalog`, which is expected to run after the QML
        window has painted.
        """
        voices: set[str] = set(cls.known_voice_names(model_root))
        if configured_model:
            voices.add(configured_model)
        return sorted(voices)

    @classmethod
    def refresh_remote_catalog(
        cls, model_root: Path, configured_model: str | None = None
    ) -> list[str]:
        """Fetch `voices.json`, refresh the on-disk cache, and return the union.

        Safe to run from a worker thread: only performs a `urlopen` and a
        file write to the cache path. Returns the same eager union as
        `available_voice_names` when the network fetch fails, so callers
        can treat any failure as a no-op.
        """
        cls._fetch_and_cache_voice_names(model_root)
        return cls.available_voice_names(model_root, configured_model)

    def available_items(self) -> list[str]:
        return self.available_voice_names(self.model_root, self.model_path)

    def refresh_catalog(self) -> list[str]:
        """Worker-thread entry point for the deferred catalog refresh."""
        names = self.refresh_remote_catalog(self.model_root, self.model_path)
        # `_fetch_and_cache_voice_names` may have rewritten `voices.json`;
        # invalidate so the next `is_item_managed` read pulls the new
        # union from disk.
        self.invalidate_known_voice_names_cache()
        return names

    @classmethod
    def is_voice_available(cls, model_root: Path, model_path: str | None) -> bool:
        if not model_path:
            return False

        # Synthesis (`_get_voice`) loads `<resolved>.json` alongside the
        # `.onnx`. Reporting "available" without that sidecar lets a
        # bare `custom.onnx` masquerade as ready and crash on first
        # synthesis. Require both files for every resolved branch.
        #
        # Also reject any voice with a stale `<onnx>.aria2` sidecar:
        # aria2 leaves that control file behind only when a download
        # was interrupted. The `.onnx` next to it is partial and will
        # crash Piper inside `synthesize_wav` (manifests as the
        # baffling `wave.Error: # channels not specified` because the
        # WAV is closed without `setnchannels` ever being called).
        # Layer-4 smoke-load catches new corruption at download time;
        # this guard catches LEGACY corruption from pre-v0.3.2
        # installs that never went through layer-4.
        def _is_complete(onnx: Path) -> bool:
            return not Path(f"{onnx}.aria2").exists()

        candidate = Path(model_path).expanduser()
        if candidate.exists():
            return _is_complete(candidate) and Path(f"{candidate}.json").exists()

        local_candidate = model_root / model_path
        if local_candidate.exists():
            return _is_complete(local_candidate) and Path(f"{local_candidate}.json").exists()

        onnx_candidate = model_root / f"{model_path}.onnx"
        json_candidate = model_root / f"{model_path}.onnx.json"
        if not (onnx_candidate.exists() and json_candidate.exists()):
            return False
        return _is_complete(onnx_candidate)

    def is_item_available(self, item_name: str) -> bool:
        return self.is_voice_available(self.model_root, item_name)

    def invalidate_known_voice_names_cache(self) -> None:
        with self._known_voice_names_lock:
            self._known_voice_names_cache = None

    def _get_known_voice_names(self) -> set[str]:
        with self._known_voice_names_lock:
            if self._known_voice_names_cache is None:
                self._known_voice_names_cache = type(self).known_voice_names(self.model_root)
            return self._known_voice_names_cache

    def is_item_managed(self, item_name: str) -> bool:
        return item_name in self._get_known_voice_names()

    def is_item_downloadable(self, item_name: str) -> bool:
        # A name is downloadable when we know about it (cached in
        # `voices.json` or already on disk) OR it is shaped like a
        # Piper voice name and so resolvable via `_voice_remote_prefix`
        # at install time. The second branch matters for first-run
        # before the deferred `voices.json` fetch lands: a configured
        # `TTS_MODEL=en_US-lessac-medium` should still let the user
        # click Install, even though `is_item_managed` is False until
        # the cache populates.
        if self.is_item_managed(item_name):
            return True
        return self._looks_like_voice_name(item_name)

    @property
    def selected_item(self) -> str | None:
        return self.model_path

    def set_model_path(self, model_path: str | None) -> None:
        self.model_path = model_path
        self._loaded_voice_path = None
        self._voice = None

    def set_selected_item(self, item_name: str | None) -> None:
        self.set_model_path(item_name)

    def synthesize(self, text: str, progress_callback=None) -> Path | None:
        if not self.enabled:
            return None

        fd, raw_path = tempfile.mkstemp(prefix="voiceagent-tts-", suffix=".wav")
        os.close(fd)
        Path(raw_path).unlink(missing_ok=True)
        output_path = Path(raw_path)

        resolved_model_path = self._resolve_existing_model_path()
        if resolved_model_path is None:
            raise RuntimeError(self._missing_model_message())

        try:
            with wave.open(str(output_path), "wb") as wav_file:
                self._get_voice(resolved_model_path).synthesize_wav(text, wav_file)
        except Exception as exc:
            output_path.unlink(missing_ok=True)
            raise RuntimeError(str(exc) or "TTS synthesis failed.") from exc

        if not output_path.exists() or output_path.stat().st_size == 0:
            output_path.unlink(missing_ok=True)
            raise RuntimeError("TTS did not create an audio file.")

        return output_path

    def download_voice(self, progress_callback=None) -> None:
        if not self.enabled:
            raise RuntimeError("TTS is not configured. Set TTS_MODEL to a Piper voice or model path.")

        if self.is_available:
            return

        if not self.can_download:
            raise RuntimeError(self._missing_model_message())

        assert self.model_path is not None
        self._download_voice(self.model_path, progress_callback=progress_callback)

    def download_selected_item(self, progress_callback=None) -> None:
        self.download_voice(progress_callback=progress_callback)

    def download_item(self, item_name: str, progress_callback=None) -> None:
        self._download_voice(item_name, progress_callback=progress_callback)

    def remove_item(self, item_name: str) -> None:
        if not item_name:
            return

        candidate = self.model_root / f"{item_name}.onnx"
        config_candidate = self.model_root / f"{item_name}.onnx.json"
        nested_candidate = self.model_root / item_name

        if candidate.exists():
            candidate.unlink()
        if config_candidate.exists():
            config_candidate.unlink()
        if nested_candidate.exists() and nested_candidate.is_dir():
            shutil.rmtree(nested_candidate)

        if self.model_path == item_name:
            self._loaded_voice_path = None
            self._voice = None

        # On-disk inventory shifted; invalidate so the next
        # `is_item_managed` read reflects the deletion. The voice may
        # remain "known" via `voices.json` (a remote-catalog entry doesn't
        # disappear when the user deletes the local file) — that's
        # correct: the row stays downloadable.
        self.invalidate_known_voice_names_cache()

    def artifact_paths(self, item_name: str) -> list[Path]:
        """Return the two files a Piper voice install is made of.

        Used by `ParallelItemLoader._verify_download` (to look for
        aria2 sidecars) and `_cleanup_failed_download` (to wipe
        partials). The order is `[onnx, onnx.json]`; the base
        verifier treats any `<artifact>.aria2` as a failed transfer.
        """
        onnx_path = self.model_root / f"{item_name}.onnx"
        json_path = self.model_root / f"{item_name}.onnx.json"
        return [onnx_path, json_path]

    def artifact_manifest(
        self, item_name: str
    ) -> dict[Path, ArtifactManifestEntry]:
        """Per-file size + md5 from upstream `voices.json` at the pinned SHA.

        Read by `ParallelItemLoader._verify_download` for layers 2
        (size) and 3 (checksum). Fetches `voices.json` from
        `rhasspy/piper-voices` at the commit SHA captured for THIS
        voice by `_download_voice` at download start
        (`_download_sha_by_name[item_name]`). Both the file bytes on
        disk and the manifest entry that describes them resolve
        against the same commit, so an upstream republish during the
        download window cannot make layers 2/3 fail-close on a
        healthy install.

        Lookup is keyed on `item_name` (not a shared scalar) so two
        concurrent installs running on `TtsVoiceLoader`'s
        `max_workers=3` executor cannot bleed each other's pin —
        voice A's verifier always sees voice A's SHA.

        **No SHA available** — no entry for `item_name` in
        `_download_sha_by_name`. Outside an active download, or for
        an unrelated voice. We log and return an empty manifest
        (verifier degrades to layer 1 + 4) rather than falling back
        to `main`, which would re-open the TOCTOU window this method
        exists to close. In practice the verifier is only invoked
        from `_download_worker` immediately after `_download_voice`
        sets the SHA, so this branch only fires from defensive
        callers / tests.

        **Network failure during the SHA-pinned fetch** — same
        contract: empty manifest, warning log, layers 1 + 4 are
        still authoritative.

        voices.json is the upstream `rhasspy/piper-voices` manifest;
        each voice entry's `files` map keys are repo-relative paths
        (`ar/ar_JO/kareem/low/ar_JO-kareem-low.onnx`) and the values
        carry `size_bytes` and `md5_digest` per file. We map
        manifest entries onto local artifact paths by basename.
        """
        from voiceagent.parallel_item_loader import ArtifactManifestEntry

        # Consume-on-read: pop the per-voice pin atomically with the
        # lookup so the verifier sees it exactly once and the dict
        # cannot grow unbounded across many installs. Any second
        # `artifact_manifest` call for the same voice would degrade to
        # the empty-manifest branch — acceptable, since the verifier
        # only fires once per install (see
        # `ParallelItemLoader._download_worker`).
        #
        # Fallback to `_default_download_sha` only when no per-voice
        # entry exists — preserves the legacy `_current_download_sha`
        # attribute used by older tests. Production never lands here:
        # `_download_voice` always writes the per-voice dict before the
        # verifier runs.
        with self._download_sha_lock:
            sha = self._download_sha_by_name.pop(item_name, None)
        if sha is None:
            sha = self._default_download_sha
        if not sha:
            self._logger.warning(
                "Piper artifact_manifest called without a pinned SHA "
                "for item=%s; skipping layers 2/3 (layers 1 + 4 still "
                "apply). This should only happen outside of an active "
                "download — the production verifier path always sets "
                "the SHA.",
                item_name,
            )
            return {}

        try:
            payload = type(self)._fetch_voices_json_at_sha(sha)
        except Exception:
            self._logger.warning(
                "Piper voices.json SHA-pinned fetch raised during "
                "verification (sha=%s); skipping layers 2/3 (layers "
                "1 + 4 still apply)",
                sha,
                exc_info=True,
            )
            return {}

        if not payload:
            self._logger.warning(
                "Piper voices.json SHA-pinned fetch returned empty "
                "during verification (sha=%s, network failure or "
                "malformed JSON); skipping layers 2/3 (layers 1 + 4 "
                "still apply)",
                sha,
            )
            return {}

        entry = payload.get(item_name)
        if not isinstance(entry, dict):
            return {}
        files = entry.get("files")
        if not isinstance(files, dict):
            return {}

        manifest: dict[Path, ArtifactManifestEntry] = {}
        for repo_path, meta in files.items():
            if not isinstance(meta, dict):
                continue
            basename = repo_path.rsplit("/", 1)[-1]
            local_path = self.model_root / basename
            size = meta.get("size_bytes")
            md5 = meta.get("md5_digest")
            manifest[local_path] = ArtifactManifestEntry(
                expected_size=size if isinstance(size, int) else None,
                expected_checksum_hex=md5 if isinstance(md5, str) else None,
                checksum_algorithm="md5" if isinstance(md5, str) else None,
            )
        return manifest

    def _resolve_existing_model_path(self) -> Path | None:
        assert self.model_path is not None

        # Skip any candidate with a stale `<onnx>.aria2` sidecar — same
        # rationale as `is_voice_available`. Resolving to a partial
        # `.onnx` lets `synthesize()` proceed past its missing-model
        # guard and crash inside `synthesize_wav` with the unhelpful
        # `wave.Error: # channels not specified`.
        def _is_complete(path: Path) -> bool:
            return not Path(f"{path}.aria2").exists()

        candidate = Path(self.model_path).expanduser()
        if candidate.exists() and _is_complete(candidate):
            return candidate

        local_candidate = self.model_root / self.model_path
        if local_candidate.exists() and _is_complete(local_candidate):
            return local_candidate

        onnx_candidate = self.model_root / f"{self.model_path}.onnx"
        if onnx_candidate.exists() and _is_complete(onnx_candidate):
            return onnx_candidate

        return None

    def describe_selection_state(self) -> dict[str, str | bool]:
        model_path = self.model_path
        if not model_path:
            return {
                "selected_model": "",
                "available": False,
                "can_download": False,
                "resolved_model_path": "",
                "direct_candidate": "",
                "local_candidate": "",
                "onnx_candidate": "",
                "json_candidate": "",
            }

        candidate = Path(model_path).expanduser()
        local_candidate = self.model_root / model_path
        onnx_candidate = self.model_root / f"{model_path}.onnx"
        json_candidate = self.model_root / f"{model_path}.onnx.json"
        resolved_model_path = self._resolve_existing_model_path()
        return {
            "selected_model": model_path,
            "available": resolved_model_path is not None,
            "can_download": self.can_download,
            "resolved_model_path": str(resolved_model_path) if resolved_model_path else "",
            "direct_candidate": str(candidate),
            "local_candidate": str(local_candidate),
            "onnx_candidate": str(onnx_candidate),
            "json_candidate": str(json_candidate),
        }

    def _get_voice(self, resolved_model_path: Path) -> PiperVoice:
        if self._voice is not None and self._loaded_voice_path == resolved_model_path:
            return self._voice

        config_path = Path(f"{resolved_model_path}.json")
        self._logger.info("Loading Piper voice model=%s config=%s", resolved_model_path, config_path)
        self._voice = PiperVoice.load(
            resolved_model_path,
            config_path=config_path,
            use_cuda=False,
            download_dir=self.model_root,
        )
        self._loaded_voice_path = resolved_model_path
        return self._voice

    def _download_voice(self, voice_name: str, progress_callback=None) -> None:
        onnx_path = self.model_root / f"{voice_name}.onnx"
        json_path = self.model_root / f"{voice_name}.onnx.json"
        if onnx_path.exists() and json_path.exists():
            return

        # Pin upstream commit SHA. Both the file fetch below and the
        # `artifact_manifest` refresh that follows in
        # `_verify_download` resolve against this exact revision, so a
        # mid-download republish upstream cannot make layer 2/3 reject
        # a healthy download. Fail-closed on capture failure: pinning
        # is the whole point, falling back to `main` would re-open the
        # TOCTOU window. The existing download-error UI surfaces the
        # raise to the user the same as any network failure.
        #
        # Pin is keyed on `voice_name` so concurrent installs (the loader
        # runs with `max_workers=3`) cannot stomp each other's SHA — voice
        # A's verifier always reads voice A's pin via
        # `artifact_manifest`. NOT cleared here on success: the verifier
        # runs AFTER `_download_voice` returns (see
        # `ParallelItemLoader._download_worker`), so it still needs to
        # read the pin. The verifier-side cleanup happens in
        # `_consume_download_sha`, which `artifact_manifest` calls once
        # the manifest fetch is done. On exception we DO pop here:
        # `_download_worker` skips the verifier on a download failure, so
        # if we don't pop now the entry would leak.
        sha = self._capture_repo_sha()
        with self._download_sha_lock:
            self._download_sha_by_name[voice_name] = sha
        try:
            remote_prefix = self._voice_remote_prefix(voice_name)
            onnx_url = hf_hub_url(
                self.VOICE_REPOSITORY,
                filename=f"{remote_prefix}.onnx",
                revision=sha,
            )
            json_url = hf_hub_url(
                self.VOICE_REPOSITORY,
                filename=f"{remote_prefix}.onnx.json",
                revision=sha,
            )
            self._logger.info(
                "Downloading Piper voice voice=%s model_root=%s sha=%s",
                voice_name,
                self.model_root,
                sha,
            )
            files = [
                DownloadFile(
                    url=onnx_url,
                    destination=onnx_path,
                    size_bytes=self.downloader.get_remote_size(onnx_url),
                ),
                DownloadFile(
                    url=json_url,
                    destination=json_path,
                    size_bytes=self.downloader.get_remote_size(json_url),
                ),
            ]
            callback = progress_callback or (lambda progress: None)
            callback(DownloadProgress(completed_bytes=0, total_bytes=sum(file.size_bytes for file in files), download_speed_bytes_per_second=0))
            self.downloader.download(files, progress_callback=callback)
            self._logger.info(
                "Piper voice download completed voice=%s model_root=%s sha=%s",
                voice_name,
                self.model_root,
                sha,
            )
            # New on-disk voice — invalidate so subsequent `is_item_managed`
            # reads pick up the new entry without waiting for a catalog
            # refresh.
            self.invalidate_known_voice_names_cache()
        except Exception:
            # On any download failure, drop this voice's pin so a stale
            # value cannot leak into a follow-up `artifact_manifest`
            # call from an unrelated codepath, and so the dict cannot
            # grow unbounded across repeated failures. The verifier
            # won't run on a failed download (the worker emits
            # `load_failed` and returns), so the pin is no longer
            # needed. Other in-flight voices are NOT affected — only
            # this voice's entry is popped.
            with self._download_sha_lock:
                self._download_sha_by_name.pop(voice_name, None)
            raise

    def _capture_repo_sha(self) -> str:
        """Resolve the current commit SHA of `rhasspy/piper-voices`.

        Used at download-start to pin every URL constructed during
        the install (file fetches AND the verifier's manifest
        refetch) to the same revision. Fails-closed: any error
        (network blip, HF 5xx, malformed response, missing `sha`
        attribute) raises so the download never proceeds against an
        unpinned `main` — the whole point of pinning is consistency.
        """
        api = HfApi()
        info = api.repo_info(self.VOICE_REPOSITORY)
        sha = getattr(info, "sha", None)
        if not isinstance(sha, str) or not sha:
            raise RuntimeError(
                f"Could not capture upstream commit SHA for "
                f"{self.VOICE_REPOSITORY}; refusing to download "
                f"against an unpinned revision"
            )
        return sha

    def _missing_model_message(self) -> str:
        assert self.model_path is not None
        if self.can_download:
            return f"Piper voice '{self.model_path}' is not downloaded. Click Load Voice first."
        return f"TTS model path not found: {self.model_path}"

    @classmethod
    def _cached_voice_names(cls, model_root: Path) -> set[str]:
        voices: set[str] = set()
        for onnx_path in model_root.glob("*.onnx"):
            if (model_root / f"{onnx_path.name}.json").exists():
                voices.add(onnx_path.stem)
        return voices

    @classmethod
    def _voice_names_from_cache_file(cls, model_root: Path) -> set[str]:
        cache_path = model_root / "voices.json"
        if not cache_path.exists():
            return set()

        try:
            import json

            return set(json.loads(cache_path.read_text(encoding="utf-8")).keys())
        except Exception:
            return set()

    @classmethod
    def _voices_json_url_for_sha(cls, sha: str) -> str:
        """Construct the SHA-pinned `voices.json` URL.

        Used by the download-verification path so the manifest read
        for layers 2/3 resolves against the same commit as the file
        bytes captured by `_download_voice`. The catalog-refresh
        path uses `VOICES_JSON_URL` (resolves against `main`)
        because it wants the latest set of browsable voices.
        """
        return (
            f"https://huggingface.co/{cls.VOICE_REPOSITORY}/resolve/"
            f"{sha}/voices.json?download=true"
        )

    @classmethod
    def _fetch_voices_json_at_sha(cls, sha: str) -> dict | None:
        """Fetch `voices.json` from upstream pinned to `sha`.

        Returns the parsed top-level dict on success, `None` on any
        failure (network, JSON parse, non-dict payload). Does NOT
        write to the on-disk `voices.json` cache — that cache is
        anchored to `main` for the eager catalog path, and writing
        a SHA-pinned snapshot over it could rewind the user's view
        of the catalog if upstream had advanced past `sha`.
        """
        url = cls._voices_json_url_for_sha(sha)
        try:
            with urllib.request.urlopen(url, timeout=5) as response:
                payload = response.read().decode("utf-8")
        except Exception:
            return None

        try:
            import json

            parsed = json.loads(payload)
        except Exception:
            return None

        if not isinstance(parsed, dict):
            return None
        return parsed

    @classmethod
    def _fetch_and_cache_voice_names(cls, model_root: Path) -> set[str]:
        try:
            with urllib.request.urlopen(cls.VOICES_JSON_URL, timeout=5) as response:
                payload = response.read().decode("utf-8")
        except Exception:
            return set()

        try:
            import json

            voices = set(json.loads(payload).keys())
            cache_path = model_root / "voices.json"
            # Atomic replace: write to a per-call unique tempfile in the
            # same directory then `os.replace` onto `voices.json`. A
            # process kill mid-write leaves the previous (valid) cache
            # in place. The unique name (via `NamedTemporaryFile`) avoids
            # collisions if two refreshes ever overlap, and the
            # try/finally cleans up partials so a failed write doesn't
            # leak `*.json.tmp` files in `model_root`.
            fd, tmp_name = tempfile.mkstemp(
                dir=model_root, prefix="voices.", suffix=".json.tmp"
            )
            tmp_path = Path(tmp_name)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(payload)
                os.replace(tmp_path, cache_path)
            except Exception:
                tmp_path.unlink(missing_ok=True)
                raise
            return voices
        except Exception:
            return set()

    def _looks_like_voice_name(self, value: str) -> bool:
        return "://" not in value and "/" not in value and value.count("-") >= 2

    def _voice_remote_prefix(self, voice_name: str) -> str:
        parts = voice_name.split("-")
        locale = parts[0]
        quality = parts[-1]
        speaker = "-".join(parts[1:-1])
        if not locale or not speaker or not quality or "_" not in locale:
            raise RuntimeError(f"Unsupported Piper voice name format: {voice_name}")

        language = locale.split("_", 1)[0]
        return f"{language}/{locale}/{speaker}/{quality}/{voice_name}"
