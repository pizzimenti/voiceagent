"""Tests for `voiceagent.parallel_item_loader.ParallelItemLoader`.

These exercise three properties that motivated extracting the base
class:

- The state machine emits `loading_changed` on idle->busy edges only.
- Worker-thread progress callbacks route through a queued signal so
  `_progress_by_item` is only ever written by the owner thread, even
  when multiple workers tick concurrently.
- `_finish_*` slots are idempotent: a second `load_failed` for the
  same item (e.g. one from inside the worker, one from `_handle_done`)
  must be a no-op and not re-emit transition signals.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path
from typing import Callable

# Ensure the worktree's `src/` resolves before any editable install.
_HERE = Path(__file__).resolve().parent
_SRC = _HERE.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

# Headless Qt for any environment that didn't already set this.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QCoreApplication

from voiceagent.downloaders import DownloadProgress
from voiceagent.parallel_item_loader import ParallelItemLoader


# --- test doubles --------------------------------------------------------


class FakeBackend:
    """Test double satisfying the `_ItemBackend` protocol.

    Tests configure the side effect of `download_item` via
    `download_strategy`. The fake records every progress callback so
    tests can drive ticks themselves (single-threaded) or run a thread
    that ticks then completes.
    """

    backend_name = "Test"
    selection_label = "model"

    def __init__(self, available_names: tuple[str, ...] = ("a", "b", "c")) -> None:
        self._available = list(available_names)
        self._installed: set[str] = set()
        self._lock = threading.Lock()
        self.download_strategy: Callable[[str, Callable[[DownloadProgress], None]], None] | None = None
        self.calls: list[tuple[str, str]] = []

    @property
    def is_available(self) -> bool:
        return bool(self._installed)

    def available_items(self) -> list[str]:
        return list(self._available)

    def is_item_available(self, name: str) -> bool:
        with self._lock:
            return name in self._installed

    def mark_installed(self, name: str) -> None:
        with self._lock:
            self._installed.add(name)

    def download_item(
        self,
        name: str,
        progress_callback: Callable[[DownloadProgress], None] | None = None,
    ) -> None:
        self.calls.append(("download", name))
        cb = progress_callback or (lambda _p: None)
        if self.download_strategy is not None:
            self.download_strategy(name, cb)
            return
        # Default: emit a tick at half, then complete.
        cb(DownloadProgress(completed_bytes=50, total_bytes=100, download_speed_bytes_per_second=1000))
        cb(DownloadProgress(completed_bytes=100, total_bytes=100, download_speed_bytes_per_second=0))
        self.mark_installed(name)

    def remove_item(self, name: str) -> None:
        self.calls.append(("remove", name))
        with self._lock:
            self._installed.discard(name)


class _ConcreteLoader(ParallelItemLoader):
    """Minimal subclass with trivial status strings, for testing the base."""

    def _status_checking(self) -> str:
        return "checking"

    def _status_downloading(self) -> str:
        return "downloading"

    def _status_removing(self) -> str:
        return "removing"

    def _status_ready(self) -> str:
        return "ready"

    def _status_load_failed(self) -> str:
        return "load_failed"

    def _status_remove_failed(self) -> str:
        return "remove_failed"

    def _status_idle_prompt(self) -> str:
        return "idle"

    def _status_removed_ok(self) -> str:
        return "removed"

    def _status_select_to_enable(self) -> str:
        return "select_to_enable"


# --- helpers -------------------------------------------------------------


def _wait(qtbot, predicate, timeout: int = 2000) -> None:
    qtbot.waitUntil(predicate, timeout=timeout)


def _process_events(times: int = 5) -> None:
    app = QCoreApplication.instance()
    assert app is not None
    for _ in range(times):
        app.processEvents()


# --- tests ---------------------------------------------------------------


def test_loading_changed_idle_to_busy_edge_only(qtbot):
    backend = FakeBackend()
    # Block the worker so we can observe the busy state synchronously.
    release = threading.Event()

    def strategy(name, cb):
        cb(DownloadProgress(completed_bytes=10, total_bytes=100, download_speed_bytes_per_second=0))
        release.wait(timeout=2.0)
        cb(DownloadProgress(completed_bytes=100, total_bytes=100, download_speed_bytes_per_second=0))
        backend.mark_installed(name)

    backend.download_strategy = strategy
    loader = _ConcreteLoader(backend)
    qtbot.addWidget  # noqa: B018  (qtbot installs the QApplication)

    loading_signals: list[bool] = []
    loader.loading_changed.connect(loading_signals.append)

    loader.download_item("a")
    # idle -> busy
    assert loading_signals == [True]

    loader.download_item("b")
    # still busy, no extra emission
    assert loading_signals == [True]

    release.set()
    _wait(qtbot, lambda: not loader.is_loading)
    # busy -> idle exactly once at the end
    assert loading_signals == [True, False]
    loader.shutdown()


def test_two_parallel_downloads_aggregate(qtbot):
    backend = FakeBackend()
    started = threading.Barrier(2)
    release = threading.Event()

    def strategy(name, cb):
        started.wait(timeout=2.0)
        if name == "a":
            cb(DownloadProgress(completed_bytes=30, total_bytes=100, download_speed_bytes_per_second=500))
        else:
            cb(DownloadProgress(completed_bytes=70, total_bytes=200, download_speed_bytes_per_second=1500))
        release.wait(timeout=2.0)
        cb(DownloadProgress(
            completed_bytes=100 if name == "a" else 200,
            total_bytes=100 if name == "a" else 200,
            download_speed_bytes_per_second=0,
        ))
        backend.mark_installed(name)

    backend.download_strategy = strategy
    loader = _ConcreteLoader(backend)

    aggregate_updates: list[DownloadProgress] = []
    loader.progress_changed.connect(aggregate_updates.append)

    loader.download_item("a")
    loader.download_item("b")

    # Wait for both ticks to land on the owner thread.
    _wait(qtbot, lambda: loader.progress_for("a").completed_bytes == 30 and loader.progress_for("b").completed_bytes == 70)

    aggregate = loader._aggregate_progress()
    assert aggregate.completed_bytes == 100  # 30 + 70
    assert aggregate.total_bytes == 300       # 100 + 200
    assert aggregate.download_speed_bytes_per_second == 2000  # 500 + 1500

    release.set()
    _wait(qtbot, lambda: not loader.is_loading)
    loader.shutdown()


def test_success_finalizes_per_item_progress(qtbot):
    backend = FakeBackend()
    loader = _ConcreteLoader(backend)

    item_progress: list[tuple[str, DownloadProgress]] = []
    loader.item_progress_changed.connect(lambda n, p: item_progress.append((n, p)))

    loader.download_item("a")
    _wait(qtbot, lambda: not loader.is_loading and loader.is_ready)

    # `_progress_by_item` is cleaned up by `_finish_success`.
    assert "a" not in loader._progress_by_item
    # Final tick is total/total.
    final = item_progress[-1]
    assert final[0] == "a"
    assert final[1].completed_bytes == final[1].total_bytes > 0
    loader.shutdown()


def test_idempotent_finish_failure(qtbot):
    backend = FakeBackend()

    def strategy(name, cb):
        raise RuntimeError("boom")

    backend.download_strategy = strategy
    loader = _ConcreteLoader(backend)

    loading_signals: list[bool] = []
    loader.loading_changed.connect(loading_signals.append)
    error_signals: list[str] = []
    loader.error_changed.connect(error_signals.append)

    loader.download_item("a")
    _wait(qtbot, lambda: "a" not in loader._active_items)

    # First failure went through. Now simulate `_handle_done` racing in
    # with a second `load_failed` for the same item.
    loader.load_failed.emit("a", "different message")
    _process_events()

    # No second loading_changed(False); no second error_changed.
    assert loading_signals.count(False) == 1
    assert len(error_signals) == 2  # ["", "boom"] from the legitimate path
    assert error_signals[-1] == "boom"
    loader.shutdown()


def test_idempotent_finish_success(qtbot):
    backend = FakeBackend()
    loader = _ConcreteLoader(backend)

    loading_signals: list[bool] = []
    loader.loading_changed.connect(loading_signals.append)

    loader.download_item("a")
    _wait(qtbot, lambda: not loader.is_loading)

    # Replay load_completed; with idempotency it should be a no-op.
    item_loading_events: list[tuple[str, bool]] = []
    loader.item_loading_changed.connect(lambda n, b: item_loading_events.append((n, b)))
    loader.load_completed.emit("a")
    _process_events()
    assert item_loading_events == []
    # And no extra loading_changed.
    assert loading_signals == [True, False]
    loader.shutdown()


def test_progress_tick_from_worker_thread_is_safe(qtbot):
    backend = FakeBackend()
    loader = _ConcreteLoader(backend)

    # Skip the executor entirely; manually mark item active so ticks
    # are accepted, and emit progress from a non-Qt thread.
    loader._active_items.add("a")
    loader._progress_by_item["a"] = DownloadProgress(0, 0, 0)

    received: list[DownloadProgress] = []
    loader.item_progress_changed.connect(lambda n, p: received.append(p))

    def worker():
        for i in range(1, 6):
            loader._emit_progress_from_worker(
                "a",
                DownloadProgress(
                    completed_bytes=i * 10,
                    total_bytes=100,
                    download_speed_bytes_per_second=100,
                ),
            )
            time.sleep(0.005)

    t = threading.Thread(target=worker)
    t.start()
    _wait(qtbot, lambda: len(received) >= 5, timeout=3000)
    t.join(timeout=2.0)
    assert not t.is_alive()

    # The owner thread is the sole writer, and the last tick wins.
    assert loader._progress_by_item["a"].completed_bytes == 50
    loader.shutdown()


def test_progress_tick_after_finalization_is_dropped(qtbot):
    backend = FakeBackend()
    loader = _ConcreteLoader(backend)

    loader.download_item("a")
    _wait(qtbot, lambda: not loader.is_loading)
    # `a` is now finalized; progress_by_item no longer has it.
    assert "a" not in loader._progress_by_item

    # A late tick for `a` arrives. The slot should drop it.
    received: list[tuple[str, DownloadProgress]] = []
    loader.item_progress_changed.connect(lambda n, p: received.append((n, p)))
    loader._emit_progress_from_worker(
        "a",
        DownloadProgress(completed_bytes=99, total_bytes=100, download_speed_bytes_per_second=0),
    )
    _process_events()
    assert received == []
    assert "a" not in loader._progress_by_item
    loader.shutdown()


def test_delete_clears_active_state(qtbot):
    backend = FakeBackend()
    backend.mark_installed("a")
    loader = _ConcreteLoader(backend)

    loader.delete_item("a")
    _wait(qtbot, lambda: not loader.is_loading)
    assert "a" not in loader._active_items
    assert ("remove", "a") in backend.calls
    loader.shutdown()
