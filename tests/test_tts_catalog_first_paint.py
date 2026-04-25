"""Tests for the deferred `voices.json` catalog refresh.

The first-paint path must never block on the Piper voice catalog
network fetch. These exercise three properties:

1. `PiperTtsService.available_items()` returns the eager on-disk union
   (installed `.onnx` + configured voice + cached `voices.json`) without
   touching the network.
2. `TtsVoiceLoader.refresh_catalog_async()` runs the fetch off-thread
   and emits `catalog_changed` exactly once when the fetch adds new
   voices.
3. A network failure during the refresh leaves the eager catalog intact
   and does not emit `catalog_changed`.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# Ensure the worktree's `src/` resolves before any editable install.
_HERE = Path(__file__).resolve().parent
_SRC = _HERE.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

# Headless Qt for any environment that didn't already set this.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QCoreApplication

from voiceagent.services.tts import PiperTtsService
from voiceagent.tts_loader import TtsVoiceLoader


# --- helpers -------------------------------------------------------------


def _touch_piper_voice(model_root: Path, name: str) -> None:
    """Create the paired `.onnx` + `.onnx.json` files Piper expects."""
    (model_root / f"{name}.onnx").write_bytes(b"fake-onnx-payload")
    (model_root / f"{name}.onnx.json").write_text("{}", encoding="utf-8")


def _write_voices_cache(model_root: Path, names: list[str]) -> None:
    """Write a synthetic `voices.json` keyed by `names`."""
    payload = {name: {"key": name} for name in names}
    (model_root / "voices.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )


def _make_service(model_root: Path, configured: str | None = None) -> PiperTtsService:
    service = PiperTtsService(command=["piper"], model_path=configured)
    service.model_root = model_root
    return service


def _wait_for(qtbot, predicate, timeout: int = 2000) -> None:
    qtbot.waitUntil(predicate, timeout=timeout)


# --- service-level: eager catalog never touches the network ---------------


def test_available_items_no_cache_no_network(tmp_path, monkeypatch):
    """No cache + no network → empty list, and `urlopen` is never called."""
    calls: list[str] = []

    def _boom(*_args, **_kwargs):  # pragma: no cover - must not execute
        calls.append("called")
        raise RuntimeError("network must not be touched on eager path")

    monkeypatch.setattr("voiceagent.services.tts.urllib.request.urlopen", _boom)

    service = _make_service(tmp_path)
    assert service.available_items() == []
    assert calls == []


def test_available_items_includes_installed_and_configured(tmp_path, monkeypatch):
    """Installed `.onnx` pairs + configured model surface without the network."""
    monkeypatch.setattr(
        "voiceagent.services.tts.urllib.request.urlopen",
        lambda *a, **k: pytest.fail("eager path hit the network"),
    )

    _touch_piper_voice(tmp_path, "en_US-ryan-high")
    service = _make_service(tmp_path, configured="en_US-amy-medium")
    items = service.available_items()
    assert "en_US-ryan-high" in items
    assert "en_US-amy-medium" in items


def test_available_items_reads_existing_cache(tmp_path, monkeypatch):
    """A cached `voices.json` contributes its keys without any fetch."""
    monkeypatch.setattr(
        "voiceagent.services.tts.urllib.request.urlopen",
        lambda *a, **k: pytest.fail("cache path hit the network"),
    )
    _write_voices_cache(tmp_path, ["en_US-cached-low", "de_DE-cached-medium"])
    service = _make_service(tmp_path)
    items = service.available_items()
    assert "en_US-cached-low" in items
    assert "de_DE-cached-medium" in items


# --- service-level: explicit network refresh writes cache, returns union --


def test_refresh_remote_catalog_writes_cache_and_returns_union(tmp_path, monkeypatch):
    _touch_piper_voice(tmp_path, "en_US-ryan-high")
    payload = json.dumps({"en_US-remote-low": {}, "fr_FR-remote-high": {}})

    class _FakeResponse:
        def __init__(self, body: str) -> None:
            self._body = body.encode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return self._body

    def _fake_urlopen(url, timeout=None):
        return _FakeResponse(payload)

    monkeypatch.setattr(
        "voiceagent.services.tts.urllib.request.urlopen", _fake_urlopen
    )

    service = _make_service(tmp_path)
    refreshed = service.refresh_catalog()
    assert "en_US-ryan-high" in refreshed  # installed on disk
    assert "en_US-remote-low" in refreshed  # from remote
    assert "fr_FR-remote-high" in refreshed
    # Cache was persisted so the next cold launch is fast.
    cache = json.loads((tmp_path / "voices.json").read_text())
    assert set(cache.keys()) == {"en_US-remote-low", "fr_FR-remote-high"}


def test_refresh_remote_catalog_network_failure_returns_eager(tmp_path, monkeypatch):
    _touch_piper_voice(tmp_path, "en_US-ryan-high")

    def _boom(*_args, **_kwargs):
        raise OSError("no route to host")

    monkeypatch.setattr(
        "voiceagent.services.tts.urllib.request.urlopen", _boom
    )

    service = _make_service(tmp_path)
    refreshed = service.refresh_catalog()
    # Network failed, but the eager list is still returned — never raises.
    assert refreshed == ["en_US-ryan-high"]
    # No cache was written (the fetch never produced a payload).
    assert not (tmp_path / "voices.json").exists()


# --- loader-level: async refresh emits `catalog_changed` once on success --


def test_refresh_catalog_async_emits_once_on_success(tmp_path, monkeypatch, qtbot):
    _touch_piper_voice(tmp_path, "en_US-ryan-high")
    payload = json.dumps(
        {"en_US-remote-low": {}, "en_US-ryan-high": {}, "fr_FR-remote-high": {}}
    )

    class _FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return payload.encode("utf-8")

    monkeypatch.setattr(
        "voiceagent.services.tts.urllib.request.urlopen",
        lambda *a, **k: _FakeResponse(),
    )

    service = _make_service(tmp_path)
    loader = TtsVoiceLoader(service)
    received: list[list[str]] = []
    loader.catalog_changed.connect(lambda names: received.append(list(names)))

    loader.refresh_catalog_async()
    # Idempotent — a second call while the first is in flight is a no-op.
    loader.refresh_catalog_async()

    _wait_for(qtbot, lambda: len(received) >= 1)
    # Pump any late-emitted duplicates so we can assert "exactly once".
    for _ in range(10):
        QCoreApplication.instance().processEvents()

    assert len(received) == 1
    names = received[0]
    assert "en_US-remote-low" in names
    assert "fr_FR-remote-high" in names
    assert "en_US-ryan-high" in names
    loader.shutdown()


def test_refresh_catalog_async_network_failure_is_silent(tmp_path, monkeypatch, qtbot):
    _touch_piper_voice(tmp_path, "en_US-ryan-high")

    def _boom(*_args, **_kwargs):
        raise OSError("offline")

    monkeypatch.setattr(
        "voiceagent.services.tts.urllib.request.urlopen", _boom
    )

    service = _make_service(tmp_path)
    loader = TtsVoiceLoader(service)
    received: list[list[str]] = []
    loader.catalog_changed.connect(lambda names: received.append(list(names)))

    loader.refresh_catalog_async()
    # Give the worker thread + queued signal time to land.
    _wait_for(qtbot, lambda: loader.catalog_refresh_scheduled, timeout=500)
    for _ in range(20):
        QCoreApplication.instance().processEvents()

    # Refresh failed → eager catalog stays put, no broken signal.
    assert received == []
    # And the eager catalog is still reachable for QML.
    assert "en_US-ryan-high" in service.available_items()
    loader.shutdown()


def test_refresh_catalog_async_no_emit_when_nothing_changed(tmp_path, monkeypatch, qtbot):
    """A remote refresh that matches the eager catalog must not emit."""
    _touch_piper_voice(tmp_path, "en_US-ryan-high")
    payload = json.dumps({"en_US-ryan-high": {}})

    class _FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return payload.encode("utf-8")

    monkeypatch.setattr(
        "voiceagent.services.tts.urllib.request.urlopen",
        lambda *a, **k: _FakeResponse(),
    )

    service = _make_service(tmp_path)
    loader = TtsVoiceLoader(service)
    received: list[list[str]] = []
    loader.catalog_changed.connect(lambda names: received.append(list(names)))

    loader.refresh_catalog_async()
    # Drain the executor + queued signal.
    for _ in range(20):
        QCoreApplication.instance().processEvents()

    assert received == []
    loader.shutdown()
