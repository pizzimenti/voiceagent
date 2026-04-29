"""Thread-safety tests for `voiceagent.controller.VoiceController`.

Covers the queued-signal bridge that routes executor-thread callback
mutations of `_active_pipeline_count` and `_partial_inflight` through
`_pipeline_count_delta` / `_partial_inflight_changed`, both connected
via `Qt.QueuedConnection` so the paired slots run on the owner thread
and act as the sole writer for each attribute.

Mirrors the pattern demonstrated in
`tests/test_parallel_item_loader.py::test_progress_tick_from_worker_thread_is_safe`
and the `test_llm_controller.py` fakes.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Callable

import pytest
from PySide6.QtCore import QObject, QCoreApplication, Signal

from voiceagent.controller import VoiceController


# --- Test doubles ---------------------------------------------------------
#
# VoiceController depends on a MicrophoneRecorder, an STT backend, a chat
# client, a TTS backend, and an AudioPlayer. The thread-safety invariants
# we care about live entirely in the controller's worker-callback code
# paths, so we provide the thinnest fakes that satisfy the constructor
# plus the attributes the controller touches at import time.


class _FakeRecorder:
    """Minimal MicrophoneRecorder stand-in.

    Exposes only the surface VoiceController touches during the tests
    (mostly just the `is_recording` flag and the sample_rate). Attributes
    that are only read during the recording paths we don't exercise are
    left as stubs.
    """

    def __init__(self) -> None:
        self.sample_rate = 16000
        self.is_recording = False

    def stop(self, *, discard: bool = False) -> None:
        self.is_recording = False

    def start(self, *, segment_ready_callback=None) -> None:
        self.is_recording = True

    def take_pending_segment(self):
        return None

    def snapshot_active_segment(self):
        return None

    def force_finalize_active_segment(self, reason: str) -> bool:
        return False

    def suspend_input(self) -> None:
        pass

    def resume_input(self, warmup_seconds: float = 0.0, reason: str = "") -> None:
        pass


class _FakeTranscriber:
    is_loaded = True
    backend_name = "Fake"
    selection_label = "Model"

    def ensure_loaded(self) -> None:
        pass

    def transcribe(self, path: Path) -> str:
        return ""


class _FakeChatClient:
    def complete(self, text: str) -> str:
        return ""


class _FakeTts:
    enabled = False

    def synthesize(self, text: str):
        return None


class _FakePlayer(QObject):
    # Match AudioPlayer's signal surface so the controller's connects
    # succeed without dragging in the real audio stack.
    playback_started = Signal(str)
    playback_finished = Signal(str)
    playback_failed = Signal(str, str)

    def stop(self) -> None:
        pass

    def play_file(self, path) -> bool:
        return True


@pytest.fixture
def controller(qtbot):
    """A real VoiceController wired to fake dependencies.

    `qtbot` is requested to ensure a QApplication exists; the controller
    itself is not added to qtbot because it has no widget to track.
    """

    ctrl = VoiceController(
        recorder=_FakeRecorder(),
        transcriber=_FakeTranscriber(),
        chat_client=_FakeChatClient(),
        tts_service=_FakeTts(),
        player=_FakePlayer(),
    )
    yield ctrl
    ctrl.shutdown()


def _process_events(times: int = 5) -> None:
    app = QCoreApplication.instance()
    assert app is not None
    for _ in range(times):
        app.processEvents()


# --- Tests ---------------------------------------------------------------


def test_pipeline_count_delta_from_worker_thread_is_safe(qtbot, controller):
    """Emit `_pipeline_count_delta` from a non-Qt thread; the queued
    connection must deliver the update to the owner thread, which is the
    sole writer of `_active_pipeline_count`."""

    # Seed a non-zero starting value on the owner thread so we can
    # observe both increment and decrement deltas.
    controller._active_pipeline_count = 2

    def worker():
        # +1 then -1 twice: net zero delta from start, landing at 2.
        controller._pipeline_count_delta.emit(1)
        controller._pipeline_count_delta.emit(-1)
        controller._pipeline_count_delta.emit(1)
        controller._pipeline_count_delta.emit(-1)

    t = threading.Thread(target=worker)
    t.start()
    t.join(timeout=2.0)
    assert not t.is_alive()

    # Drain the queued events and verify the final owner-thread state.
    qtbot.waitUntil(
        lambda: controller._active_pipeline_count == 2, timeout=2000
    )


def test_pipeline_count_clamps_to_zero_from_worker(qtbot, controller):
    """Even if a worker over-decrements, the slot clamps at zero so the
    attribute never goes negative (matches the legacy `max(0, ...)`
    semantics)."""

    controller._active_pipeline_count = 1

    def worker():
        controller._pipeline_count_delta.emit(-1)
        controller._pipeline_count_delta.emit(-1)
        controller._pipeline_count_delta.emit(-1)

    t = threading.Thread(target=worker)
    t.start()
    t.join(timeout=2.0)
    qtbot.waitUntil(
        lambda: controller._active_pipeline_count == 0, timeout=2000
    )


def test_partial_inflight_changed_from_worker_thread_is_safe(qtbot, controller):
    """Emit `_partial_inflight_changed` from a non-Qt thread and verify
    the owner-thread slot is the sole writer."""

    controller._partial_inflight = True

    def worker():
        controller._partial_inflight_changed.emit(False)

    t = threading.Thread(target=worker)
    t.start()
    t.join(timeout=2.0)
    qtbot.waitUntil(
        lambda: controller._partial_inflight is False, timeout=2000
    )

    # Flip it back the other way.
    def worker_on():
        controller._partial_inflight_changed.emit(True)

    t2 = threading.Thread(target=worker_on)
    t2.start()
    t2.join(timeout=2.0)
    qtbot.waitUntil(
        lambda: controller._partial_inflight is True, timeout=2000
    )


def test_concurrent_pipeline_count_stress(qtbot, controller):
    """Kick off N concurrent worker threads that each emit the full
    increment+decrement pair the real pipeline would produce, and assert
    the net effect resolves to zero on the owner thread with no
    RuntimeError.

    Simulates the shape of `_handle_segment_ready` (+1 on owner thread)
    + `_handle_pipeline_done` (-1 on executor thread) across many
    parallel pipelines. Here we emit both deltas from worker threads so
    the test actually exercises the queued path for both directions."""

    # Start fresh.
    controller._active_pipeline_count = 0

    worker_count = 4
    iterations = 10
    barrier = threading.Barrier(worker_count)
    errors: list[BaseException] = []

    def worker():
        try:
            barrier.wait(timeout=2.0)
            for _ in range(iterations):
                controller._pipeline_count_delta.emit(1)
                controller._pipeline_count_delta.emit(-1)
        except BaseException as exc:  # pragma: no cover - diagnostic
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(worker_count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5.0)

    assert not errors, f"Worker threads raised: {errors!r}"
    assert all(not t.is_alive() for t in threads)

    # All 4*10*2 = 80 queued events must drain to a net-zero count.
    qtbot.waitUntil(
        lambda: controller._active_pipeline_count == 0, timeout=4000
    )


def test_protected_attributes_have_invariant_comment():
    """Guardrail: future edits that drop the INVARIANT comment on the
    two protected attributes should fail loudly here. The docstring on
    `_progress_by_item` in parallel_item_loader.py plays the same role."""

    source = Path(__file__).resolve().parent.parent / "src" / "voiceagent" / "controller.py"
    text = source.read_text(encoding="utf-8")
    # Both attributes must be accompanied by an INVARIANT comment that
    # names the slot that is the sole writer.
    assert "INVARIANT: only the owner thread writes `_active_pipeline_count`" in text
    assert "_on_pipeline_count_delta" in text
    assert "INVARIANT: only the owner thread writes `_partial_inflight`" in text
    assert "_on_partial_inflight_changed" in text


def test_queued_connection_is_used_for_bridges():
    """Sanity check that the two internal bridges exist as Signals on
    the class so mis-renames during refactors fail fast."""

    assert hasattr(VoiceController, "_pipeline_count_delta")
    assert hasattr(VoiceController, "_partial_inflight_changed")


def test_pipeline_count_decrements_inline_when_emitted_on_owner_thread(qtbot, controller):
    """`Future.add_done_callback` invokes the callback synchronously on the
    caller's thread when the future is already done at registration time.
    For our submit/add-done pattern that's the owner thread.

    With QueuedConnection, the same-thread emit would *post* the
    decrement instead of applying it inline; subsequent same-thread
    signals (pipeline_completed/pipeline_failed) would then read a stale
    `_active_pipeline_count` and skip the resume/state-transition logic.

    AutoConnection (the new default for these bridges) routes same-thread
    emits inline, so by the time pipeline_completed fires the count is
    already correct.

    Codex P2 round 3 on PR #6.
    """

    controller._active_pipeline_count = 3
    # Same-thread emit (the owner thread is currently executing this test).
    controller._pipeline_count_delta.emit(-1)
    # No event loop spin needed — the slot must have run inline.
    assert controller._active_pipeline_count == 2

    # The cross-thread path still works.
    def _emit_from_worker():
        controller._pipeline_count_delta.emit(-1)

    t = threading.Thread(target=_emit_from_worker)
    t.start()
    t.join(timeout=1.0)
    # Cross-thread emit queues; spin the loop.
    deadline = 50
    while controller._active_pipeline_count != 1 and deadline > 0:
        _process_events(2)
        deadline -= 1
    assert controller._active_pipeline_count == 1


def test_partial_inflight_clears_inline_when_emitted_on_owner_thread(qtbot, controller):
    """Same property as above for the partial-inflight bridge."""

    controller._partial_inflight = True
    controller._partial_inflight_changed.emit(False)
    # Inline same-thread delivery via AutoConnection.
    assert controller._partial_inflight is False


# --- Empty-transcript short-circuit (v0.9.14) ---------------------------


class _RecordingChatClient:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def complete(self, text: str) -> str:
        self.calls.append(text)
        return "should-not-be-returned"


class _EmptyTranscriber:
    is_loaded = True
    backend_name = "Fake"
    selection_label = "Model"

    def ensure_loaded(self) -> None:
        pass

    def transcribe(self, path: Path) -> str:
        return ""


class _WhitespaceTranscriber(_EmptyTranscriber):
    def transcribe(self, path: Path) -> str:
        return "   \n  "


def _make_controller(transcriber, chat_client):
    return VoiceController(
        recorder=_FakeRecorder(),
        transcriber=transcriber,
        chat_client=chat_client,
        tts_service=_FakeTts(),
        player=_FakePlayer(),
    )


def test_run_pipeline_skips_chat_on_empty_transcript(qtbot, tmp_path):
    """`_run_pipeline` must NOT call `chat_client.complete` when the
    transcriber returns an empty string. This is the v0.9.14 fix for the
    "Whisper did not return any transcript" pipeline failure that
    surfaced silence as a red error row instead of a clean no-op."""

    chat = _RecordingChatClient()
    ctrl = _make_controller(_EmptyTranscriber(), chat)
    try:
        audio = tmp_path / "fake.wav"
        audio.write_bytes(b"")  # _run_pipeline unlinks in finally
        result = ctrl._run_pipeline(audio)
    finally:
        ctrl.shutdown()

    assert chat.calls == []
    assert result.transcript == ""
    assert result.response == ""
    assert result.tts_audio_path is None


def test_run_pipeline_skips_chat_on_whitespace_only_transcript(qtbot, tmp_path):
    """A transcript of pure whitespace is also a no-speech outcome and
    must not reach the LLM."""

    chat = _RecordingChatClient()
    ctrl = _make_controller(_WhitespaceTranscriber(), chat)
    try:
        audio = tmp_path / "fake.wav"
        audio.write_bytes(b"")
        result = ctrl._run_pipeline(audio)
    finally:
        ctrl.shutdown()

    assert chat.calls == []
    assert result.transcript == ""
    assert result.response == ""
    assert result.tts_audio_path is None


def test_run_pipeline_unlinks_audio_after_empty_transcript(qtbot, tmp_path):
    """The `finally` cleanup that deletes the recorded WAV still fires
    when we short-circuit on empty — otherwise stale audio would
    accumulate in the tempdir for every silent turn."""

    chat = _RecordingChatClient()
    ctrl = _make_controller(_EmptyTranscriber(), chat)
    try:
        audio = tmp_path / "fake.wav"
        audio.write_bytes(b"\x00")
        assert audio.exists()
        ctrl._run_pipeline(audio)
        assert not audio.exists()
    finally:
        ctrl.shutdown()
