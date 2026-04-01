from __future__ import annotations

from array import array
from collections import deque
import logging
import os
from pathlib import Path
import tempfile
import threading
import time
from typing import Any, Callable
import wave


class MicrophoneRecorder:
    def __init__(self, sample_rate: int = 16_000, channels: int = 1) -> None:
        self.sample_rate = sample_rate
        self.channels = channels
        self._frames: list[bytes] = []
        self._pending_segments: deque[list[bytes]] = deque()
        self._pre_roll_frames: deque[tuple[bytes, int]] = deque()
        self._pre_roll_rms: deque[float] = deque()
        self._lock = threading.Lock()
        self._stream: Any | None = None
        self._logger = logging.getLogger(__name__)
        self._status_count = 0
        self._segment_ready_callback: Callable[[], None] | None = None
        self._speech_threshold = 600
        self._silence_threshold = 180
        self._active_speech_threshold = 600.0
        self._active_silence_threshold = 180.0
        self._silence_timeout_frames = int(self.sample_rate * 1.5)
        self._max_turn_frames = int(self.sample_rate * 120.0)
        self._min_speech_frames = int(self.sample_rate * 0.35)
        self._pre_roll_max_frames = int(self.sample_rate * 0.25)
        self._speech_trigger_frames = int(self.sample_rate * 0.18)
        self._post_finalize_ignore_seconds = 0.85
        self._speech_frames = 0
        self._silence_frames = 0
        self._speech_candidate_frames = 0
        self._speech_candidate_peak_rms = 0.0
        self._segment_started = False
        self._pre_roll_frame_total = 0
        self._last_logged_rms = 0.0
        self._idle_peak_rms = 0.0
        self._idle_log_frames = 0
        self._input_suspended = False
        self._ignore_input_until_monotonic = 0.0
        self._ignore_input_reason = ""

    @property
    def is_recording(self) -> bool:
        with self._lock:
            return self._stream is not None

    def start(
        self,
        *,
        segment_ready_callback: Callable[[], None] | None = None,
        silence_timeout_seconds: float = 1.5,
        speech_threshold: int = 600,
        silence_threshold: int = 180,
        max_turn_seconds: float = 120.0,
        min_speech_seconds: float = 0.35,
        pre_roll_seconds: float = 0.25,
        speech_trigger_seconds: float = 0.18,
    ) -> None:
        with self._lock:
            if self._stream is not None:
                raise RuntimeError("Recording is already in progress.")

            import sounddevice as sd

            self._frames = []
            self._pending_segments.clear()
            self._pre_roll_frames.clear()
            self._pre_roll_rms.clear()
            self._status_count = 0
            self._segment_ready_callback = segment_ready_callback
            self._speech_threshold = speech_threshold
            self._silence_threshold = min(silence_threshold, speech_threshold)
            self._active_speech_threshold = float(self._speech_threshold)
            self._active_silence_threshold = float(self._silence_threshold)
            self._silence_timeout_frames = max(1, int(self.sample_rate * silence_timeout_seconds))
            self._max_turn_frames = max(1, int(self.sample_rate * max_turn_seconds))
            self._min_speech_frames = max(1, int(self.sample_rate * min_speech_seconds))
            self._pre_roll_max_frames = max(1, int(self.sample_rate * pre_roll_seconds))
            self._speech_trigger_frames = max(1, int(self.sample_rate * speech_trigger_seconds))
            self._reset_segment_tracking_locked()
            self._stream = sd.RawInputStream(
                samplerate=self.sample_rate,
                channels=self.channels,
                dtype="int16",
                callback=self._handle_audio_chunk,
            )
            self._stream.start()
            self._logger.info(
                "Microphone recording started sample_rate=%s channels=%s silence_timeout_seconds=%s max_turn_seconds=%s speech_threshold=%s silence_threshold=%s",
                self.sample_rate,
                self.channels,
                silence_timeout_seconds,
                max_turn_seconds,
                speech_threshold,
                self._silence_threshold,
            )
        self._log_snapshot("start")

    def stop(self, *, discard: bool = False) -> Path | None:
        with self._lock:
            if self._stream is None:
                raise RuntimeError("Recording is not in progress.")

            stream = self._stream
            self._stream = None
            self._segment_ready_callback = None
            pending_segments = len(self._pending_segments)
            buffered_frames = len(self._frames)
            segment_started = self._segment_started

        stream.stop()
        stream.close()
        self._logger.info(
            "Microphone stream closed discard=%s pending_segments=%s buffered_frames=%s segment_started=%s",
            discard,
            pending_segments,
            buffered_frames,
            segment_started,
        )

        if discard:
            with self._lock:
                self._frames = []
                self._pending_segments.clear()
                self._pre_roll_frames.clear()
                self._pre_roll_rms.clear()
                self._reset_segment_tracking_locked()
            self._logger.info("Microphone recording stopped and discarded active audio")
            self._log_snapshot("stop_discard")
            return None

        frames = self._extract_stop_frames()
        if not frames:
            self._logger.warning("Microphone recording stopped with no audio frames captured")
            raise RuntimeError("No audio was captured from the microphone.")

        return self._write_frames_to_wav(frames)

    def take_pending_segment(self) -> Path | None:
        with self._lock:
            if not self._pending_segments:
                self._logger.info("No pending microphone segment available to extract")
                return None
            frames = self._pending_segments.popleft()
            remaining_segments = len(self._pending_segments)

        self._logger.info(
            "Extracting pending microphone segment frames=%s remaining_segments=%s",
            len(frames),
            remaining_segments,
        )

        return self._write_frames_to_wav(frames)

    def snapshot_active_segment(self) -> tuple[Path, int] | None:
        with self._lock:
            if not self._segment_started or not self._frames:
                return None
            frames = list(self._frames)
            turn_frames = self._speech_frames + self._silence_frames

        self._logger.info(
            "Snapshotting active microphone segment frame_chunks=%s turn_frames=%s speech_frames=%s silence_frames=%s",
            len(frames),
            turn_frames,
            self._speech_frames,
            self._silence_frames,
        )
        return self._write_frames_to_wav(frames), turn_frames

    def force_finalize_active_segment(self, reason: str) -> bool:
        callback_needed = False
        with self._lock:
            if not self._segment_started or not self._frames:
                self._logger.info("Force finalize skipped reason=%s because no active segment exists", reason)
                return False
            callback_needed = self._finalize_segment_locked(reason, None) is not None

        self._logger.info("Force finalized active microphone segment reason=%s callback_needed=%s", reason, callback_needed)
        if callback_needed and self._segment_ready_callback is not None:
            self._segment_ready_callback()
        return callback_needed

    def suspend_input(self) -> None:
        with self._lock:
            self._input_suspended = True
            self._frames = []
            self._pending_segments.clear()
            self._pre_roll_frames.clear()
            self._pre_roll_rms.clear()
            self._reset_segment_tracking_locked()
        self._logger.info("Microphone input suspended while keeping stream active")
        self._log_snapshot("suspend_input")

    def resume_input(self, *, warmup_seconds: float = 0.0, reason: str = "resume_input") -> None:
        with self._lock:
            self._input_suspended = False
            self._frames = []
            self._pending_segments.clear()
            self._pre_roll_frames.clear()
            self._pre_roll_rms.clear()
            self._reset_segment_tracking_locked()
            if warmup_seconds > 0:
                self._set_ignore_window_locked(warmup_seconds, reason=reason)
        self._logger.info(
            "Microphone input resumed on active stream warmup_seconds=%.2f reason=%s",
            warmup_seconds,
            reason,
        )
        self._log_snapshot("resume_input")

    def ignore_input_for(self, seconds: float, *, reason: str) -> None:
        with self._lock:
            self._set_ignore_window_locked(seconds, reason=reason)
        self._log_snapshot("ignore_input")

    def _extract_stop_frames(self) -> list[bytes]:
        with self._lock:
            if self._pending_segments:
                frames = self._pending_segments.popleft()
                source = "pending"
            else:
                frames = list(self._frames)
                source = "active_buffer"
            self._frames = []
            self._pending_segments.clear()
            self._pre_roll_frames.clear()
            self._pre_roll_rms.clear()
            self._reset_segment_tracking_locked()
        self._logger.info("Extracted microphone frames source=%s frame_chunks=%s", source, len(frames))
        return frames

    def _write_frames_to_wav(self, frames: list[bytes]) -> Path:
        fd, raw_path = tempfile.mkstemp(prefix="voiceagent-input-", suffix=".wav")
        os.close(fd)
        Path(raw_path).unlink(missing_ok=True)
        path = Path(raw_path)

        with wave.open(str(path), "wb") as wav_file:
            wav_file.setnchannels(self.channels)
            wav_file.setsampwidth(2)
            wav_file.setframerate(self.sample_rate)
            for frame in frames:
                wav_file.writeframes(frame)

        self._logger.info(
            "Microphone recording saved path=%s frames=%s bytes=%s status_events=%s",
            path,
            len(frames),
            path.stat().st_size,
            self._status_count,
        )
        return path

    def _handle_audio_chunk(self, indata, frames, time_info, status) -> None:
        callback: Callable[[], None] | None = None
        if status:
            self._status_count += 1
            self._logger.warning("Audio callback status=%s", status)
            return

        chunk = bytes(indata)
        rms = self._chunk_rms(chunk)

        with self._lock:
            if self._stream is None:
                return
            if self._input_suspended:
                self._idle_peak_rms = max(self._idle_peak_rms, rms)
                self._idle_log_frames += frames
                if self._idle_log_frames >= self.sample_rate * 2:
                    self._logger.info(
                        "Microphone input suspended peak_rms=%.1f buffered_pre_roll=%s pending_segments=%s",
                        self._idle_peak_rms,
                        len(self._pre_roll_frames),
                        len(self._pending_segments),
                    )
                    self._idle_peak_rms = 0.0
                    self._idle_log_frames = 0
                return
            ignore_remaining_seconds = self._ignore_input_until_monotonic - time.monotonic()
            if ignore_remaining_seconds > 0:
                self._idle_peak_rms = max(self._idle_peak_rms, rms)
                self._idle_log_frames += frames
                self._append_pre_roll_locked(chunk, frames, rms)
                if self._idle_log_frames >= self.sample_rate:
                    self._logger.info(
                        "Microphone ignore window active reason=%s remaining_seconds=%.2f peak_rms=%.1f buffered_pre_roll=%s",
                        self._ignore_input_reason,
                        ignore_remaining_seconds,
                        self._idle_peak_rms,
                        len(self._pre_roll_frames),
                    )
                    self._idle_peak_rms = 0.0
                    self._idle_log_frames = 0
                return

            if self._segment_started:
                self._frames.append(chunk)
                total_turn_frames = self._speech_frames + self._silence_frames
                if rms >= self._active_silence_threshold:
                    if self._silence_frames > 0:
                        self._logger.debug(
                            "Microphone silence interrupted rms=%.1f silence_threshold=%.1f accumulated_silence_frames=%s",
                            rms,
                            self._active_silence_threshold,
                            self._silence_frames,
                        )
                    self._speech_frames += frames
                    self._silence_frames = 0
                else:
                    self._silence_frames += frames
                    if self._silence_frames >= self.sample_rate and (
                        self._silence_frames - frames
                    ) < self.sample_rate:
                        self._logger.info(
                            "Microphone waiting for end-of-turn silence silence_seconds=%.2f silence_threshold=%.1f speech_frames=%s",
                            self._silence_frames / self.sample_rate,
                            self._active_silence_threshold,
                            self._speech_frames,
                        )
                if self._silence_frames >= self._silence_timeout_frames:
                    callback = self._finalize_segment_locked("silence_timeout", callback)
                elif total_turn_frames + frames >= self._max_turn_frames:
                    self._logger.info(
                        "Microphone max turn reached turn_seconds=%.2f max_turn_seconds=%.2f speech_frames=%s silence_frames=%s",
                        (total_turn_frames + frames) / self.sample_rate,
                        self._max_turn_frames / self.sample_rate,
                        self._speech_frames,
                        self._silence_frames,
                    )
                    callback = self._finalize_segment_locked("max_turn", callback)
            else:
                if rms >= self._active_speech_threshold:
                    self._speech_candidate_frames += frames
                    self._speech_candidate_peak_rms = max(self._speech_candidate_peak_rms, rms)
                    self._append_pre_roll_locked(chunk, frames, rms)
                    if self._speech_candidate_frames >= self._speech_trigger_frames:
                        noise_floor = self._estimate_noise_floor_locked()
                        self._segment_started = True
                        self._frames = [data for data, _ in self._pre_roll_frames]
                        self._pre_roll_frames.clear()
                        self._pre_roll_rms.clear()
                        self._pre_roll_frame_total = 0
                        self._speech_frames = self._speech_candidate_frames
                        self._silence_frames = 0
                        self._active_speech_threshold = max(float(self._speech_threshold), noise_floor * 2.5)
                        self._active_silence_threshold = max(float(self._silence_threshold), noise_floor * 1.35)
                        self._logger.info(
                            "Microphone speech detected rms=%.1f threshold=%.1f trigger_frames=%s pre_roll_frames=%s noise_floor=%.1f active_silence_threshold=%.1f",
                            self._speech_candidate_peak_rms,
                            self._active_speech_threshold,
                            self._speech_candidate_frames,
                            len(self._frames),
                            noise_floor,
                            self._active_silence_threshold,
                        )
                        self._speech_candidate_frames = 0
                        self._speech_candidate_peak_rms = 0.0
                else:
                    if self._speech_candidate_frames > 0:
                        self._logger.debug(
                            "Microphone speech start candidate reset candidate_frames=%s peak_rms=%.1f threshold=%.1f",
                            self._speech_candidate_frames,
                            self._speech_candidate_peak_rms,
                            self._active_speech_threshold,
                        )
                        self._speech_candidate_frames = 0
                        self._speech_candidate_peak_rms = 0.0
                    self._idle_peak_rms = max(self._idle_peak_rms, rms)
                    self._idle_log_frames += frames
                    if self._idle_log_frames >= self.sample_rate * 2:
                        self._logger.info(
                            "Microphone waiting for speech peak_rms=%.1f threshold=%s buffered_pre_roll=%s",
                            self._idle_peak_rms,
                            self._active_speech_threshold,
                            len(self._pre_roll_frames),
                        )
                        self._idle_peak_rms = 0.0
                        self._idle_log_frames = 0
                    if rms > self._last_logged_rms:
                        self._last_logged_rms = rms
                    self._append_pre_roll_locked(chunk, frames, rms)

        if callback is not None:
            callback()

    def _append_pre_roll_locked(self, chunk: bytes, frames: int, rms: float) -> None:
        self._pre_roll_frames.append((chunk, frames))
        self._pre_roll_rms.append(rms)
        self._pre_roll_frame_total += frames
        while self._pre_roll_frame_total > self._pre_roll_max_frames and self._pre_roll_frames:
            _, removed_frames = self._pre_roll_frames.popleft()
            self._pre_roll_rms.popleft()
            self._pre_roll_frame_total -= removed_frames

    def _reset_segment_tracking_locked(self) -> None:
        self._speech_frames = 0
        self._silence_frames = 0
        self._segment_started = False
        self._speech_candidate_frames = 0
        self._speech_candidate_peak_rms = 0.0
        self._pre_roll_frame_total = 0
        self._last_logged_rms = 0.0
        self._idle_peak_rms = 0.0
        self._idle_log_frames = 0
        self._active_speech_threshold = float(self._speech_threshold)
        self._active_silence_threshold = float(self._silence_threshold)

    def _chunk_rms(self, chunk: bytes) -> float:
        if not chunk:
            return 0.0

        samples = array("h")
        samples.frombytes(chunk)
        if not samples:
            return 0.0

        total = sum(sample * sample for sample in samples)
        return (total / len(samples)) ** 0.5

    def _finalize_segment_locked(self, reason: str, callback: Callable[[], None] | None) -> Callable[[], None] | None:
        total_turn_frames = self._speech_frames + self._silence_frames
        if total_turn_frames >= self._min_speech_frames and self._speech_frames > 0 and self._frames:
            self._pending_segments.append(list(self._frames))
            self._logger.info(
                "Microphone segment ready queued=%s reason=%s turn_frames=%s speech_frames=%s silence_frames=%s chunk_count=%s",
                len(self._pending_segments),
                reason,
                total_turn_frames,
                self._speech_frames,
                self._silence_frames,
                len(self._frames),
            )
            callback = self._segment_ready_callback
        else:
            self._logger.info(
                "Discarding short microphone segment reason=%s turn_frames=%s speech_frames=%s silence_frames=%s",
                reason,
                total_turn_frames,
                self._speech_frames,
                self._silence_frames,
            )
        self._set_ignore_window_locked(self._post_finalize_ignore_seconds, reason=f"segment_finalized:{reason}")
        self._frames = []
        self._pre_roll_frames.clear()
        self._pre_roll_rms.clear()
        self._reset_segment_tracking_locked()
        return callback

    def _log_snapshot(self, context: str) -> None:
        with self._lock:
            self._logger.info(
                "Microphone snapshot context=%s stream_active=%s input_suspended=%s pending_segments=%s buffered_frames=%s pre_roll_frames=%s speech_frames=%s silence_frames=%s segment_started=%s active_speech_threshold=%.1f active_silence_threshold=%.1f",
                context,
                self._stream is not None,
                self._input_suspended,
                len(self._pending_segments),
                len(self._frames),
                len(self._pre_roll_frames),
                self._speech_frames,
                self._silence_frames,
                self._segment_started,
                self._active_speech_threshold,
                self._active_silence_threshold,
            )

    def _estimate_noise_floor_locked(self) -> float:
        quiet_rms = [value for value in self._pre_roll_rms if value < float(self._speech_threshold)]
        if quiet_rms:
            sorted_rms = sorted(quiet_rms)
        elif self._pre_roll_rms:
            sorted_rms = sorted(self._pre_roll_rms)
        else:
            return float(self._silence_threshold)
        midpoint = len(sorted_rms) // 2
        if len(sorted_rms) % 2:
            return float(sorted_rms[midpoint])
        return float((sorted_rms[midpoint - 1] + sorted_rms[midpoint]) / 2)

    def _set_ignore_window_locked(self, seconds: float, *, reason: str) -> None:
        duration = max(0.0, seconds)
        deadline = time.monotonic() + duration
        if deadline <= self._ignore_input_until_monotonic and self._ignore_input_reason == reason:
            return
        self._ignore_input_until_monotonic = max(self._ignore_input_until_monotonic, deadline)
        self._ignore_input_reason = reason
        self._logger.info(
            "Microphone ignore window scheduled reason=%s duration_seconds=%.2f until_monotonic=%.3f",
            reason,
            duration,
            self._ignore_input_until_monotonic,
        )
