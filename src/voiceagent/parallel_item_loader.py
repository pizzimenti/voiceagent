"""Shared base class for the Whisper / Piper parallel-install loaders.

This module factors out the common state machine that drives parallel
downloads, deletes, per-row progress, and aggregate progress for the
voice and STT model loaders. The two subclasses
(`WhisperModelLoader`, `TtsVoiceLoader`) only need to provide
backend-specific status strings, optional download preconditions, and
optional state-logging.

Three properties of the shared machinery worth highlighting:

1. **Single owner thread for `_progress_by_item`.** Worker callbacks
   run on `ThreadPoolExecutor` threads. They must NOT mutate
   `_progress_by_item` directly. Instead they emit the internal
   `_progress_tick` signal, which is connected to
   `_on_progress_tick` via `Qt.QueuedConnection`. The slot then runs
   on the owner thread (the thread that constructed this object,
   typically the GUI thread) and is the only place that writes the
   dict and the only place that calls `_aggregate_progress`. This
   eliminates the data race that arose with `max_workers=3`.

2. **Idempotent finalization.** `_handle_done` runs in the executor
   thread when the future settles and may emit `load_failed` for an
   item that already emitted `load_failed` from inside its worker
   (e.g. an exception path that already signalled, then bubbled up).
   Each `_finish_*` slot guards `name in self._active_items` and
   no-ops on the duplicate, preventing double-emission of
   `loading_changed`, `ready_changed`, and friends.

3. **Status strings live in subclass hooks.** The base class never
   hardcodes Whisper- or Piper-flavored language; subclasses override
   `_status_*` methods to localize each transition.
"""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor, wait as futures_wait
from dataclasses import dataclass
import hashlib
import logging
from pathlib import Path
import threading
from typing import Callable, ClassVar, Optional, Protocol, runtime_checkable

from PySide6.QtCore import QObject, Qt, Signal

from voiceagent.downloaders import DownloadProgress


_EMPTY_PROGRESS = DownloadProgress(
    completed_bytes=0, total_bytes=0, download_speed_bytes_per_second=0
)

# Module-level logger so verifier helpers (which are static methods on
# the loader class) can emit operator-visible warnings without needing
# an instance handle. The instance-level `self._logger` is still used
# from non-static paths so log records carry the subclass name.
_module_logger = logging.getLogger(__name__)

# Chunk size for streaming-checksum reads. 1 MiB is large enough that
# the per-call hashlib.update overhead is negligible relative to the
# disk read, and small enough that holding it in memory is trivial
# even for the largest Whisper artifacts (`large-v3` model.bin is
# ~3 GiB — read in ~3000 chunks).
_CHECKSUM_READ_CHUNK = 1 << 20

# Algorithms the verifier accepts. Piper's voices.json carries md5
# hex digests; HuggingFace LFS pointers carry sha256. The integrity
# threat model here is transfer/disk corruption (not adversarial
# replacement), so md5 is fit for purpose alongside sha256.
_SUPPORTED_CHECKSUM_ALGORITHMS = frozenset({"md5", "sha256"})


@dataclass(frozen=True)
class ArtifactManifestEntry:
    """Authoritative size + checksum for one artifact path.

    Either field may be `None` when the manifest source doesn't
    publish that piece of metadata for this file (e.g. HuggingFace
    non-LFS blobs only carry size, not sha256). The verifier skips
    layers it doesn't have data for instead of failing closed —
    this keeps installs working when only a partial manifest is
    available.
    """

    expected_size: int | None = None
    expected_checksum_hex: str | None = None
    checksum_algorithm: str | None = None  # "md5" | "sha256" | None


@runtime_checkable
class _ItemBackend(Protocol):
    """Minimal surface required of a backend by `ParallelItemLoader`.

    Both `SpeechToTextBackend` and `TextToSpeechBackend` satisfy this.
    """

    backend_name: str
    selection_label: str

    @property
    def is_available(self) -> bool: ...

    def available_items(self) -> list[str]: ...

    def is_item_available(self, name: str) -> bool: ...

    def download_item(
        self, name: str, progress_callback: Callable[[DownloadProgress], None] | None = None
    ) -> None: ...

    def remove_item(self, name: str) -> None: ...

    def artifact_paths(self, name: str) -> list[Path]: ...


class ParallelItemLoader(QObject):
    """Base class for parallel model/voice loaders.

    Subclasses customize the user-facing status strings via the
    `_status_*` hooks and may override `_can_download` and
    `_log_state` to add preconditions or diagnostics.

    Every name in `_REQUIRED_STATUS_HOOKS` must be overridden by every
    concrete subclass. `__init_subclass__` enforces this at class-build
    time so a missing override fails at import (loud, deterministic)
    rather than the first time a state machine reaches that hook
    (lazy, situational, hard to repro). `@abstractmethod` would be the
    canonical mechanism but `QObject`'s metaclass conflicts with
    `ABCMeta`, so a manual MRO walk is the simplest equivalent.
    """

    _REQUIRED_STATUS_HOOKS: ClassVar[tuple[str, ...]] = (
        "_status_checking",
        "_status_downloading",
        "_status_removing",
        "_status_ready",
        "_status_load_failed",
        "_status_remove_failed",
        "_status_idle_prompt",
        "_status_removed_ok",
        "_status_select_to_enable",
    )

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        missing: list[str] = []
        for hook in cls._REQUIRED_STATUS_HOOKS:
            # Walk the MRO and find the first class that defines this
            # hook. If that's `ParallelItemLoader` itself, neither `cls`
            # nor any intermediate ancestor has overridden the
            # `NotImplementedError`-raising sentinel.
            defined_on = next(
                (base for base in cls.__mro__ if hook in base.__dict__),
                None,
            )
            if defined_on is None or defined_on is ParallelItemLoader:
                missing.append(hook)
        if missing:
            raise TypeError(
                f"{cls.__name__} must override all status hooks; "
                f"missing: {', '.join(missing)}"
            )

    selection_changed = Signal(str)
    ready_changed = Signal(bool)
    loading_changed = Signal(bool)
    status_changed = Signal(str)
    # Aggregate progress across all active downloads.
    progress_changed = Signal(object)
    # Per-item signals for parallel-install UI.
    item_loading_changed = Signal(str, bool)
    item_progress_changed = Signal(str, object)
    error_changed = Signal(str)
    load_completed = Signal(str)
    load_failed = Signal(str, str)
    delete_completed = Signal(str)
    delete_failed = Signal(str, str)

    # Internal: emitted from worker threads, consumed on the owner
    # thread via Qt.QueuedConnection. The slot is the SOLE writer to
    # `_progress_by_item`.
    _progress_tick = Signal(str, object)

    def __init__(
        self,
        backend: _ItemBackend,
        *,
        max_workers: int = 3,
        thread_name_prefix: str = "voiceagent-loader",
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._backend = backend
        self.executor = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix=thread_name_prefix
        )
        self._active_items: set[str] = set()
        # INVARIANT: only `_on_progress_tick` writes this dict, and it
        # always runs on the owner thread. Worker threads must route
        # progress via `_emit_progress_from_worker` so the queued
        # signal serializes the writes.
        self._progress_by_item: dict[str, DownloadProgress] = {}
        # In-flight worker futures so `shutdown()` can do a bounded
        # join. The lock guards mutation from both owner-thread submits
        # and worker-thread done-callbacks (which `_untrack_inflight`
        # registers).
        self._inflight_futures: set[Future] = set()
        self._inflight_lock = threading.Lock()
        self._shutdown_started = False
        self._logger = logging.getLogger(self.__class__.__module__)

        self._progress_tick.connect(
            self._on_progress_tick, Qt.ConnectionType.QueuedConnection
        )
        self.load_completed.connect(self._finish_success)
        self.load_failed.connect(self._finish_failure)
        self.delete_completed.connect(self._finish_delete_success)
        self.delete_failed.connect(self._finish_delete_failure)

    # -- public surface ----------------------------------------------------

    @property
    def is_ready(self) -> bool:
        return self._backend.is_available

    @property
    def is_loading(self) -> bool:
        return bool(self._active_items)

    def is_item_loading(self, name: str) -> bool:
        return name in self._active_items

    def progress_for(self, name: str) -> DownloadProgress:
        return self._progress_by_item.get(name, _EMPTY_PROGRESS)

    @property
    def active_items(self) -> frozenset[str]:
        return frozenset(self._active_items)

    def shutdown(self, *, timeout: float = 2.0) -> None:
        """Stop accepting new work; cancel queued tasks; bounded-wait
        on in-flight workers.

        The previous `shutdown(wait=False, cancel_futures=True)` was a
        fire-and-forget call: queued tasks were cancelled, but any worker
        already running continued in the background after `shutdown`
        returned. Tests that asserted state immediately after
        `loader.shutdown()` could see in-flight emits leak into the next
        test, and app shutdown could leave a Piper voice mid-download
        well after `app.aboutToQuit` ran.

        New contract: when `shutdown()` returns, every in-flight worker
        has either finished (within `timeout` seconds) or been left to
        continue in the background. Workers that overrun the timeout
        rely on Qt's queued-connection safety net (a deleted-receiver
        emission is dropped silently — see `_handle_done`'s
        `name in self._active_items` guard for the application-side
        invariant).

        Idempotent — repeated calls are a no-op after the first.
        """
        if self._shutdown_started:
            return
        self._shutdown_started = True

        # Snapshot inflight futures BEFORE we tell the executor to stop —
        # we need their handles to wait on. The set is mutated from both
        # this owner thread and from worker-thread done-callbacks, so
        # take the snapshot under the lock.
        with self._inflight_lock:
            pending = [f for f in self._inflight_futures if not f.done()]

        # Cancel queued (not-yet-started) tasks; refuse new submissions.
        # `wait=False` returns immediately so we can run our bounded join
        # explicitly with a timeout below.
        self.executor.shutdown(wait=False, cancel_futures=True)

        if pending:
            futures_wait(pending, timeout=timeout)

    # -- inflight-future tracking -----------------------------------------

    def _track_inflight(self, future: Future) -> None:
        with self._inflight_lock:
            self._inflight_futures.add(future)
        # Registered AFTER add so that if the future is already done at
        # registration time (rare but possible — submit can race with
        # immediate completion in a tight test loop), the discard sees
        # the future in the set.
        future.add_done_callback(self._untrack_inflight)

    def _untrack_inflight(self, future: Future) -> None:
        # Runs on the worker thread when the future completes.
        with self._inflight_lock:
            self._inflight_futures.discard(future)

    # -- core operations ---------------------------------------------------

    def download_item(self, name: str) -> None:
        if (
            not name
            or name in self._active_items
            or self._backend.is_item_available(name)
            or not self._can_download(name)
        ):
            return

        was_idle = not self._active_items
        self._active_items.add(name)
        self._progress_by_item[name] = _EMPTY_PROGRESS
        self.item_loading_changed.emit(name, True)
        self.item_progress_changed.emit(name, _EMPTY_PROGRESS)
        if was_idle:
            self.loading_changed.emit(True)
        self.error_changed.emit("")
        self.status_changed.emit(self._status_checking())
        self.progress_changed.emit(self._aggregate_progress())

        future = self.executor.submit(self._download_worker, name)
        self._track_inflight(future)
        future.add_done_callback(lambda f, n=name: self._handle_done(f, n, "download"))

    def delete_item(self, name: str) -> None:
        if (
            not name
            or name in self._active_items
            or not self._backend.is_item_available(name)
        ):
            return

        was_idle = not self._active_items
        self._active_items.add(name)
        self._progress_by_item[name] = _EMPTY_PROGRESS
        self.item_loading_changed.emit(name, True)
        # Symmetric with download_item: emit the initial empty progress so
        # window.py's slot populates {stt,tts}ProgressMap, keeping the QML
        # delegate's "busy" predicate honest during deletes (which never
        # produce real progress ticks).
        self.item_progress_changed.emit(name, _EMPTY_PROGRESS)
        if was_idle:
            self.loading_changed.emit(True)
        self.error_changed.emit("")
        self.status_changed.emit(self._status_removing())
        self.progress_changed.emit(self._aggregate_progress())

        future = self.executor.submit(self._delete_worker, name)
        self._track_inflight(future)
        future.add_done_callback(lambda f, n=name: self._handle_done(f, n, "delete"))

    # -- subclass hooks ----------------------------------------------------

    def _can_download(self, name: str) -> bool:
        """Override to gate `download_item` on extra preconditions."""
        return True

    def _log_state(self, context: str) -> None:
        """Override for verbose diagnostic logging at state transitions."""

    def _status_checking(self) -> str:
        raise NotImplementedError

    def _status_downloading(self) -> str:
        raise NotImplementedError

    def _status_removing(self) -> str:
        raise NotImplementedError

    def _status_ready(self) -> str:
        raise NotImplementedError

    def _status_load_failed(self) -> str:
        raise NotImplementedError

    def _status_remove_failed(self) -> str:
        raise NotImplementedError

    def _status_idle_prompt(self) -> str:
        raise NotImplementedError

    def _status_removed_ok(self) -> str:
        raise NotImplementedError

    def _status_select_to_enable(self) -> str:
        raise NotImplementedError

    # -- download verification --------------------------------------------

    def _verify_download(self, name: str) -> Optional[str]:
        """Post-download integrity check. NOT a generic readiness probe.

        This hook runs from the worker thread on the success path of
        `_download_worker`, exactly once per install attempt, between
        the backend's `download_item` returning and `load_completed`
        being emitted. Returns `None` when the freshly downloaded
        artifacts pass verification, or an error message string that
        routes the install through `load_failed` + cleanup.

        It is **not** safe to call from a generic "is this ready?"
        codepath. Subclass overrides may run heavy probes — the Piper
        loader runs a one-shot `onnxruntime.InferenceSession` (~30–50
        ms) — that are appropriate for a one-time post-download gate
        but would be a serious cost regression if invoked from any
        per-row availability check (`is_item_available`,
        `_CatalogStateAdapter.is_installed`, etc.).

        The default implementation runs the cheap-and-generic layers:

        - **Layer 1 — aria2 sidecar.** For every artifact path
          reported by the backend, reject the download if a
          `<path>.aria2` sidecar is still present. aria2 writes this
          control file during active transfers and removes it on
          clean completion, so a leftover sidecar means the transfer
          was interrupted.
        - **Layer 2 — file size vs manifest.** If the backend exposes
          an `artifact_manifest(name)` method, every entry with a
          non-`None` `expected_size` is compared against the on-disk
          file size. A mismatch fails closed.
        - **Layer 3 — checksum vs manifest.** Same source. Every
          entry with a non-`None` `expected_checksum_hex` +
          `checksum_algorithm` is streamed through the named hash
          (md5 or sha256) and compared. A mismatch fails closed.

        Manifest entries with `None` fields are skipped at the
        respective layer — that allows a backend to publish only the
        data it actually has (e.g. HF non-LFS blobs carry size but
        not sha256) without a missing piece collapsing the whole
        check. A backend that doesn't implement `artifact_manifest`
        at all simply gets layer 1.

        Subclasses may override to add expensive layers on top (e.g.
        the Piper loader runs a smoke-load via onnxruntime — layer
        4). Overrides should run the base check first (via
        `super()._verify_download`) and only continue to their own
        checks when the base returns `None`.
        """
        try:
            paths = list(self._backend.artifact_paths(name))
        except Exception as exc:  # defensive: missing/broken impl
            self._logger.exception(
                "%s artifact_paths raised for item=%s",
                self.__class__.__name__,
                name,
            )
            return f"could not determine artifact paths: {exc}"

        # Layer 1 — aria2 sidecar.
        for path in paths:
            sidecar = path.with_name(path.name + ".aria2")
            if sidecar.exists():
                return (
                    f"aria2 sidecar still present for {path.name} "
                    f"— download did not complete cleanly"
                )

        # Layers 2 + 3 — only run when the backend ships a manifest.
        # `getattr` over hard-typing the `_ItemBackend` protocol so
        # third-party / test backends that don't carry manifests
        # degrade gracefully to layer 1 only.
        manifest_getter = getattr(self._backend, "artifact_manifest", None)
        if manifest_getter is None or not callable(manifest_getter):
            return None

        try:
            manifest = manifest_getter(name)
        except Exception as exc:
            # Fail-soft on manifest-source errors (e.g. HF API timeout
            # mid-install). The user just successfully downloaded
            # gigabytes; aborting the install on a metadata blip
            # would be terrible UX. Layer 1 already passed.
            self._logger.warning(
                "%s artifact_manifest raised for item=%s; skipping layers 2/3: %s",
                self.__class__.__name__,
                name,
                exc,
            )
            return None

        if not manifest:
            return None

        for path in paths:
            entry = manifest.get(path)
            if entry is None:
                continue
            size_error = self._verify_size(path, entry, name)
            if size_error is not None:
                return size_error
            checksum_error = self._verify_checksum(path, entry, name)
            if checksum_error is not None:
                return checksum_error

        return None

    @staticmethod
    def _verify_size(
        path: Path, entry: ArtifactManifestEntry, name: str
    ) -> Optional[str]:
        """Compare on-disk file size against the manifest entry.

        Fails closed when the manifest lists this file but it isn't
        on disk: a manifest-covered artifact MUST be present after
        a successful download, so a missing one is a real
        verification failure (not the optional-file tolerance that
        `manifest.get(path) is None` already handles upstream).

        Returns `None` when the size matches or when the entry has
        no expected size; an error string on size mismatch or a
        manifest-listed-but-absent file.
        """
        if entry.expected_size is None:
            return None
        try:
            actual_size = path.stat().st_size
        except FileNotFoundError:
            return (
                f"missing artifact for {path.name} (item={name}): "
                f"manifest lists this file but it is absent on disk"
            )
        if actual_size != entry.expected_size:
            return (
                f"size mismatch for {path.name} (item={name}): "
                f"expected {entry.expected_size} bytes, got {actual_size}"
            )
        return None

    @staticmethod
    def _verify_checksum(
        path: Path, entry: ArtifactManifestEntry, name: str
    ) -> Optional[str]:
        """Stream the file through the named hash and compare.

        Fails closed on missing-but-manifest-listed files (same
        rationale as `_verify_size`). Unknown algorithm names log
        a warning and skip layer 3 — that's a backend-config bug,
        not a corrupted download, and the operator-visible warning
        prevents silent integrity-check regressions if a backend
        ships a typo'd algorithm name.
        """
        expected_hex = entry.expected_checksum_hex
        algorithm = entry.checksum_algorithm
        if expected_hex is None or algorithm is None:
            return None
        if algorithm not in _SUPPORTED_CHECKSUM_ALGORITHMS:
            _module_logger.warning(
                "Skipping layer 3 for %s (item=%s): unsupported "
                "checksum algorithm %r — supported: %s",
                path.name,
                name,
                algorithm,
                sorted(_SUPPORTED_CHECKSUM_ALGORITHMS),
            )
            return None
        # Construct the hasher BEFORE opening the file so a
        # provider-unavailable error (notably md5 on FIPS-mode
        # OpenSSL builds, which raises `ValueError: [digital envelope
        # routines] unsupported`) is treated like the unsupported-
        # algorithm case: warn and skip layer 3, don't fail closed
        # against a healthy artifact. Without this, the worker's
        # broad `except Exception` would convert the ValueError into
        # a generic "verification hook raised" install failure.
        try:
            hasher = hashlib.new(algorithm)
        except (ValueError, TypeError) as exc:
            _module_logger.warning(
                "Skipping layer 3 for %s (item=%s): hashlib.new(%r) "
                "raised %s — this is typically FIPS-mode OpenSSL "
                "rejecting md5. Layers 1/2 still apply.",
                path.name,
                name,
                algorithm,
                exc,
            )
            return None
        try:
            with path.open("rb") as fh:
                while True:
                    chunk = fh.read(_CHECKSUM_READ_CHUNK)
                    if not chunk:
                        break
                    hasher.update(chunk)
        except FileNotFoundError:
            return (
                f"missing artifact for {path.name} (item={name}): "
                f"manifest lists this file but it is absent on disk "
                f"(cannot verify {algorithm} checksum)"
            )
        actual_hex = hasher.hexdigest().lower()
        expected_norm = expected_hex.lower()
        if actual_hex != expected_norm:
            return (
                f"{algorithm} mismatch for {path.name} (item={name}): "
                f"expected {expected_norm}, got {actual_hex}"
            )
        return None

    def _cleanup_failed_download(self, name: str) -> None:
        """Best-effort removal of partial artifacts + aria2 sidecars.

        Runs on the worker thread from the verification-failure path so
        the next attempt starts from a clean slate and stale partials
        never get mistaken for a good install by `is_item_available`.

        For nested-layout backends (e.g. Whisper, where artifacts live
        under `<model_root>/<item_name>/`) we also try to rmdir each
        artifact's parent directory if it became empty after the unlinks.
        Without this step the empty `<item_name>/` directory lingers; the
        next download pass treats it as empty so it isn't catastrophic,
        but the FS state reads cleaner without it.
        """
        try:
            paths = list(self._backend.artifact_paths(name))
        except Exception:
            self._logger.exception(
                "%s artifact_paths raised during cleanup for item=%s",
                self.__class__.__name__,
                name,
            )
            return

        for path in paths:
            sidecar = path.with_name(path.name + ".aria2")
            for target in (path, sidecar):
                try:
                    if target.exists():
                        target.unlink()
                except Exception:
                    self._logger.exception(
                        "%s cleanup failed to remove %s",
                        self.__class__.__name__,
                        target,
                    )

        # Best-effort rmdir on per-item nested directories that became
        # empty after the artifact unlinks. Two guards must both hold:
        #
        # 1. `parent.name == name` — restricts rmdir to layouts where
        #    each item lives in a dedicated subdirectory (Whisper's
        #    `<model_root>/<item_name>/`).
        # 2. `parent != backend_root` — the basename check alone is
        #    not enough. If the configured `VOICEAGENT_TTS_MODEL_ROOT`
        #    or `VOICEAGENT_STT_MODEL_ROOT` is a path whose basename
        #    happens to equal the item being installed (e.g.
        #    `/srv/voices/en_US-ryan-high/` for item `en_US-ryan-high`),
        #    Piper's flat-layout artifacts live directly under that
        #    root, so `parent.name == name` is satisfied AND `parent`
        #    is the shared root. Without this second guard, a failed
        #    verification deletes the entire model root and the next
        #    `voices.json` refresh fails inside
        #    `tempfile.mkstemp(dir=model_root)` until a human
        #    recreates the dir.
        backend_root = getattr(self._backend, "model_root", None)
        seen_parents: set[Path] = set()
        for path in paths:
            parent = path.parent
            if parent in seen_parents:
                continue
            seen_parents.add(parent)
            if parent.name != name:
                continue
            if backend_root is not None and parent == backend_root:
                continue
            try:
                if parent.is_dir() and not any(parent.iterdir()):
                    parent.rmdir()
            except Exception:
                # rmdir failure is fine — the next pass will treat the
                # directory as empty regardless.
                self._logger.debug(
                    "%s cleanup could not rmdir %s",
                    self.__class__.__name__,
                    parent,
                )

    # -- worker-thread side -----------------------------------------------

    def _download_worker(self, name: str) -> None:
        try:
            self.status_changed.emit(self._status_downloading())
            self._backend.download_item(
                name,
                progress_callback=lambda p, n=name: self._emit_progress_from_worker(n, p),
            )
        except Exception as exc:
            self._logger.exception("%s download failed", self.__class__.__name__)
            self.load_failed.emit(name, str(exc))
            return

        # Verify the download before marking the item ready. The base
        # implementation catches `.aria2` sidecar leftovers (partial
        # aria2 transfer); Piper overrides this with an additional
        # smoke-load layer. See the v0.3.2 FOLLOWUPS P2 item
        # ("Verify model/voice downloads before marking ready") for the
        # corrupt-ONNX bug this guards against.
        try:
            verification_error = self._verify_download(name)
        except Exception as exc:  # defensive: hook must not escape
            self._logger.exception(
                "%s verification hook raised", self.__class__.__name__
            )
            verification_error = f"verification hook raised: {exc}"

        if verification_error is not None:
            self._logger.error(
                "%s download verification failed item=%s message=%s",
                self.__class__.__name__,
                name,
                verification_error,
            )
            self._cleanup_failed_download(name)
            self.load_failed.emit(name, verification_error)
            return

        self.load_completed.emit(name)

    def _delete_worker(self, name: str) -> None:
        try:
            self._backend.remove_item(name)
        except Exception as exc:
            self._logger.exception("%s delete failed", self.__class__.__name__)
            self.delete_failed.emit(name, str(exc))
            return

        self.delete_completed.emit(name)

    def _emit_progress_from_worker(
        self, name: str, progress: DownloadProgress
    ) -> None:
        """Worker-thread entry point. Does NOT touch `_progress_by_item`.

        Must only emit `_progress_tick`; the queued connection delivers
        the update to `_on_progress_tick` on the owner thread, which is
        the sole writer of the dict.
        """
        self._progress_tick.emit(name, progress)

    def _handle_done(self, future: Future[None], name: str, operation: str) -> None:
        try:
            future.result()
        except Exception:
            self._logger.exception(
                "%s future raised unexpectedly for item=%s operation=%s",
                self.__class__.__name__,
                name,
                operation,
            )
            # Emit unconditionally: this callback runs on the executor
            # thread, so we must NOT read owner-thread state like
            # `_active_items` to gate the emission. The connected
            # finish-* slots run on the owner thread and each have an
            # idempotent `name in self._active_items` guard that
            # safely no-ops on stale/duplicate emissions (e.g. when
            # the worker already emitted load_failed before the future
            # bubbled the same exception up here).
            if operation == "delete":
                self.delete_failed.emit(name, "Item operation failed unexpectedly")
            else:
                self.load_failed.emit(name, "Item operation failed unexpectedly")

    # -- owner-thread slots ------------------------------------------------

    def _on_progress_tick(self, name: str, progress: DownloadProgress) -> None:
        # Sole writer of `_progress_by_item`. Runs on the owner thread
        # because `_progress_tick` is connected with QueuedConnection.
        if name not in self._active_items:
            # Tick arrived after finalization (e.g. a final aria2
            # progress update queued just before completion). Ignore;
            # finalization already emitted the terminal state.
            return
        self._progress_by_item[name] = progress
        self.item_progress_changed.emit(name, progress)
        self.progress_changed.emit(self._aggregate_progress())

    def _finish_success(self, name: str) -> None:
        if name not in self._active_items:
            return
        self._active_items.discard(name)
        self._progress_by_item.pop(name, None)
        self._log_state("finish_success")
        self.item_loading_changed.emit(name, False)
        if not self._active_items:
            self.loading_changed.emit(False)
        self.ready_changed.emit(self.is_ready)
        self.status_changed.emit(self._status_ready())
        self.progress_changed.emit(self._aggregate_progress())

    def _finish_failure(self, name: str, message: str) -> None:
        if name not in self._active_items:
            return
        self._logger.error(
            "%s load failed item=%s message=%s",
            self.__class__.__name__,
            name,
            message,
        )
        self._active_items.discard(name)
        self._progress_by_item.pop(name, None)
        self._log_state("finish_failure")
        self.item_loading_changed.emit(name, False)
        self.item_progress_changed.emit(name, _EMPTY_PROGRESS)
        if not self._active_items:
            self.loading_changed.emit(False)
        self.ready_changed.emit(self.is_ready)
        self.status_changed.emit(self._status_load_failed())
        self.error_changed.emit(message)
        self.progress_changed.emit(self._aggregate_progress())

    def _finish_delete_success(self, name: str) -> None:
        if name not in self._active_items:
            return
        self._active_items.discard(name)
        self._progress_by_item.pop(name, None)
        self._log_state("finish_delete_success")
        self.item_loading_changed.emit(name, False)
        self.item_progress_changed.emit(name, _EMPTY_PROGRESS)
        if not self._active_items:
            self.loading_changed.emit(False)
        self.ready_changed.emit(self.is_ready)
        self.status_changed.emit(self._status_removed_ok())
        self.progress_changed.emit(self._aggregate_progress())

    def _finish_delete_failure(self, name: str, message: str) -> None:
        if name not in self._active_items:
            return
        self._logger.error(
            "%s delete failed item=%s message=%s",
            self.__class__.__name__,
            name,
            message,
        )
        self._active_items.discard(name)
        self._progress_by_item.pop(name, None)
        self._log_state("finish_delete_failure")
        self.item_loading_changed.emit(name, False)
        self.item_progress_changed.emit(name, _EMPTY_PROGRESS)
        if not self._active_items:
            self.loading_changed.emit(False)
        self.ready_changed.emit(self.is_ready)
        self.status_changed.emit(self._status_remove_failed())
        self.error_changed.emit(message)
        self.progress_changed.emit(self._aggregate_progress())

    def _aggregate_progress(self) -> DownloadProgress:
        if not self._progress_by_item:
            return _EMPTY_PROGRESS
        completed = sum(p.completed_bytes for p in self._progress_by_item.values())
        total = sum(p.total_bytes for p in self._progress_by_item.values())
        speed = sum(
            p.download_speed_bytes_per_second for p in self._progress_by_item.values()
        )
        return DownloadProgress(
            completed_bytes=completed,
            total_bytes=total,
            download_speed_bytes_per_second=speed,
        )
