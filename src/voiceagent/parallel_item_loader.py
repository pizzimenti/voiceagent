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

from concurrent.futures import Future, ThreadPoolExecutor
import logging
from typing import Callable, Protocol, runtime_checkable

from PySide6.QtCore import QObject, Qt, Signal

from voiceagent.downloaders import DownloadProgress


_EMPTY_PROGRESS = DownloadProgress(
    completed_bytes=0, total_bytes=0, download_speed_bytes_per_second=0
)


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


class ParallelItemLoader(QObject):
    """Base class for parallel model/voice loaders.

    Subclasses customize the user-facing status strings via the
    `_status_*` hooks and may override `_can_download` and
    `_log_state` to add preconditions or diagnostics.
    """

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

    def shutdown(self) -> None:
        self.executor.shutdown(wait=False, cancel_futures=True)

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
        future.add_done_callback(lambda f, n=name: self._handle_done(f, n))

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
        future.add_done_callback(lambda f, n=name: self._handle_done(f, n))

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

    def _handle_done(self, future: Future[None], name: str) -> None:
        try:
            future.result()
        except Exception:
            self._logger.exception(
                "%s future raised unexpectedly for item=%s",
                self.__class__.__name__,
                name,
            )
            if name in self._active_items:
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
        total = self._progress_by_item.pop(name, _EMPTY_PROGRESS).total_bytes or 1
        self._log_state("finish_success")
        self.item_loading_changed.emit(name, False)
        self.item_progress_changed.emit(
            name,
            DownloadProgress(
                completed_bytes=total,
                total_bytes=total,
                download_speed_bytes_per_second=0,
            ),
        )
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
