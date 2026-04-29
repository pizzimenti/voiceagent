"""Behavior-preservation harness for ``MicrophoneRecorder._handle_audio_chunk``.

The audio callback drives four overlapping state machines: idle/pre-roll
buffering, speech-candidate accrual, active-segment tracking with
mid-turn silence interruptions, and finalization on either silence
timeout or max-turn timeout.

These tests synthesize ``int16`` PCM chunks (loud or silent) and feed
them to the callback directly, then assert on the observable outputs:

- ``take_pending_segment()`` returning a finalized WAV.
- The ``segment_ready_callback`` being invoked exactly when expected.
- Internal segment-tracking state matching expectations after each
  transition (these are what the orchestrator and helpers preserve).

No real audio device is touched and no Qt event loop is involved.
"""

from __future__ import annotations

import struct
import sys
import time
from pathlib import Path
from typing import List

# Resolve the in-tree src/ before any installed copy.
_HERE = Path(__file__).resolve().parent
_SRC = _HERE.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import pytest

from voiceagent.services.audio import MicrophoneRecorder


# --- helpers -------------------------------------------------------------


SAMPLE_RATE = 16_000


def _silent_chunk(frames: int) -> bytes:
    return struct.pack("<h", 0) * frames


def _loud_chunk(frames: int, amplitude: int = 8000) -> bytes:
    """Build an alternating ``+amp / -amp`` square wave so RMS == amplitude."""

    samples = []
    for i in range(frames):
        samples.append(amplitude if (i & 1) == 0 else -amplitude)
    return struct.pack(f"<{frames}h", *samples)


class _StubStream:
    """Truthy placeholder for ``self._stream`` so the callback proceeds.

    The real callback short-circuits when ``self._stream is None``;
    we want it to run, but never actually start sounddevice.
    """

    def start(self) -> None:  # pragma: no cover - never invoked here
        pass

    def stop(self) -> None:  # pragma: no cover
        pass

    def close(self) -> None:  # pragma: no cover
        pass


@pytest.fixture
def recorder():
    """Recorder configured with the default ``start()`` knobs but no real stream.

    We bypass ``start()`` entirely (it would import sounddevice and open
    a device). Instead we mirror the configuration ``start()`` would
    perform with default arguments and install a stub stream so the
    callback's ``self._stream is None`` guard does not trip.
    """

    rec = MicrophoneRecorder(sample_rate=SAMPLE_RATE, channels=1)
    rec._speech_threshold = 480
    rec._silence_threshold = 180
    rec._active_speech_threshold = 480.0
    rec._active_silence_threshold = 180.0
    rec._silence_timeout_frames = int(SAMPLE_RATE * 1.5)
    rec._max_turn_frames = int(SAMPLE_RATE * 120.0)
    rec._min_speech_frames = int(SAMPLE_RATE * 0.35)
    rec._pre_roll_max_frames = int(SAMPLE_RATE * 0.25)
    rec._speech_trigger_frames = int(SAMPLE_RATE * 0.18)
    rec._reset_segment_tracking_locked()
    rec._stream = _StubStream()
    return rec


def _feed(recorder: MicrophoneRecorder, chunk: bytes, frames: int) -> None:
    recorder._handle_audio_chunk(chunk, frames, None, None)


# --- tests: idle / pre-roll buffering -----------------------------------


def test_idle_silent_chunk_buffers_pre_roll_without_starting_segment(recorder):
    chunk_frames = 800  # 50 ms at 16 kHz
    _feed(recorder, _silent_chunk(chunk_frames), chunk_frames)

    assert recorder._segment_started is False
    assert len(recorder._pre_roll_frames) == 1
    assert recorder._pre_roll_frame_total == chunk_frames
    assert recorder._speech_candidate_frames == 0
    assert recorder._pending_segments == recorder._pending_segments  # exists, empty
    assert len(recorder._pending_segments) == 0


def test_idle_pre_roll_evicts_oldest_when_capacity_exceeded(recorder):
    # 0.25 s pre-roll budget at 16 kHz = 4_000 frames. Feed 5x 1_000-frame
    # silent chunks; the deque must hold at most ~4_000 worth of frames.
    chunk_frames = 1_000
    for _ in range(5):
        _feed(recorder, _silent_chunk(chunk_frames), chunk_frames)

    assert recorder._pre_roll_frame_total <= recorder._pre_roll_max_frames
    # We fed 5 chunks; oldest was evicted, so 4 remain.
    assert len(recorder._pre_roll_frames) == 4


# --- tests: speech-candidate accrual ------------------------------------


def test_loud_chunk_below_trigger_only_accrues_candidate(recorder):
    # speech_trigger is 0.18 s = 2880 frames. Feed a single 1000-frame
    # loud chunk: candidate accrues but no segment yet.
    chunk_frames = 1_000
    _feed(recorder, _loud_chunk(chunk_frames), chunk_frames)

    assert recorder._segment_started is False
    assert recorder._speech_candidate_frames == chunk_frames
    assert recorder._speech_candidate_peak_rms > 0.0


def test_silent_chunk_resets_candidate(recorder):
    chunk_frames = 1_000
    _feed(recorder, _loud_chunk(chunk_frames), chunk_frames)
    assert recorder._speech_candidate_frames == chunk_frames

    _feed(recorder, _silent_chunk(chunk_frames), chunk_frames)
    assert recorder._speech_candidate_frames == 0
    assert recorder._speech_candidate_peak_rms == 0.0
    assert recorder._segment_started is False


# --- tests: candidate -> active transition ------------------------------


def test_candidate_above_trigger_starts_active_segment(recorder):
    # Two 1500-frame loud chunks = 3000 frames >= trigger 2880; the
    # second chunk crosses the line.
    chunk_frames = 1_500
    _feed(recorder, _loud_chunk(chunk_frames), chunk_frames)
    assert recorder._segment_started is False

    _feed(recorder, _loud_chunk(chunk_frames), chunk_frames)
    assert recorder._segment_started is True
    # Once started, the pre-roll deque was hoisted into _frames and
    # cleared.
    assert len(recorder._pre_roll_frames) == 0
    assert recorder._pre_roll_frame_total == 0
    assert len(recorder._frames) >= 1
    # Speech frames seeded from candidate accrual.
    assert recorder._speech_frames > 0
    assert recorder._silence_frames == 0


# --- tests: active-segment steady state ---------------------------------


def test_active_segment_appends_chunks_and_resets_silence(recorder):
    # Seed a quiet pre-roll so the noise-floor estimator stays low and
    # the post-trigger active_silence_threshold remains around 180-ish.
    # Without this, a pre-roll of all-loud chunks scales the silence
    # threshold above the loud-chunk RMS and the dynamic comparison
    # flips.
    _feed(recorder, _silent_chunk(800), 800)
    _feed(recorder, _silent_chunk(800), 800)

    chunk_frames = 1_500
    _feed(recorder, _loud_chunk(chunk_frames), chunk_frames)
    _feed(recorder, _loud_chunk(chunk_frames), chunk_frames)
    assert recorder._segment_started is True
    speech_after_start = recorder._speech_frames

    # A subsequent loud chunk extends speech_frames and keeps silence at 0.
    _feed(recorder, _loud_chunk(chunk_frames), chunk_frames)
    assert recorder._speech_frames > speech_after_start
    assert recorder._silence_frames == 0


def test_active_segment_brief_silence_then_speech_resets_silence(recorder):
    _feed(recorder, _silent_chunk(800), 800)
    _feed(recorder, _silent_chunk(800), 800)

    chunk_frames = 1_500
    _feed(recorder, _loud_chunk(chunk_frames), chunk_frames)
    _feed(recorder, _loud_chunk(chunk_frames), chunk_frames)
    assert recorder._segment_started

    _feed(recorder, _silent_chunk(chunk_frames), chunk_frames)
    assert recorder._silence_frames == chunk_frames
    assert recorder._segment_started

    _feed(recorder, _loud_chunk(chunk_frames), chunk_frames)
    # Silence interruption clears the silence counter.
    assert recorder._silence_frames == 0


# --- tests: finalization on silence timeout -----------------------------


def test_active_segment_finalizes_on_silence_timeout(tmp_path, recorder):
    callbacks: List[None] = []
    recorder._segment_ready_callback = lambda: callbacks.append(None)

    chunk_frames = 2_000
    # Build a long enough loud burst that satisfies _min_speech_frames
    # (5_600 frames). Three 2_000-frame chunks = 6_000 frames of speech.
    for _ in range(3):
        _feed(recorder, _loud_chunk(chunk_frames), chunk_frames)
    assert recorder._segment_started is True

    # silence_timeout is 1.5 s = 24_000 frames. Feed enough silent
    # chunks to cross it: 12 x 2_000 = 24_000.
    for _ in range(12):
        _feed(recorder, _silent_chunk(chunk_frames), chunk_frames)

    # Callback should have fired exactly once on finalization.
    assert len(callbacks) == 1
    # Segment should be queued.
    assert len(recorder._pending_segments) == 1
    # State reset.
    assert recorder._segment_started is False
    assert recorder._speech_frames == 0
    assert recorder._silence_frames == 0

    # take_pending_segment yields a real WAV path.
    path = recorder.take_pending_segment()
    assert path is not None
    assert path.exists()
    path.unlink(missing_ok=True)


def test_short_segment_below_min_speech_is_discarded_on_silence(recorder):
    # Bump min_speech_frames above the total turn (speech accrual plus
    # the full silence-timeout window); _finalize_segment_locked
    # branches on `total_turn_frames >= min_speech_frames`, so we need
    # min above what we will actually accrue.
    recorder._min_speech_frames = 60_000

    callbacks: List[None] = []
    recorder._segment_ready_callback = lambda: callbacks.append(None)

    chunk_frames = 1_500
    _feed(recorder, _loud_chunk(chunk_frames), chunk_frames)
    _feed(recorder, _loud_chunk(chunk_frames), chunk_frames)
    assert recorder._segment_started

    chunk_frames = 2_000
    for _ in range(12):
        _feed(recorder, _silent_chunk(chunk_frames), chunk_frames)

    # No callback because the segment was too short.
    assert callbacks == []
    assert len(recorder._pending_segments) == 0
    assert recorder._segment_started is False


# --- tests: finalization on max-turn timeout ----------------------------


def test_active_segment_finalizes_on_max_turn(recorder):
    # Drop max turn to something feasible to drive without thousands of
    # chunks. The orchestrator must finalize once total_turn_frames +
    # frames >= max_turn_frames.
    recorder._max_turn_frames = int(SAMPLE_RATE * 0.5)  # 8_000 frames
    recorder._min_speech_frames = 1  # ensure not discarded as too short

    callbacks: List[None] = []
    recorder._segment_ready_callback = lambda: callbacks.append(None)

    chunk_frames = 2_000
    # Drive past the trigger first.
    _feed(recorder, _loud_chunk(1_500), 1_500)
    _feed(recorder, _loud_chunk(1_500), 1_500)
    assert recorder._segment_started

    # Now keep speaking until max-turn fires.
    for _ in range(6):
        _feed(recorder, _loud_chunk(chunk_frames), chunk_frames)
        if not recorder._segment_started:
            break

    assert len(callbacks) == 1
    assert len(recorder._pending_segments) == 1
    assert recorder._segment_started is False


# --- tests: input gating --------------------------------------------------


def test_suspended_input_is_dropped_silently(recorder):
    recorder._input_suspended = True

    chunk_frames = 1_000
    _feed(recorder, _loud_chunk(chunk_frames), chunk_frames)

    # No segment, no candidate accrual, no pre-roll.
    assert recorder._segment_started is False
    assert recorder._speech_candidate_frames == 0
    assert len(recorder._pre_roll_frames) == 0
    assert recorder._current_input_level == 0.0


def test_ignore_window_drops_chunks_but_keeps_pre_roll(recorder):
    # Set a 5 s ignore window starting now.
    recorder._ignore_input_until_monotonic = time.monotonic() + 5.0
    recorder._ignore_input_reason = "test"

    chunk_frames = 1_000
    _feed(recorder, _loud_chunk(chunk_frames), chunk_frames)

    # current_input_level forced to zero.
    assert recorder._current_input_level == 0.0
    # No candidate accrual.
    assert recorder._speech_candidate_frames == 0
    # But pre-roll DID accrue (this is the active difference between
    # suspended-input and ignore-window).
    assert len(recorder._pre_roll_frames) == 1


def test_callback_status_event_is_counted_and_returns_early(recorder):
    chunk_frames = 1_000
    # Pass a truthy "status" object; the callback must increment the
    # counter and return without touching state.
    recorder._handle_audio_chunk(_loud_chunk(chunk_frames), chunk_frames, None, "underflow")

    assert recorder._status_count == 1
    assert recorder._segment_started is False
    assert len(recorder._pre_roll_frames) == 0


def test_no_stream_returns_without_state_change(recorder):
    recorder._stream = None
    chunk_frames = 1_000
    _feed(recorder, _loud_chunk(chunk_frames), chunk_frames)

    assert recorder._segment_started is False
    assert recorder._speech_candidate_frames == 0
    assert len(recorder._pre_roll_frames) == 0
