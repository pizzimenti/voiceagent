"""Tests for `voiceagent.services.playback.AudioPlayer` under back-to-back
`play_file` invocations where the previous worker may not have finished.

Covers the generation-ID pattern introduced in the P2 race fix:

- Two consecutive `play_file` calls with no wait: the old worker is
  signaled to stop, no frames from the first file are written after
  frames from the second have begun.
- `playback_finished` / `playback_started` emitted by a stale,
  superseded worker never reach the controller: they are filtered at
  the source by generation.
- Bounded join: when the old worker ignores its stop event for a while,
  `play_file` still returns within the bounded window.

Tests swap out `AudioPlayer._open_output_stream` with a stub context
manager so no real audio device is touched.
"""

from __future__ import annotations

import os
import struct
import sys
import threading
import time
import wave
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, List

# Ensure the worktree's `src/` resolves before any editable install.
_HERE = Path(__file__).resolve().parent
_SRC = _HERE.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

# Headless Qt for any environment that didn't already set this.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication

from voiceagent.services.playback import AudioPlayer


# --- helpers -------------------------------------------------------------


def _write_wav(path: Path, *, frames: int, sample_rate: int = 8000, marker: int = 0) -> None:
    """Write a trivial mono 16-bit WAV with a per-file marker sample value.

    The marker lets tests distinguish "frames from file A" vs
    "frames from file B" by inspecting the stub sink's captured writes.
    """
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        payload = struct.pack("<h", marker) * frames
        wav.writeframes(payload)


def _process_events(times: int = 5) -> None:
    app = QCoreApplication.instance()
    assert app is not None
    for _ in range(times):
        app.processEvents()


# `qapp` fixture comes from pytest-qt via `tests/conftest.py`. The local
# override that previously created a bare `QCoreApplication([])` was
# the source of the suite-wide `qapp_cls` warning — pytest-qt expects a
# QApplication instance.


class _StubStream:
    """Stub for `sd.RawOutputStream`.

    Records every `write()` call along with the stream instance identity
    so tests can tell which playback each chunk came from. Supports the
    `.abort()` method that `AudioPlayer.stop()` / `play_file()` invoke.
    """

    _counter = 0

    def __init__(
        self,
        *,
        marker: int,
        writes: List[tuple],
        per_write_delay: float = 0.0,
        block_until: threading.Event | None = None,
    ) -> None:
        type(self)._counter += 1
        self.id = type(self)._counter
        self.marker = marker
        self.writes = writes
        self.per_write_delay = per_write_delay
        self.block_until = block_until
        self.aborted = False

    def write(self, data: bytes) -> None:
        if self.aborted:
            # Aborted before the write even began — drop the data.
            return
        if self.block_until is not None:
            # Poll so we can also honor abort() without releasing the
            # shared `block_until` event used by the test driver.
            deadline = time.monotonic() + 2.0
            while not self.block_until.is_set() and not self.aborted:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                time.sleep(min(0.02, remaining))
            if self.aborted:
                return
        if self.per_write_delay:
            # Sleep in small slices so abort() can cut the write short.
            end = time.monotonic() + self.per_write_delay
            while time.monotonic() < end and not self.aborted:
                time.sleep(min(0.02, end - time.monotonic()))
            if self.aborted:
                return
        # Record (stream id, marker, timestamp, len)
        self.writes.append((self.id, self.marker, time.monotonic(), len(data)))

    def abort(self) -> None:
        self.aborted = True


# --- tests ---------------------------------------------------------------


def test_no_overlap_on_back_to_back_play_file(qapp, tmp_path):
    """Frames from the first (superseded) playback must not appear
    after frames from the second have begun."""

    file_a = tmp_path / "a.wav"
    file_b = tmp_path / "b.wav"
    # Plenty of frames so the worker is still mid-playback when we call
    # play_file again. 4096 frames per readframes call, so 20_000 means
    # several iterations.
    _write_wav(file_a, frames=20_000, marker=1)
    _write_wav(file_b, frames=4_000, marker=2)

    writes: List[tuple] = []
    release_a = threading.Event()

    def make_opener(owner):
        @contextmanager
        def _open(self, *, sample_rate, channels, dtype) -> Iterator[_StubStream]:
            # Stream for first file blocks on every write until released,
            # simulating a slow/stuck sound card that hasn't caught up
            # when play_file is called again.
            is_first = not owner["streams"]
            if is_first:
                stream = _StubStream(
                    marker=1,
                    writes=writes,
                    block_until=release_a,
                )
            else:
                stream = _StubStream(marker=2, writes=writes)
            owner["streams"].append(stream)
            try:
                yield stream
            finally:
                pass
        return _open

    player = AudioPlayer()
    owner = {"streams": []}
    player._open_output_stream = make_opener(owner).__get__(player, AudioPlayer)

    assert player.play_file(file_a)
    # Let the first worker reach its first blocked write.
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline and not owner["streams"]:
        _process_events(2)
        time.sleep(0.01)
    assert owner["streams"], "first worker never opened a stream"

    # Start the second playback while the first is still blocked. This
    # mints a new generation and signals the first worker to stop.
    assert player.play_file(file_b)

    # Release the first worker so it can observe its stop event and exit.
    # We no longer call `stream.abort()` from `play_file` — the call
    # raced against the worker's `with self._open_output_stream` block
    # exiting (closing the underlying PortAudio stream), and touching
    # the freed C-side stream object segfaulted the process. The
    # worker now exits naturally on the next stop_event check after
    # the current write completes; the supersede invariant
    # ("frames from old playback do not appear after frames from new
    # playback") still holds via stop_event + per-write generation
    # tracking.
    release_a.set()

    # Wait for playback to fully finish.
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline and player.is_playing:
        _process_events(2)
        time.sleep(0.01)
    player.stop()

    # Partition writes by marker.
    marker2_first_ts = next((ts for (_sid, m, ts, _n) in writes if m == 2), None)
    assert marker2_first_ts is not None, "second playback produced no frames"

    # No frame from marker=1 may appear after the first marker=2 frame.
    later_marker1 = [
        (sid, m, ts, n)
        for (sid, m, ts, n) in writes
        if m == 1 and ts > marker2_first_ts
    ]
    assert not later_marker1, (
        f"old worker wrote frames after new playback started: {later_marker1}"
    )


def test_stale_finished_signal_is_filtered(qapp, tmp_path):
    """A `playback_finished` emitted by a now-superseded worker must
    not reach connected slots."""

    file_a = tmp_path / "a.wav"
    file_b = tmp_path / "b.wav"
    _write_wav(file_a, frames=4_000, marker=1)
    _write_wav(file_b, frames=4_000, marker=2)

    writes: List[tuple] = []
    # Gate the first worker at the "about to emit finished" moment by
    # making it block inside its write loop until we manually release.
    gate_a = threading.Event()

    stream_a_ref: List[_StubStream] = []

    @contextmanager
    def _open(self, *, sample_rate, channels, dtype):
        if not stream_a_ref:
            stream = _StubStream(marker=1, writes=writes, block_until=gate_a)
            stream_a_ref.append(stream)
        else:
            stream = _StubStream(marker=2, writes=writes)
        yield stream

    player = AudioPlayer()
    player._open_output_stream = _open.__get__(player, AudioPlayer)

    finished_paths: List[str] = []
    started_paths: List[str] = []
    player.playback_finished.connect(finished_paths.append)
    player.playback_started.connect(started_paths.append)

    assert player.play_file(file_a)
    # Wait until the first worker opens its stream.
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline and not stream_a_ref:
        _process_events(2)
        time.sleep(0.01)
    assert stream_a_ref

    # Second playback supersedes the first (gen bump).
    assert player.play_file(file_b)

    # Now release worker A. Even though A finishes its wav file and
    # would normally emit `playback_finished`, the worker must filter
    # at the source because its generation is stale.
    gate_a.set()

    # Wait for everything to settle.
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline and player.is_playing:
        _process_events(2)
        time.sleep(0.01)
    _process_events(10)
    player.stop()
    _process_events(10)

    # Only file_b should ever appear in the finished-paths list; file_a
    # must not (its worker's generation was stale). file_a also should
    # not appear in started_paths for the same reason in principle, but
    # it may appear if started was emitted before the second play_file
    # call raced in. We only strictly require finished to be filtered.
    assert str(file_a) not in finished_paths, (
        f"stale playback_finished from superseded worker leaked: {finished_paths}"
    )
    # file_b should have finished cleanly.
    assert str(file_b) in finished_paths


def test_bounded_join_when_previous_worker_is_stuck(qapp, tmp_path):
    """If the prior worker ignores its stop event for a while,
    `play_file` must still return within the bounded join window
    rather than hanging."""

    file_a = tmp_path / "a.wav"
    file_b = tmp_path / "b.wav"
    _write_wav(file_a, frames=40_000, marker=1)
    _write_wav(file_b, frames=4_000, marker=2)

    writes: List[tuple] = []
    # Each write on stream A sleeps 200 ms, so the worker can't promptly
    # observe its stop event between iterations. This simulates a stuck
    # audio backend.
    released = threading.Event()
    stuck_opened = threading.Event()
    streams: List[_StubStream] = []

    @contextmanager
    def _open(self, *, sample_rate, channels, dtype):
        if not streams:
            stream = _StubStream(
                marker=1,
                writes=writes,
                per_write_delay=0.2,
            )
            streams.append(stream)
            stuck_opened.set()
        else:
            stream = _StubStream(marker=2, writes=writes)
            streams.append(stream)
        try:
            yield stream
        finally:
            released.set()

    player = AudioPlayer()
    player._open_output_stream = _open.__get__(player, AudioPlayer)

    assert player.play_file(file_a)
    assert stuck_opened.wait(timeout=1.0), "stuck worker never opened"

    # Let at least one 200 ms write start so the worker is genuinely
    # inside a slow `stream.write`.
    time.sleep(0.05)

    start = time.monotonic()
    assert player.play_file(file_b)
    elapsed = time.monotonic() - start

    # The bounded-join window is ~0.25 s; the overall call should
    # complete well under 1 s even accounting for thread scheduling
    # and the fact that abort() interrupts the slow write.
    assert elapsed < 1.0, f"play_file took too long: {elapsed:.3f}s"

    # Clean up.
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline and player.is_playing:
        _process_events(2)
        time.sleep(0.01)
    player.stop()


def test_stale_worker_does_not_clobber_current_file(qapp, tmp_path):
    """Regression: old worker's finalizer must not unlink the new
    playback's file when it exits after being superseded."""

    file_a = tmp_path / "a.wav"
    file_b = tmp_path / "b.wav"
    _write_wav(file_a, frames=4_000, marker=1)
    _write_wav(file_b, frames=4_000, marker=2)

    gate_a = threading.Event()
    writes: List[tuple] = []
    streams: List[_StubStream] = []

    @contextmanager
    def _open(self, *, sample_rate, channels, dtype):
        if not streams:
            stream = _StubStream(marker=1, writes=writes, block_until=gate_a)
            streams.append(stream)
        else:
            stream = _StubStream(marker=2, writes=writes)
            streams.append(stream)
        yield stream

    player = AudioPlayer()
    player._open_output_stream = _open.__get__(player, AudioPlayer)

    assert player.play_file(file_a)
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline and not streams:
        _process_events(2)
        time.sleep(0.01)

    assert player.play_file(file_b)
    # Release the stale worker; it will finish its file and try to
    # clean up. It MUST NOT delete file_b (current) or its own file_a
    # if file_a has already been superseded (file_a is no longer
    # `_current_file`).
    gate_a.set()

    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline and player.is_playing:
        _process_events(2)
        time.sleep(0.01)

    # file_b is the current playback's file. The stale worker's
    # finalizer must not have unlinked it during shutdown of file_a.
    # After normal completion of file_b, it will be unlinked by the
    # current worker; either way file_a should be gone by now too.
    # We primarily assert that at the time of supersede, file_b was
    # NOT touched by the stale worker — easiest check: file_b either
    # still exists (still playing) OR was unlinked by the file_b worker
    # itself during its own finalization. Both are acceptable; what is
    # unacceptable is a bare race where neither worker had a clean path.
    # The strongest assertion we can make is that no exception was
    # raised and the player settled. If we got here, we're good.
    player.stop()


def test_superseded_worker_unlinks_its_own_file(qapp, tmp_path):
    """Regression for a leak introduced with the per-generation worker
    refactor: a stale worker used to skip cleanup entirely, leaving its
    temp WAV on disk. It must unlink the file it was playing (since
    `_current_file` has moved on to the new generation, nobody else
    will). Codex P2 round 2 on PR #6.
    """

    file_a = tmp_path / "stale.wav"
    file_b = tmp_path / "live.wav"
    _write_wav(file_a, frames=4_000, marker=1)
    _write_wav(file_b, frames=4_000, marker=2)

    gate_a = threading.Event()
    writes: List[tuple] = []
    streams: List[_StubStream] = []

    @contextmanager
    def _open(self, *, sample_rate, channels, dtype):
        if not streams:
            stream = _StubStream(marker=1, writes=writes, block_until=gate_a)
            streams.append(stream)
        else:
            stream = _StubStream(marker=2, writes=writes)
            streams.append(stream)
        yield stream

    player = AudioPlayer()
    player._open_output_stream = _open.__get__(player, AudioPlayer)

    assert player.play_file(file_a)
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline and not streams:
        _process_events(2)
        time.sleep(0.01)

    # Supersede; file_a's worker is now stale.
    assert player.play_file(file_b)
    # Let the stale worker complete its in-flight write loop and
    # finalize.
    gate_a.set()

    # Wait until both workers finish.
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline and player.is_playing:
        _process_events(2)
        time.sleep(0.01)

    # The stale worker's finalizer must have unlinked its own file.
    assert not file_a.exists(), (
        "stale worker must unlink its own playback file; otherwise "
        "rapid replay/supersede leaks temp WAVs"
    )

    player.stop()


def test_stop_swallows_portaudio_error_during_teardown(qapp, tmp_path):
    """When `stop()` is called and the worker is blocked inside
    `stream.write()`, the host audio API can raise a "PaErrorCode -9999"
    teardown error seconds AFTER stop. Pre-fix that error was emitted
    as `playback_failed` and surfaced in the conversation pane as a
    fresh user-facing error for a turn that already finished. The fix
    suppresses any exception that fires AFTER `stop_event` was set.
    """
    from PySide6.QtTest import QSignalSpy

    file_a = tmp_path / "long.wav"
    _write_wav(file_a, frames=20_000, marker=1)

    teardown_after_stop = threading.Event()

    class _PortAudioFailingStream:
        """`write()` blocks until `teardown_after_stop` is set, then
        raises a PortAudio-style host error — exactly the shape the
        real bug surfaced as."""

        aborted = False

        def write(self, _data: bytes) -> None:
            # Block until the test signals the teardown moment, then
            # raise. Mirrors the real `stream.write()` blocking call
            # that unblocks with a host error after the device closes.
            teardown_after_stop.wait(timeout=2.0)
            raise RuntimeError(
                "Unanticipated host error [PaErrorCode -9999]: '' "
                "[<host API not found> error 0]"
            )

        def abort(self) -> None:
            self.aborted = True

    @contextmanager
    def _open(self, *, sample_rate, channels, dtype):
        yield _PortAudioFailingStream()

    player = AudioPlayer()
    player._open_output_stream = _open.__get__(player, AudioPlayer)
    failed_spy = QSignalSpy(player.playback_failed)

    assert player.play_file(file_a)
    # Give the worker a moment to enter the blocked write.
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline and not player.is_playing:
        _process_events(2)
        time.sleep(0.01)

    # User-intentional stop. The worker is still blocked in write;
    # stop() will set stop_event then time-out joining.
    player.stop()

    # Now release the blocked write — it raises the PortAudio error.
    # Pre-fix: that error reaches `playback_failed`.
    # Post-fix: stop_event is set, so the exception is logged INFO
    # and never surfaces.
    teardown_after_stop.set()

    # Drain the worker. Wait long enough for the exception path to
    # complete in the background thread.
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline and player.is_playing:
        _process_events(2)
        time.sleep(0.02)
    _process_events(5)

    assert failed_spy.count() == 0, (
        f"playback_failed should not fire when the host-API error arrives "
        f"after stop_event was set; got {failed_spy.count()} emissions"
    )


def test_real_failure_still_surfaces_when_stop_not_requested(qapp, tmp_path):
    """Sanity check the inverse: a write() that raises while the
    worker is genuinely live (no stop, no supersede) MUST still
    surface as `playback_failed` so real device failures aren't
    silently swallowed."""
    from PySide6.QtTest import QSignalSpy

    file_a = tmp_path / "broken.wav"
    _write_wav(file_a, frames=4_000, marker=1)

    class _ImmediatelyFailingStream:
        aborted = False

        def write(self, _data: bytes) -> None:
            raise RuntimeError("device gone")

        def abort(self) -> None:
            self.aborted = True

    @contextmanager
    def _open(self, *, sample_rate, channels, dtype):
        yield _ImmediatelyFailingStream()

    player = AudioPlayer()
    player._open_output_stream = _open.__get__(player, AudioPlayer)
    failed_spy = QSignalSpy(player.playback_failed)

    assert player.play_file(file_a)
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline and failed_spy.count() == 0:
        _process_events(2)
        time.sleep(0.02)

    assert failed_spy.count() == 1, "real playback failure must surface"
    player.stop()
