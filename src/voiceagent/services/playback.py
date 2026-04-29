from __future__ import annotations

from array import array
from contextlib import contextmanager
import logging
import math
from pathlib import Path
import threading
import time
from typing import Any, Iterator
import wave

from PySide6.QtCore import QObject, Signal


# Bounded join on previous worker when starting a new playback or stopping.
# The old worker's stop_event has already been set; we wait briefly for a
# clean exit, then abandon. The audio device will close on next write by
# the now-zombie worker when its `with` block unwinds.
_JOIN_TIMEOUT_SECONDS = 0.25


class AudioPlayer(QObject):
    playback_started = Signal(str)
    playback_finished = Signal(str)
    playback_failed = Signal(str, str)
    playback_state_changed = Signal(str, str)
    muted_changed = Signal(bool)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._logger = logging.getLogger(__name__)

        # Per-playback identity. `_generation` is the source of truth for
        # "which playback is current". Each `play_file` mints a new
        # generation and a fresh stop event owned by the corresponding
        # worker; the instance never shares a stop event across workers.
        #
        # `_lock` guards writes to `_generation`, `_current_stop_event`,
        # `_thread`, and `_current_file` so that a new `play_file` can
        # supersede an in-flight one without torn state.
        self._lock = threading.Lock()
        self._generation: int = 0
        self._current_stop_event: threading.Event | None = None
        self._thread: threading.Thread | None = None
        self._current_file: Path | None = None

        self._stream: Any | None = None
        self._pause_condition = threading.Condition()
        self._paused = False
        self._muted = False

        self._level_lock = threading.Lock()
        self._current_output_level = 0.0
        self._recent_output_chunk = b""
        self._recent_output_timestamp = 0.0
        self._recent_output_sample_rate = 0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def play_file(self, path: Path) -> bool:
        self._logger.info(
            "Starting audio playback path=%s exists=%s bytes=%s currently_playing=%s currently_paused=%s",
            path,
            path.exists(),
            path.stat().st_size if path.exists() else 0,
            self.is_playing,
            self.is_paused,
        )

        # Signal any in-flight worker to stop, then swap in a brand new
        # identity for this call. We hold `_lock` while swapping so a
        # concurrent `stop()` or `play_file()` observes a consistent view.
        previous_thread: threading.Thread | None
        previous_stop_event: threading.Event | None
        with self._lock:
            previous_thread = self._thread
            previous_stop_event = self._current_stop_event

            self._generation += 1
            gen = self._generation
            stop_event = threading.Event()
            self._current_stop_event = stop_event
            self._current_file = path
            self._paused = False

        if previous_stop_event is not None:
            previous_stop_event.set()
        # Wake a paused prior worker so it can observe its stop event.
        with self._pause_condition:
            self._pause_condition.notify_all()

        if self._stream is not None:
            try:
                self._stream.abort()
            except Exception:
                pass

        # Bounded join on the prior worker. If the old worker is stuck,
        # log and abandon rather than blocking the caller; the stop event
        # has been set and the `with` block will close the output stream
        # on the next write attempt.
        if previous_thread is not None and previous_thread.is_alive():
            previous_thread.join(timeout=_JOIN_TIMEOUT_SECONDS)
            if previous_thread.is_alive():
                self._logger.warning(
                    "Previous playback worker did not exit within %.3fs; abandoning",
                    _JOIN_TIMEOUT_SECONDS,
                )

        thread = threading.Thread(
            target=self._playback_worker,
            args=(gen, path, stop_event),
            daemon=True,
        )
        with self._lock:
            # A concurrent `play_file` may have already bumped the
            # generation past `gen` while we were joining. If so, we lost
            # the race; discard this attempt entirely. Unlink the WAV
            # we accepted before bailing — synthesize() handed it off to
            # us, and the worker that would have unlinked it is never
            # going to start.
            if self._generation != gen:
                self._logger.info(
                    "Abandoning start of playback gen=%s; superseded by gen=%s",
                    gen,
                    self._generation,
                )
                try:
                    Path(path).unlink(missing_ok=True)
                except OSError as exc:
                    self._logger.warning(
                        "Failed to unlink superseded playback temp path=%s: %s",
                        path,
                        exc,
                    )
                return False
            self._thread = thread
        thread.start()
        return True

    def pause(self) -> None:
        with self._lock:
            current_file = self._current_file
        if current_file is None or self._paused:
            return
        self._logger.info("Pausing audio playback path=%s", current_file)
        with self._pause_condition:
            self._paused = True
        self.playback_state_changed.emit(str(current_file), "PausedState")

    def resume(self) -> None:
        with self._lock:
            current_file = self._current_file
        if current_file is None or not self._paused:
            return
        self._logger.info("Resuming audio playback path=%s", current_file)
        with self._pause_condition:
            self._paused = False
            self._pause_condition.notify_all()
        self.playback_state_changed.emit(str(current_file), "PlayingState")

    def stop(self) -> None:
        with self._lock:
            previous_thread = self._thread
            previous_stop_event = self._current_stop_event
            current_file = self._current_file
            thread_alive = previous_thread.is_alive() if previous_thread is not None else False

        if current_file is None and previous_thread is None:
            return

        self._logger.info(
            "Stopping audio playback path=%s thread_alive=%s paused=%s",
            current_file,
            thread_alive,
            self._paused,
        )

        if previous_stop_event is not None:
            previous_stop_event.set()
        with self._pause_condition:
            self._paused = False
            self._pause_condition.notify_all()

        if self._stream is not None:
            try:
                self._stream.abort()
            except Exception:
                pass

        if previous_thread is not None and previous_thread.is_alive():
            previous_thread.join(timeout=_JOIN_TIMEOUT_SECONDS)
            if previous_thread.is_alive():
                self._logger.warning(
                    "Playback worker did not exit within %.3fs during stop; abandoning",
                    _JOIN_TIMEOUT_SECONDS,
                )

        with self._lock:
            # Only clear references if they still point at what we just
            # stopped. A concurrent `play_file` may have installed new
            # ones; we must not clobber those.
            if self._thread is previous_thread:
                self._thread = None
            if self._current_stop_event is previous_stop_event:
                self._current_stop_event = None

        self._set_output_level(0.0)
        self._set_recent_output(b"", 0)
        self._cleanup_current_file()

    @property
    def is_playing(self) -> bool:
        with self._lock:
            thread = self._thread
            current_file = self._current_file
        return (
            current_file is not None
            and thread is not None
            and thread.is_alive()
            and not self._paused
        )

    @property
    def is_paused(self) -> bool:
        with self._lock:
            current_file = self._current_file
        return current_file is not None and self._paused

    @property
    def is_muted(self) -> bool:
        return self._muted

    @property
    def current_output_level(self) -> float:
        with self._level_lock:
            return self._current_output_level

    def recent_output_chunk(self) -> tuple[bytes, int, float]:
        with self._level_lock:
            return (
                self._recent_output_chunk,
                self._recent_output_sample_rate,
                self._recent_output_timestamp,
            )

    def set_muted(self, muted: bool) -> None:
        if self._muted == muted:
            return
        self._muted = muted
        self._logger.info("Audio output muted=%s", muted)
        self.muted_changed.emit(muted)

    # ------------------------------------------------------------------
    # Worker
    # ------------------------------------------------------------------

    def _playback_worker(
        self,
        gen: int,
        path: Path,
        stop_event: threading.Event,
    ) -> None:
        try:
            chunk_writes = 0
            muted_chunk_sleeps = 0
            with wave.open(str(path), "rb") as wav_file:
                channels = wav_file.getnchannels()
                sample_rate = wav_file.getframerate()
                sample_width = wav_file.getsampwidth()
                dtype = self._dtype_for_sample_width(sample_width)
                self._logger.info(
                    "Audio playback stream open gen=%s path=%s channels=%s sample_rate=%s sample_width=%s",
                    gen,
                    path,
                    channels,
                    sample_rate,
                    sample_width,
                )
                with self._open_output_stream(
                    sample_rate=sample_rate,
                    channels=channels,
                    dtype=dtype,
                ) as stream:
                    # Only publish this worker as the active stream if we
                    # are still the current generation. Otherwise the
                    # caller that superseded us already owns the seat.
                    if self._is_current_generation(gen):
                        self._stream = stream
                        self._emit_state_changed(gen, str(path), "PlayingState")
                        self._emit_started(gen, str(path))
                    while not stop_event.is_set():
                        with self._pause_condition:
                            while self._paused and not stop_event.is_set():
                                self._pause_condition.wait(timeout=0.1)

                        if stop_event.is_set():
                            break

                        data = wav_file.readframes(4096)
                        if not data:
                            break
                        # Only drive shared output-level / recent-chunk
                        # state while we are the current generation; a
                        # stale worker must not bleed meter updates into
                        # a fresher playback.
                        if self._is_current_generation(gen):
                            self._set_output_level(
                                self._normalize_level(self._chunk_rms(data))
                            )
                            self._set_recent_output(data, sample_rate)
                        if self._muted:
                            frame_count = len(data) // max(1, channels * sample_width)
                            time.sleep(frame_count / sample_rate)
                            muted_chunk_sleeps += 1
                            continue
                        stream.write(data)
                        chunk_writes += 1
        except Exception as exc:
            self._logger.exception(
                "Audio playback failed gen=%s path=%s", gen, path
            )
            self._emit_failed(gen, str(path), str(exc) or "Audio playback failed.")
        else:
            if not stop_event.is_set():
                self._logger.info(
                    "Audio playback finished gen=%s path=%s chunk_writes=%s muted_chunk_sleeps=%s",
                    gen,
                    path,
                    chunk_writes,
                    muted_chunk_sleeps,
                )
                self._emit_finished(gen, str(path))
        finally:
            self._logger.info(
                "Audio playback worker finalizing gen=%s path=%s stop_event=%s paused=%s current=%s",
                gen,
                path,
                stop_event.is_set(),
                self._paused,
                self._is_current_generation(gen),
            )
            if self._is_current_generation(gen):
                self._stream = None
                self._set_output_level(0.0)
                self._set_recent_output(b"", 0)
                self._cleanup_current_file_if(path)
                self._paused = False
            else:
                # Stale worker: a newer play_file() has superseded us. The
                # new generation owns `_current_file`, but `path` is the
                # file *this* worker was playing — nobody else will unlink
                # it, so we must. Touch nothing else (controller-visible
                # state belongs to the live generation).
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    self._logger.exception(
                        "Failed to unlink stale playback file path=%s", path
                    )
            # Whether or not we were current, drop the reference to this
            # thread if it's still pointing at us, so `is_playing`
            # eventually reports False.
            with self._lock:
                if self._generation == gen and self._thread is threading.current_thread():
                    self._thread = None
                    self._current_stop_event = None

    # ------------------------------------------------------------------
    # Generation-aware signal emission
    # ------------------------------------------------------------------
    #
    # These helpers filter stale emissions at the source: if a worker's
    # generation no longer matches the current one, the corresponding
    # public signal is silently dropped. That keeps the controller's
    # per-signal slots ignorant of generational bookkeeping.

    def _is_current_generation(self, gen: int) -> bool:
        with self._lock:
            return self._generation == gen

    def _emit_started(self, gen: int, path: str) -> None:
        if not self._is_current_generation(gen):
            self._logger.info("Dropping stale playback_started gen=%s path=%s", gen, path)
            return
        self.playback_started.emit(path)

    def _emit_finished(self, gen: int, path: str) -> None:
        if not self._is_current_generation(gen):
            self._logger.info("Dropping stale playback_finished gen=%s path=%s", gen, path)
            return
        self.playback_finished.emit(path)

    def _emit_failed(self, gen: int, path: str, message: str) -> None:
        if not self._is_current_generation(gen):
            self._logger.info(
                "Dropping stale playback_failed gen=%s path=%s message=%s",
                gen,
                path,
                message,
            )
            return
        self.playback_failed.emit(path, message)

    def _emit_state_changed(self, gen: int, path: str, state: str) -> None:
        if not self._is_current_generation(gen):
            self._logger.info(
                "Dropping stale playback_state_changed gen=%s path=%s state=%s",
                gen,
                path,
                state,
            )
            return
        self.playback_state_changed.emit(path, state)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    # Test seam: tests replace `_open_output_stream` with a stub context
    # manager. Named leading-underscore to signal this is not public API.
    @contextmanager
    def _open_output_stream(
        self,
        *,
        sample_rate: int,
        channels: int,
        dtype: str,
    ) -> Iterator[Any]:
        import sounddevice as sd

        stream = sd.RawOutputStream(
            samplerate=sample_rate,
            channels=channels,
            dtype=dtype,
        )
        with stream as open_stream:
            yield open_stream

    def _cleanup_current_file(self) -> None:
        with self._lock:
            current_file = self._current_file
            self._current_file = None
        if current_file is None:
            return
        self._logger.info("Cleaning up playback file path=%s", current_file)
        current_file.unlink(missing_ok=True)

    def _cleanup_current_file_if(self, path: Path) -> None:
        """Clean up `_current_file` only if it still points at `path`.

        Called from worker finalization: a superseding playback may have
        installed a new `_current_file`, and we must not unlink that.
        """
        with self._lock:
            if self._current_file != path:
                return
            self._current_file = None
        self._logger.info("Cleaning up playback file path=%s", path)
        path.unlink(missing_ok=True)

    def _dtype_for_sample_width(self, sample_width: int) -> str:
        if sample_width == 1:
            return "uint8"
        if sample_width == 2:
            return "int16"
        if sample_width == 3:
            return "int24"
        if sample_width == 4:
            return "int32"
        raise RuntimeError(f"Unsupported WAV sample width: {sample_width}")

    def _chunk_rms(self, chunk: bytes) -> float:
        if not chunk:
            return 0.0
        samples = array("h")
        samples.frombytes(chunk)
        if not samples:
            return 0.0
        total = sum(sample * sample for sample in samples)
        return (total / len(samples)) ** 0.5

    def _normalize_level(self, rms: float) -> float:
        if rms <= 0:
            return 0.0
        ceiling = 8000.0
        return min(1.0, math.log10(1.0 + rms) / math.log10(1.0 + ceiling))

    def _set_output_level(self, level: float) -> None:
        with self._level_lock:
            self._current_output_level = max(0.0, min(1.0, level))

    def _set_recent_output(self, chunk: bytes, sample_rate: int) -> None:
        with self._level_lock:
            self._recent_output_chunk = chunk
            self._recent_output_sample_rate = sample_rate
            self._recent_output_timestamp = time.monotonic() if chunk else 0.0
