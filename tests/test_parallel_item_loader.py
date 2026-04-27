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

    def artifact_paths(self, name: str) -> list[Path]:
        # Base verifier only inspects these for sibling `.aria2` files;
        # the default test backend has no on-disk artifacts, so an
        # empty list is fine (verification passes for everything).
        return []


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


@pytest.fixture
def make_loader():
    """Yield a factory that builds a `ParallelItemLoader` subclass and
    registers it for unconditional shutdown.

    Each test was previously calling `loader = _ConcreteLoader(backend);
    ...; loader.shutdown()` with the shutdown trailing the body. If an
    assertion in the middle raised, the loader's executor leaked into
    the next test. Routing construction through this fixture moves the
    teardown into a pytest finalizer that runs even on assertion failure.
    """
    loaders: list[ParallelItemLoader] = []

    def _make(backend, loader_cls=_ConcreteLoader, **kwargs):
        loader = loader_cls(backend, **kwargs)
        loaders.append(loader)
        return loader

    yield _make

    for loader in loaders:
        try:
            loader.shutdown()
        except Exception:  # pragma: no cover - teardown best-effort
            pass


# --- tests ---------------------------------------------------------------


def test_loading_changed_idle_to_busy_edge_only(qtbot, make_loader):
    backend = FakeBackend()
    # Block the worker so we can observe the busy state synchronously.
    release = threading.Event()

    def strategy(name, cb):
        cb(DownloadProgress(completed_bytes=10, total_bytes=100, download_speed_bytes_per_second=0))
        release.wait(timeout=2.0)
        cb(DownloadProgress(completed_bytes=100, total_bytes=100, download_speed_bytes_per_second=0))
        backend.mark_installed(name)

    backend.download_strategy = strategy
    loader = make_loader(backend)

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


def test_two_parallel_downloads_aggregate(qtbot, make_loader):
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
    loader = make_loader(backend)

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


def test_success_does_not_synthesize_terminal_progress_tick(qtbot, make_loader):
    backend = FakeBackend()

    # Worker emits a single mid-download tick (30/100) and returns
    # without ever sending a final 100/100. If `_finish_success`
    # synthesized a terminal `item_progress_changed`, we'd see a
    # second `(name, 30/30)` (or similar full-bar) emission after
    # `item_loading_changed(name, False)`.
    def strategy(name, cb):
        cb(DownloadProgress(completed_bytes=30, total_bytes=100, download_speed_bytes_per_second=500))
        backend.mark_installed(name)

    backend.download_strategy = strategy
    loader = make_loader(backend)

    progress_events: list[tuple[str, DownloadProgress]] = []
    loader.item_progress_changed.connect(lambda n, p: progress_events.append((n, p)))
    loading_events: list[tuple[str, bool]] = []
    loader.item_loading_changed.connect(lambda n, b: loading_events.append((n, b)))

    loader.download_item("a")
    _wait(qtbot, lambda: not loader.is_loading and loader.is_ready)

    assert "a" not in loader._progress_by_item
    # `download_item` synchronously emits an initial 0/0 tick before the
    # worker runs; the worker then emits 30/100. No synthetic terminal
    # tick after `item_loading_changed("a", False)`.
    assert progress_events == [
        ("a", DownloadProgress(0, 0, 0)),
        ("a", DownloadProgress(completed_bytes=30, total_bytes=100, download_speed_bytes_per_second=500)),
    ]
    # `item_loading_changed(False)` is the last lifecycle event.
    assert loading_events[-1] == ("a", False)


def test_idempotent_finish_failure(qtbot, make_loader):
    backend = FakeBackend()

    def strategy(name, cb):
        raise RuntimeError("boom")

    backend.download_strategy = strategy
    loader = make_loader(backend)

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


def test_idempotent_finish_success(qtbot, make_loader):
    backend = FakeBackend()
    loader = make_loader(backend)

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


def test_progress_tick_from_worker_thread_is_safe(qtbot, make_loader):
    backend = FakeBackend()
    loader = make_loader(backend)

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


def test_progress_tick_after_finalization_is_dropped(qtbot, make_loader):
    backend = FakeBackend()
    loader = make_loader(backend)

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


def test_delete_clears_active_state(qtbot, make_loader):
    backend = FakeBackend()
    backend.mark_installed("a")
    loader = make_loader(backend)

    loader.delete_item("a")
    _wait(qtbot, lambda: not loader.is_loading)
    assert "a" not in loader._active_items
    assert ("remove", "a") in backend.calls


def test_handle_done_routes_failure_by_operation(qtbot, make_loader):
    """A catastrophic future failure must surface on the right signal.

    `_handle_done` is registered for both download and delete futures.
    If a delete future raises (e.g. cancelled by shutdown, slot-raised
    exception from delete_completed.emit), the failure must route
    through `delete_failed`, not `load_failed`. Codex round 3 P-minor.
    """
    from concurrent.futures import Future

    backend = FakeBackend()
    loader = make_loader(backend)
    loader._active_items.add("a")  # simulate in-flight operation

    load_failed_spy: list[tuple[str, str]] = []
    delete_failed_spy: list[tuple[str, str]] = []
    loader.load_failed.connect(lambda n, m: load_failed_spy.append((n, m)))
    loader.delete_failed.connect(lambda n, m: delete_failed_spy.append((n, m)))

    bad: Future = Future()
    bad.set_exception(RuntimeError("simulated catastrophic failure"))

    loader._handle_done(bad, "a", "delete")
    assert load_failed_spy == []
    assert len(delete_failed_spy) == 1 and delete_failed_spy[0][0] == "a"

    # Re-arm and verify the download path still routes to load_failed.
    loader._active_items.add("b")
    bad2: Future = Future()
    bad2.set_exception(RuntimeError("simulated catastrophic failure"))
    loader._handle_done(bad2, "b", "download")
    assert len(load_failed_spy) == 1 and load_failed_spy[0][0] == "b"
    assert len(delete_failed_spy) == 1  # unchanged


def test_subclass_missing_status_overrides_raises_at_class_build():
    # `__init_subclass__` enforces that every subclass overrides every
    # `_status_*` hook at class-definition time, not lazily at the first
    # call site. Without this, a forgotten override surfaces as
    # `NotImplementedError` only when the state machine reaches that
    # specific transition — situational and slow to diagnose.
    with pytest.raises(TypeError, match="must override all status hooks"):
        class _IncompleteLoader(ParallelItemLoader):
            # Define some hooks but deliberately omit several.
            def _status_checking(self) -> str:
                return "checking"

            def _status_downloading(self) -> str:
                return "downloading"


def test_shutdown_bounded_join_waits_for_inflight_within_timeout(qtbot, make_loader):
    """A worker that finishes within the bounded-join timeout is awaited
    cleanly by `shutdown(timeout=...)`.
    """
    backend = FakeBackend()
    release = threading.Event()
    finished = threading.Event()

    def strategy(name, cb):
        # Hold inside the worker until released.
        release.wait(timeout=2.0)
        cb(DownloadProgress(completed_bytes=100, total_bytes=100, download_speed_bytes_per_second=0))
        backend.mark_installed(name)
        finished.set()

    backend.download_strategy = strategy
    loader = make_loader(backend)
    loader.download_item("a")
    # Worker is now blocked inside the strategy. Release it just before
    # shutdown so the bounded-join sees a worker about to finish.
    release.set()
    # `shutdown` should join cleanly within the timeout.
    loader.shutdown(timeout=2.0)
    # The released worker had time to complete before shutdown returned.
    assert finished.is_set()


def test_shutdown_bounded_join_does_not_hang_on_overrunning_worker(qtbot, make_loader):
    """A worker that exceeds the bounded-join timeout is left to run in
    the background; `shutdown()` returns within roughly `timeout`
    seconds rather than blocking on the worker forever.
    """
    backend = FakeBackend()
    release = threading.Event()

    def strategy(name, cb):
        # Park indefinitely until the post-test cleanup releases.
        release.wait(timeout=5.0)
        backend.mark_installed(name)

    backend.download_strategy = strategy
    loader = make_loader(backend)
    loader.download_item("a")
    # Don't release — force the bounded-join to time out.
    start = time.monotonic()
    loader.shutdown(timeout=0.2)
    elapsed = time.monotonic() - start
    # 0.2s timeout + a bit of slack for thread scheduling. If the join
    # were unbounded this would hit the strategy's 5s wait.
    assert elapsed < 1.5, (
        f"shutdown blocked for {elapsed:.2f}s — bounded-join failed to honor timeout"
    )
    # Release the parked worker so the post-test cleanup (executor's
    # final teardown) can proceed.
    release.set()


def test_shutdown_is_idempotent(make_loader):
    """Calling `shutdown()` twice must be safe (the second call is a
    no-op rather than re-running the bounded join)."""
    backend = FakeBackend()
    loader = make_loader(backend)
    loader.shutdown(timeout=0.1)
    loader.shutdown(timeout=0.1)  # must not raise


def test_subclass_with_all_overrides_inherited_via_intermediate_passes(make_loader):
    # A subclass that inherits its overrides from an intermediate base
    # (rather than defining them directly) must also pass — the MRO
    # walk must look beyond `cls.__dict__`.
    class _Intermediate(ParallelItemLoader):
        def _status_checking(self) -> str:
            return "x"
        def _status_downloading(self) -> str:
            return "x"
        def _status_removing(self) -> str:
            return "x"
        def _status_ready(self) -> str:
            return "x"
        def _status_load_failed(self) -> str:
            return "x"
        def _status_remove_failed(self) -> str:
            return "x"
        def _status_idle_prompt(self) -> str:
            return "x"
        def _status_removed_ok(self) -> str:
            return "x"
        def _status_select_to_enable(self) -> str:
            return "x"

    # No new overrides — relies entirely on `_Intermediate`.
    class _Leaf(_Intermediate):
        pass

    backend = FakeBackend()
    loader = make_loader(backend, loader_cls=_Leaf)
    assert loader._status_checking() == "x"


# --- _cleanup_failed_download: rmdir guard regression coverage ----------


class _LayoutBackend(FakeBackend):
    """`FakeBackend` plus on-disk `model_root` and configurable artifact paths.

    Real backends (PiperTtsService, WhisperTranscriber) carry a
    `model_root: Path`. The cleanup rmdir guard reads it via `getattr`,
    so the test fake also exposes it.
    """

    def __init__(self, model_root: Path, artifacts_for: dict[str, list[Path]]) -> None:
        super().__init__()
        self.model_root = model_root
        self._artifacts_for = artifacts_for

    def artifact_paths(self, name: str) -> list[Path]:
        return list(self._artifacts_for.get(name, []))


def test_cleanup_does_not_rmdir_shared_model_root_with_matching_basename(
    tmp_path, make_loader,
):
    """Bug case from PR #11 review (P2, two rounds of evidence).

    If `VOICEAGENT_TTS_MODEL_ROOT` is set to a path whose basename
    matches the item name being installed, the 0.6.3 `parent.name == name`
    guard alone allowed `rmdir` of the shared root on failed verification.
    Recovery requires a human to recreate the directory.

    Scenario: Piper-style flat layout with `model_root.name == item_name`.
    """
    item = "en_US-ryan-high"
    model_root = tmp_path / item
    model_root.mkdir()
    artifact = model_root / f"{item}.onnx"
    artifact.write_bytes(b"fake")

    backend = _LayoutBackend(
        model_root=model_root, artifacts_for={item: [artifact]}
    )
    loader = make_loader(backend)

    loader._cleanup_failed_download(item)

    assert not artifact.exists(), "artifact must be unlinked"
    assert model_root.exists(), (
        "shared model_root must survive cleanup even when its basename "
        "matches the item name"
    )


def test_cleanup_rmdirs_per_item_subdirectory_when_safe(tmp_path, make_loader):
    """Whisper-style nested layout: the per-item subdir IS removed.

    Regression guard against over-tightening the rmdir condition — the
    shared-root guard must not also block the legitimate per-item
    cleanup.
    """
    item = "large"
    model_root = tmp_path
    item_dir = model_root / item
    item_dir.mkdir()
    artifact = item_dir / "model.bin"
    artifact.write_bytes(b"fake")

    backend = _LayoutBackend(
        model_root=model_root, artifacts_for={item: [artifact]}
    )
    loader = make_loader(backend)

    loader._cleanup_failed_download(item)

    assert not artifact.exists()
    assert not item_dir.exists(), "empty per-item subdir should be rmdir'd"
    assert model_root.exists(), "model_root itself must survive"


def test_cleanup_does_not_rmdir_flat_layout_root(tmp_path, make_loader):
    """Piper-style flat layout, no basename collision.

    `parent.name` ("voices") differs from `item` ("en_US-ryan-high"), so
    the original 0.6.3 guard already protected this case. Pin it down
    so the new shared-root guard doesn't accidentally regress it.
    """
    item = "en_US-ryan-high"
    model_root = tmp_path / "voices"
    model_root.mkdir()
    artifact = model_root / f"{item}.onnx"
    artifact.write_bytes(b"fake")

    backend = _LayoutBackend(
        model_root=model_root, artifacts_for={item: [artifact]}
    )
    loader = make_loader(backend)

    loader._cleanup_failed_download(item)

    assert not artifact.exists()
    assert model_root.exists()
