"""Tests for `ParallelItemLoader`'s post-download verification hook.

These exercise the multi-layer verification introduced to catch the
v0.3.1 corrupt-Piper-voice bug: a partial aria2 transfer left an
`.onnx` on disk beside an `.aria2` control sidecar, the loader emitted
`load_completed`, and first-synth crashed with
`onnxruntime InvalidProtobuf` → `wave.Error: # channels not specified`.

The base loader runs layer 1 (aria2 sidecar rejection); the Piper
loader adds layer 4 (smoke-load via onnxruntime). These tests isolate
the base-class behavior with a `FakeBackend` so they don't need a real
Piper voice or onnxruntime install at test time.
"""

from __future__ import annotations

import os
import sys
import threading
from pathlib import Path
from typing import Callable, Optional

# Ensure the worktree's `src/` resolves before any editable install.
_HERE = Path(__file__).resolve().parent
_SRC = _HERE.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

# Headless Qt for any environment that didn't already set this.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication

from voiceagent.downloaders import DownloadProgress
from voiceagent.parallel_item_loader import ParallelItemLoader


# --- test doubles --------------------------------------------------------


class VerifyingFakeBackend:
    """Backend double that "downloads" into a real tmpdir.

    Tests configure `download_strategy` to write a specific on-disk
    shape (onnx only, onnx + aria2 sidecar, onnx + json, etc.) so the
    real verifier — which walks `artifact_paths` — can fire against
    actual files.
    """

    backend_name = "TestBackend"
    selection_label = "item"

    def __init__(self, model_root: Path) -> None:
        self.model_root = model_root
        self._installed: set[str] = set()
        self._lock = threading.Lock()
        self.download_strategy: Callable[[str, Path, Callable[[DownloadProgress], None]], None] | None = None
        self.calls: list[tuple[str, str]] = []

    @property
    def is_available(self) -> bool:
        return bool(self._installed)

    def available_items(self) -> list[str]:
        return ["v1", "v2"]

    def is_item_available(self, name: str) -> bool:
        with self._lock:
            return name in self._installed

    def download_item(
        self,
        name: str,
        progress_callback: Callable[[DownloadProgress], None] | None = None,
    ) -> None:
        self.calls.append(("download", name))
        cb = progress_callback or (lambda _p: None)
        if self.download_strategy is not None:
            self.download_strategy(name, self.model_root, cb)
        with self._lock:
            self._installed.add(name)

    def remove_item(self, name: str) -> None:
        self.calls.append(("remove", name))
        with self._lock:
            self._installed.discard(name)

    def artifact_paths(self, name: str) -> list[Path]:
        onnx = self.model_root / f"{name}.onnx"
        json_ = self.model_root / f"{name}.onnx.json"
        return [onnx, json_]


class _ConcreteLoader(ParallelItemLoader):
    _status_checking = lambda self: "checking"  # type: ignore[assignment]
    _status_downloading = lambda self: "downloading"  # type: ignore[assignment]
    _status_removing = lambda self: "removing"  # type: ignore[assignment]
    _status_ready = lambda self: "ready"  # type: ignore[assignment]
    _status_load_failed = lambda self: "load_failed"  # type: ignore[assignment]
    _status_remove_failed = lambda self: "remove_failed"  # type: ignore[assignment]
    _status_idle_prompt = lambda self: "idle"  # type: ignore[assignment]
    _status_removed_ok = lambda self: "removed"  # type: ignore[assignment]
    _status_select_to_enable = lambda self: "select_to_enable"  # type: ignore[assignment]


class _SmokeLoader(_ConcreteLoader):
    """Layer-4 simulator: always returns a fake InvalidProtobuf error."""

    fake_error: Optional[str] = "smoke-load failed (InvalidProtobuf): bad bytes"

    def _verify_download(self, name: str) -> Optional[str]:
        base = super()._verify_download(name)
        if base is not None:
            return base
        return self.fake_error


# --- helpers -------------------------------------------------------------


def _wait(qtbot, predicate, timeout: int = 2000) -> None:
    qtbot.waitUntil(predicate, timeout=timeout)


def _pump() -> None:
    app = QCoreApplication.instance()
    assert app is not None
    for _ in range(10):
        app.processEvents()


# --- tests ---------------------------------------------------------------


def test_aria2_sidecar_rejects_download(tmp_path, qtbot):
    """A `.aria2` sidecar next to the target artifact means "transfer
    did not finish cleanly" — the loader must route to `load_failed`,
    not `load_completed`.
    """
    backend = VerifyingFakeBackend(tmp_path)

    def _strategy(name, root, cb):
        # Simulate a partial aria2 transfer: real onnx bytes, but the
        # control sidecar was never cleaned up.
        (root / f"{name}.onnx").write_bytes(b"not a real onnx but that's fine")
        (root / f"{name}.onnx.json").write_text("{}", encoding="utf-8")
        (root / f"{name}.onnx.aria2").write_bytes(b"aria2-control")

    backend.download_strategy = _strategy
    loader = _ConcreteLoader(backend)

    completed: list[str] = []
    failed: list[tuple[str, str]] = []
    loader.load_completed.connect(completed.append)
    loader.load_failed.connect(lambda n, m: failed.append((n, m)))

    loader.download_item("v1")
    _wait(qtbot, lambda: not loader.is_loading)

    assert completed == []
    assert len(failed) == 1
    name, message = failed[0]
    assert name == "v1"
    assert "aria2" in message.lower()
    loader.shutdown()


def test_clean_download_still_completes(tmp_path, qtbot):
    """Regression check: without a sidecar, the base verifier passes and
    `load_completed` still fires.
    """
    backend = VerifyingFakeBackend(tmp_path)

    def _strategy(name, root, cb):
        (root / f"{name}.onnx").write_bytes(b"onnx-bytes")
        (root / f"{name}.onnx.json").write_text("{}", encoding="utf-8")

    backend.download_strategy = _strategy
    loader = _ConcreteLoader(backend)

    completed: list[str] = []
    failed: list[tuple[str, str]] = []
    loader.load_completed.connect(completed.append)
    loader.load_failed.connect(lambda n, m: failed.append((n, m)))

    loader.download_item("v1")
    _wait(qtbot, lambda: not loader.is_loading)

    assert completed == ["v1"]
    assert failed == []
    loader.shutdown()


def test_failure_path_cleans_up_partials(tmp_path, qtbot):
    """After a verification failure, the partial `.onnx`, its config, and
    the `.aria2` sidecar must all be removed so a retry starts clean.
    """
    backend = VerifyingFakeBackend(tmp_path)

    def _strategy(name, root, cb):
        (root / f"{name}.onnx").write_bytes(b"partial")
        (root / f"{name}.onnx.json").write_text("{}", encoding="utf-8")
        (root / f"{name}.onnx.aria2").write_bytes(b"aria2-control")

    backend.download_strategy = _strategy
    loader = _ConcreteLoader(backend)

    failed: list[tuple[str, str]] = []
    loader.load_failed.connect(lambda n, m: failed.append((n, m)))

    loader.download_item("v1")
    _wait(qtbot, lambda: not loader.is_loading)

    assert len(failed) == 1
    assert not (tmp_path / "v1.onnx").exists()
    assert not (tmp_path / "v1.onnx.json").exists()
    assert not (tmp_path / "v1.onnx.aria2").exists()
    loader.shutdown()


def test_smoke_load_failure_routes_to_load_failed(tmp_path, qtbot):
    """Simulate layer 4: even when the base aria2 check passes, a
    smoke-load that raises (e.g. `InvalidProtobuf`) must still route
    through `load_failed`. This mirrors the Piper override.
    """
    backend = VerifyingFakeBackend(tmp_path)

    def _strategy(name, root, cb):
        # Clean on-disk shape (no sidecar) but the bytes are junk. The
        # base verifier happily accepts this; only the layer-4 override
        # catches it.
        (root / f"{name}.onnx").write_bytes(b"corrupt-onnx-payload")
        (root / f"{name}.onnx.json").write_text("{}", encoding="utf-8")

    backend.download_strategy = _strategy
    loader = _SmokeLoader(backend)

    completed: list[str] = []
    failed: list[tuple[str, str]] = []
    loader.load_completed.connect(completed.append)
    loader.load_failed.connect(lambda n, m: failed.append((n, m)))

    loader.download_item("v1")
    _wait(qtbot, lambda: not loader.is_loading)

    assert completed == []
    assert len(failed) == 1
    _, message = failed[0]
    assert "InvalidProtobuf" in message
    # And the partial artifacts were cleaned up too.
    assert not (tmp_path / "v1.onnx").exists()
    loader.shutdown()


def test_verification_hook_exception_does_not_escape(tmp_path, qtbot):
    """An override that raises must surface as a clean `load_failed` rather
    than bubbling up into the executor's `_handle_done` path.
    """
    backend = VerifyingFakeBackend(tmp_path)

    def _strategy(name, root, cb):
        (root / f"{name}.onnx").write_bytes(b"bytes")
        (root / f"{name}.onnx.json").write_text("{}", encoding="utf-8")

    backend.download_strategy = _strategy

    class _BoomLoader(_ConcreteLoader):
        def _verify_download(self, name):
            raise RuntimeError("hook exploded")

    loader = _BoomLoader(backend)
    failed: list[tuple[str, str]] = []
    completed: list[str] = []
    loader.load_failed.connect(lambda n, m: failed.append((n, m)))
    loader.load_completed.connect(completed.append)

    loader.download_item("v1")
    _wait(qtbot, lambda: not loader.is_loading)

    assert completed == []
    assert len(failed) == 1
    assert "hook exploded" in failed[0][1]
    loader.shutdown()


def test_cleanup_does_not_rmdir_shared_model_root(tmp_path, qtbot):
    """Flat-layout backends (Piper) keep all artifacts directly under
    `model_root`. After a verification failure the cleanup unlinks the
    item's files but MUST NOT remove `model_root` itself, even if it
    happens to be empty afterwards — the next `voices.json` refresh
    writes via `tempfile.mkstemp(dir=model_root)` and would silently
    break if the dir disappeared.

    The `parent.name == name` gate in `_cleanup_failed_download`
    enforces this: for Piper, `parent` is `model_root` (not `<root>/v1`),
    so the gate fails and rmdir is skipped.
    """
    backend = VerifyingFakeBackend(tmp_path)

    def _strategy(name, root, cb):
        (root / f"{name}.onnx").write_bytes(b"partial")
        (root / f"{name}.onnx.json").write_text("{}", encoding="utf-8")
        (root / f"{name}.onnx.aria2").write_bytes(b"aria2-control")

    backend.download_strategy = _strategy
    loader = _ConcreteLoader(backend)

    loader.download_item("v1")
    _wait(qtbot, lambda: not loader.is_loading)

    # Files cleaned up.
    assert not (tmp_path / "v1.onnx").exists()
    assert not (tmp_path / "v1.onnx.aria2").exists()
    # Shared root MUST still exist — without this guard, Piper installs
    # would nuke the user's TTS model directory on first failure.
    assert tmp_path.exists()
    assert tmp_path.is_dir()
    loader.shutdown()


def test_cleanup_rmdirs_per_item_subdirectory(tmp_path, qtbot):
    """Nested-layout backends (Whisper) keep artifacts under
    `<model_root>/<item_name>/`. After a verification failure the
    cleanup should unlink the artifacts AND rmdir the per-item
    subdirectory if it became empty — leaves the FS in a clean state
    for the next pass.
    """

    class _NestedBackend(VerifyingFakeBackend):
        def artifact_paths(self, name: str):
            base = self.model_root / name
            return [base / "config.json", base / "model.bin"]

    backend = _NestedBackend(tmp_path)

    def _strategy(name, root, cb):
        item_dir = root / name
        item_dir.mkdir(parents=True, exist_ok=True)
        (item_dir / "config.json").write_text("{}", encoding="utf-8")
        (item_dir / "model.bin").write_bytes(b"partial")
        (item_dir / "model.bin.aria2").write_bytes(b"aria2-control")

    backend.download_strategy = _strategy
    loader = _ConcreteLoader(backend)

    loader.download_item("v1")
    _wait(qtbot, lambda: not loader.is_loading)

    # Per-item subdir is gone (empty after cleanup, so rmdir fired).
    assert not (tmp_path / "v1").exists()
    # Shared root stays put.
    assert tmp_path.exists()
    loader.shutdown()
