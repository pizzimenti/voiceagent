from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
import logging
from pathlib import Path
import time
from typing import Callable

from PySide6.QtCore import QObject, Signal, Slot, QTimer

from voiceagent.backends import SpeechToTextBackend, TextToSpeechBackend
from voiceagent.logging_utils import log_ui_timing
from voiceagent.models import AppState, PipelineResult
from voiceagent.services.audio import MicrophoneRecorder
from voiceagent.services.chat import LmStudioClient
from voiceagent.services.playback import AudioPlayer


class VoiceController(QObject):
    """Orchestrates the voice-pipeline lifecycle.

    Owns the IDLE / RECORDING / TRANSCRIBING / THINKING / SYNTHESIZING /
    SPEAKING state machine and the executor that runs the
    record → transcribe → chat → TTS → playback pipeline off the GUI
    thread. A second, separate `partial_executor` runs in-progress
    transcription probes that drive the live-transcript draft bubble.
    Streaming chat output flows through the `chat_*_chunk` signals
    below so the conversation coordinator can incrementally update
    the assistant draft as tokens arrive.
    """

    state_changed = Signal(str)
    status_changed = Signal(str)
    connection_changed = Signal(bool)
    live_transcript_changed = Signal(str)
    transcript_changed = Signal(str)
    response_changed = Signal(str)
    error_changed = Signal(str)
    segment_ready = Signal()
    pipeline_completed = Signal(object)
    pipeline_failed = Signal(str)
    pipeline_state_changed = Signal(str, str)
    partial_transcription_ready = Signal(object)

    # v0.10.0 streaming chat. The chat client now invokes per-chunk
    # callbacks from the executor thread; we forward each as a Qt
    # signal so the GUI-thread coordinator (and QML bindings) get
    # incremental updates marshalled via AutoConnection.
    chat_thinking_chunk = Signal(str)
    chat_content_chunk = Signal(str)
    chat_usage_changed = Signal(int, int)  # prompt_tokens, completion_tokens

    # Internal worker-thread -> owner-thread bridges. Emitted from
    # `add_done_callback` callbacks that may run on executor threads OR
    # synchronously on the owner thread (when add_done_callback registers
    # against an already-done future). Connected with AutoConnection so
    # cross-thread emits queue (slot runs on the owner thread, sole
    # writer) and same-thread emits run inline (correct count is in
    # place before any subsequent same-thread signal emit reads it).
    _pipeline_count_delta = Signal(int)
    _partial_inflight_changed = Signal(bool)

    _PARTIAL_CHECK_INTERVAL_MS = 350
    _PARTIAL_MIN_SPEECH_SECONDS = 0.35
    _PARTIAL_STALE_SECONDS = 2.0
    _PARTIAL_SKIP_LOG_INTERVAL_SECONDS = 2.5
    _POST_FINALIZE_INPUT_HOLDOFF_SECONDS = 0.9
    _POST_PLAYBACK_COOLDOWN_SECONDS = 0.6
    _POST_PLAYBACK_INPUT_WARMUP_SECONDS = 1.0

    def __init__(
        self,
        recorder: MicrophoneRecorder,
        transcriber: SpeechToTextBackend,
        chat_client: LmStudioClient,
        tts_service: TextToSpeechBackend,
        player: AudioPlayer,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.recorder = recorder
        self.transcriber = transcriber
        self.chat_client = chat_client
        self.tts_service = tts_service
        self.player = player
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="voiceagent")
        self.partial_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="voiceagent-partial")
        # v0.11 multi-turn history. Callers wire this to a closure that
        # snapshots the current `ConversationModel` state via
        # `to_openai_messages(...)` — invoked on the GUI thread before
        # we hand control to the executor, so the snapshot is consistent
        # with the visible transcript at submit time. None during early
        # construction (window has not wired it yet) and in tests that
        # exercise the controller without a window: in both cases we
        # fall through to a "system prompt + current user turn" payload
        # built off `chat_client.system_prompt`, preserving v0.10
        # single-turn behavior as the safety net.
        self.chat_history_provider: Callable[[], list[dict[str, str]]] | None = None
        # Conversation-history cap used by the provider closure window
        # installs above. Default mirrors `AppConfig.max_history_turns`
        # so tests that build the controller in isolation have a sane
        # value without needing to plumb config through.
        self.max_history_turns: int = 20
        self.state = AppState.IDLE
        self._logger = logging.getLogger(__name__)
        self._voice_connection_enabled = False
        # INVARIANT: only the owner thread writes `_active_pipeline_count`.
        # Executor-thread callbacks emit `_pipeline_count_delta` and
        # `_on_pipeline_count_delta` is the sole writer (AutoConnection
        # routes to it directly when same-thread, queues when cross-thread).
        self._active_pipeline_count = 0
        self._playing_response = False
        self._aux_playback_active = False
        # INVARIANT: only the owner thread writes `_partial_inflight`.
        # Executor-thread callbacks emit `_partial_inflight_changed` and
        # `_on_partial_inflight_changed` is the sole writer (AutoConnection;
        # see the matching comment above on `_active_pipeline_count`).
        self._partial_inflight = False
        self._live_transcript = ""
        self._partial_last_text = ""
        self._partial_last_change_monotonic = 0.0
        self._partial_last_speech_frames = 0
        self._partial_generation = 0
        self._partial_last_skip_reason = ""
        self._partial_last_skip_log_monotonic = 0.0
        self._ignore_segment_until_monotonic = 0.0
        self._resume_input_generation = 0
        self._partial_timer = QTimer(self)
        self._partial_timer.setInterval(self._PARTIAL_CHECK_INTERVAL_MS)
        self._partial_timer.timeout.connect(self._schedule_partial_check)

        self.pipeline_completed.connect(self._apply_pipeline_result)
        self.pipeline_failed.connect(self._apply_pipeline_error)
        self.pipeline_state_changed.connect(self._handle_pipeline_state_changed)
        self.partial_transcription_ready.connect(self._handle_partial_transcription_ready)
        self.segment_ready.connect(self._handle_segment_ready)
        self.player.playback_started.connect(self._handle_playback_started)
        self.player.playback_finished.connect(self._handle_playback_finished)
        self.player.playback_failed.connect(self._handle_playback_failed)
        # Worker-thread -> owner-thread bridges. Use AutoConnection (default):
        # cross-thread emits queue (sole writer is still the owner thread),
        # same-thread emits run inline. Same-thread is the corner case where
        # `Future.add_done_callback` is registered against an already-done
        # future — Python invokes the callback synchronously on the caller's
        # thread (the owner thread for our submit/add-done pattern). With
        # explicit QueuedConnection, that synchronous owner-thread callback
        # would *post* the decrement instead of applying it, and the
        # subsequent `pipeline_completed` emit (also synchronous) would read
        # a stale count and skip the resume/state transition.
        self._pipeline_count_delta.connect(self._on_pipeline_count_delta)
        self._partial_inflight_changed.connect(self._on_partial_inflight_changed)

        self._apply_state(AppState.IDLE.value, "Ready")

    @property
    def voice_connection_enabled(self) -> bool:
        return self._voice_connection_enabled

    def start_recording(self) -> None:
        if self._voice_connection_enabled:
            self._logger.info("Voice connection start ignored because it is already enabled")
            return

        self._logger.info(
            "Voice connection enabling active_pipeline_count=%s playing_response=%s",
            self._active_pipeline_count,
            self._playing_response,
        )
        self.error_changed.emit("")
        self._voice_connection_enabled = True
        self.connection_changed.emit(True)
        self._reset_partial_tracking()
        if not self._start_listening():
            return
        self.recorder.resume_input(reason="voice_connection_start")
        self._logger.info(
            "Starting partial transcription timer interval_ms=%s",
            self._partial_timer.interval(),
        )
        self._partial_timer.start()
        self._apply_state(AppState.RECORDING.value, "Listening for next turn")

    def stop_recording(self) -> None:
        if not self._voice_connection_enabled:
            self._logger.info("Voice connection stop ignored because it is already disabled")
            return

        self._logger.info(
            "Voice connection disabling active_pipeline_count=%s playing_response=%s recorder_active=%s",
            self._active_pipeline_count,
            self._playing_response,
            self.recorder.is_recording,
        )
        self._voice_connection_enabled = False
        self.connection_changed.emit(False)
        self._logger.info("Stopping partial transcription timer because voice connection was disabled")
        self._partial_timer.stop()
        self._reset_partial_tracking()
        try:
            if self.recorder.is_recording:
                self.recorder.stop(discard=True)
        except Exception as exc:
            self._logger.exception("Failed to stop microphone stream")
            self.error_changed.emit(str(exc))
        self.player.stop()
        self._playing_response = False
        self._apply_state(AppState.IDLE.value, "Ready")

    def shutdown(self) -> None:
        self._logger.info("Stopping partial transcription timer during shutdown")
        self._partial_timer.stop()
        if self.recorder.is_recording:
            try:
                self.recorder.stop(discard=True)
            except Exception:
                self._logger.exception("Failed to stop microphone stream during shutdown")
        self.executor.shutdown(wait=False, cancel_futures=True)
        self.partial_executor.shutdown(wait=False, cancel_futures=True)

    def _run_pipeline(
        self,
        audio_path: Path,
        history_snapshot: list[dict[str, str]] | None = None,
    ) -> PipelineResult:
        try:
            self._logger.info(
                "Voice pipeline starting path=%s transcriber_loaded=%s tts_enabled=%s",
                audio_path,
                self.transcriber.is_loaded,
                self.tts_service.enabled,
            )
            if not self.transcriber.is_loaded:
                self.pipeline_state_changed.emit(
                    AppState.TRANSCRIBING.value,
                    f"Loading {self.transcriber.backend_name} {self.transcriber.selection_label.lower()}",
                )
                self.transcriber.ensure_loaded()

            self.pipeline_state_changed.emit(AppState.TRANSCRIBING.value, "Transcribing")
            transcript = self.transcriber.transcribe(audio_path)
            if not transcript.strip():
                self._logger.info(
                    "Voice pipeline ending early — empty transcript path=%s",
                    audio_path,
                )
                return PipelineResult(
                    transcript="",
                    response="",
                    tts_audio_path=None,
                )
            self.pipeline_state_changed.emit(AppState.THINKING.value, "Waiting for LM Studio")

            def _on_usage(usage: dict) -> None:
                prompt_tokens = int(usage.get("prompt_tokens", 0) or 0)
                completion_tokens = int(usage.get("completion_tokens", 0) or 0)
                self.chat_usage_changed.emit(prompt_tokens, completion_tokens)

            # Build the full messages list from the GUI-thread snapshot
            # (multi-turn) or fall back to a single-turn payload built
            # off the chat client's system prompt (v0.10 behavior, kept
            # as a safety net for the no-provider case).
            if history_snapshot is None:
                messages: list[dict[str, str]] = []
                if self.chat_client.system_prompt:
                    messages.append(
                        {"role": "system", "content": self.chat_client.system_prompt}
                    )
            else:
                messages = list(history_snapshot)
            messages.append({"role": "user", "content": transcript})

            response = self.chat_client.complete(
                messages,
                on_content_chunk=self.chat_content_chunk.emit,
                on_thinking_chunk=self.chat_thinking_chunk.emit,
                on_usage=_on_usage,
            )

            tts_audio_path = None
            if self.tts_service.enabled:
                self.pipeline_state_changed.emit(AppState.SYNTHESIZING.value, "Generating speech")
                tts_audio_path = self.tts_service.synthesize(response)
            else:
                self._logger.info("TTS skipped because no voice is selected")

            return PipelineResult(
                transcript=transcript,
                response=response,
                tts_audio_path=tts_audio_path,
            )
        except Exception:
            self._logger.exception("Voice pipeline failed path=%s", audio_path)
            raise
        finally:
            audio_path.unlink(missing_ok=True)

    def _handle_pipeline_done(self, future: Future[PipelineResult]) -> None:
        # Runs on the executor thread. MUST NOT mutate `_active_pipeline_count`
        # directly; route the delta through `_pipeline_count_delta` so the
        # queued slot on the owner thread is the sole writer.
        self._pipeline_count_delta.emit(-1)
        try:
            result = future.result()
        except Exception as exc:
            self.pipeline_failed.emit(str(exc))
            return

        self.pipeline_completed.emit(result)

    def _apply_pipeline_result(self, result: PipelineResult) -> None:
        self.error_changed.emit("")
        self._reset_partial_tracking()
        self.transcript_changed.emit(result.transcript)
        self.response_changed.emit(result.response)
        self._logger.info(
            "Pipeline result transcript_chars=%s response_chars=%s has_tts_audio=%s voice_connection_enabled=%s",
            len(result.transcript),
            len(result.response),
            result.tts_audio_path is not None,
            self._voice_connection_enabled,
        )

        if result.tts_audio_path is None or not self._voice_connection_enabled:
            if result.tts_audio_path is not None:
                result.tts_audio_path.unlink(missing_ok=True)
            if self._voice_connection_enabled:
                self._schedule_input_resume_after_cooldown("pipeline_without_tts")
            self._resume_listening_if_possible()
            return

        self._playing_response = True
        self._set_state(AppState.SPEAKING)
        self.status_changed.emit("Playing response")
        self.player.play_file(result.tts_audio_path)

    def handle_aux_playback_started(self, path: str) -> None:
        self._aux_playback_active = True
        self._logger.info(
            "Aux playback started path=%s voice_connection_enabled=%s recorder_active=%s",
            path,
            self._voice_connection_enabled,
            self.recorder.is_recording,
        )
        if not self._voice_connection_enabled or not self.recorder.is_recording:
            return
        self._partial_timer.stop()
        self.recorder.suspend_input()
        if self._active_pipeline_count == 0 and not self._playing_response:
            self._apply_state(AppState.SPEAKING.value, "Playing transcript replay")

    def handle_aux_playback_finished(self, path: str) -> None:
        self._logger.info(
            "Aux playback finished path=%s voice_connection_enabled=%s active_pipeline_count=%s playing_response=%s",
            path,
            self._voice_connection_enabled,
            self._active_pipeline_count,
            self._playing_response,
        )
        self._aux_playback_active = False
        if self._voice_connection_enabled:
            self._schedule_input_resume_after_cooldown("aux_playback_finished")
        self._resume_listening_if_possible()

    def handle_aux_playback_failed(self, path: str, message: str) -> None:
        self._logger.error("Aux playback failed path=%s message=%s", path, message)
        self._aux_playback_active = False
        if self._voice_connection_enabled:
            self._schedule_input_resume_after_cooldown("aux_playback_failed")
        self._resume_listening_if_possible()

    def _apply_pipeline_error(self, message: str) -> None:
        self._logger.error("Pipeline error message=%s", message)
        self._reset_partial_tracking()
        self.error_changed.emit(message)
        if self._voice_connection_enabled:
            self._schedule_input_resume_after_cooldown("pipeline_error")
        self._resume_listening_if_possible()

    def _handle_playback_finished(self, _path: str) -> None:
        self._logger.info("Playback finished voice_connection_enabled=%s", self._voice_connection_enabled)
        self._playing_response = False
        if self._voice_connection_enabled:
            self._schedule_input_resume_after_cooldown("playback_finished")
        self._resume_listening_if_possible()

    def _handle_playback_failed(self, _path: str, message: str) -> None:
        self._logger.error("Playback failed message=%s voice_connection_enabled=%s", message, self._voice_connection_enabled)
        self._playing_response = False
        self.error_changed.emit(message)
        if self._voice_connection_enabled:
            self._schedule_input_resume_after_cooldown("playback_failed")
        self._resume_listening_if_possible()

    def _handle_playback_started(self, _path: str) -> None:
        self._logger.info(
            "Playback started; suspending microphone input active_pipeline_count=%s recorder_active=%s",
            self._active_pipeline_count,
            self.recorder.is_recording,
        )
        self._logger.info("Stopping partial transcription timer during active playback")
        self._partial_timer.stop()
        self.recorder.suspend_input()

    def _handle_pipeline_state_changed(self, state: str, status: str) -> None:
        if not self._voice_connection_enabled:
            self._logger.info(
                "Ignoring pipeline state change while voice connection disabled state=%s status=%s",
                state,
                status,
            )
            return
        self._logger.info("Pipeline state change state=%s status=%s", state, status)
        self._apply_state(state, status)

    def _handle_segment_ready(self) -> None:
        self._logger.info("Resetting partial tracking because a full segment is ready")
        self._reset_partial_tracking()
        self._logger.info(
            "Segment-ready signal received voice_connection_enabled=%s active_pipeline_count=%s recorder_active=%s",
            self._voice_connection_enabled,
            self._active_pipeline_count,
            self.recorder.is_recording,
        )
        audio_path = self.recorder.take_pending_segment()
        if audio_path is None:
            self._logger.info("Segment-ready signal fired with no pending audio segment")
            return
        if not self._voice_connection_enabled:
            self._logger.info("Discarding pending segment because voice connection is disabled path=%s", audio_path)
            audio_path.unlink(missing_ok=True)
            return
        if time.monotonic() < self._ignore_segment_until_monotonic:
            self._logger.info(
                "Discarding pending segment because it arrived during post-finalize holdoff path=%s",
                audio_path,
            )
            audio_path.unlink(missing_ok=True)
            return

        self._active_pipeline_count += 1
        self._logger.info(
            "Submitting voice turn path=%s active_pipeline_count=%s",
            audio_path,
            self._active_pipeline_count,
        )
        self._ignore_segment_until_monotonic = time.monotonic() + self._POST_FINALIZE_INPUT_HOLDOFF_SECONDS
        self._logger.info(
            "Applying post-finalize input holdoff holdoff_seconds=%.2f until_monotonic=%.3f",
            self._POST_FINALIZE_INPUT_HOLDOFF_SECONDS,
            self._ignore_segment_until_monotonic,
        )
        self.recorder.suspend_input()
        self._apply_state(AppState.TRANSCRIBING.value, "Detected silence, processing turn")
        # Snapshot conversation history on the GUI thread before the
        # executor takes over. The provider walks the `ConversationModel`,
        # which is mutated only on the GUI thread — capturing here is
        # the only safe read site. Falling through to None hands the
        # executor an empty list and `_run_pipeline` rebuilds a
        # single-turn payload from the chat client's system prompt.
        history_snapshot: list[dict[str, str]] | None
        if self.chat_history_provider is not None:
            try:
                history_snapshot = list(self.chat_history_provider())
            except Exception:
                self._logger.exception(
                    "chat_history_provider raised; falling back to single-turn"
                )
                history_snapshot = None
        else:
            history_snapshot = None
        future = self.executor.submit(self._run_pipeline, audio_path, history_snapshot)
        future.add_done_callback(self._handle_pipeline_done)

    def _schedule_partial_check(self) -> None:
        skip_reason = self._partial_skip_reason()
        if skip_reason is not None:
            self._log_partial_skip(skip_reason)
            return

        snapshot = self.recorder.snapshot_active_segment()
        if snapshot is None:
            self._log_partial_skip("no_active_segment")
            return

        audio_path, turn_frames = snapshot
        minimum_partial_frames = int(self.recorder.sample_rate * self._PARTIAL_MIN_SPEECH_SECONDS)
        if turn_frames < minimum_partial_frames:
            self._logger.info(
                "Skipping partial transcription because active turn is too short turn_frames=%s minimum_frames=%s",
                turn_frames,
                minimum_partial_frames,
            )
            audio_path.unlink(missing_ok=True)
            return

        self._partial_generation += 1
        generation = self._partial_generation
        self._partial_inflight = True
        self._logger.info(
            "Scheduling partial transcription snapshot path=%s turn_frames=%s generation=%s",
            audio_path,
            turn_frames,
            generation,
        )
        future = self.partial_executor.submit(
            self._run_partial_transcription,
            audio_path,
            turn_frames,
            generation,
        )
        future.add_done_callback(self._handle_partial_done)

    def _run_partial_transcription(self, audio_path: Path, turn_frames: int, generation: int) -> dict[str, object]:
        try:
            if not self.transcriber.is_loaded:
                self._logger.info(
                    "Loading STT model for partial transcription generation=%s path=%s",
                    generation,
                    audio_path,
                )
                self.transcriber.ensure_loaded()
            transcript = self.transcriber.transcribe(audio_path)
            return {"text": transcript, "turn_frames": turn_frames, "generation": generation}
        except Exception as exc:
            return {"text": "", "turn_frames": turn_frames, "generation": generation, "error": str(exc)}
        finally:
            audio_path.unlink(missing_ok=True)

    def _handle_partial_done(self, future: Future[dict[str, object]]) -> None:
        # Runs on the partial-executor thread. MUST NOT mutate
        # `_partial_inflight` directly; route the change through
        # `_partial_inflight_changed` so the queued slot on the owner thread
        # is the sole writer.
        self._partial_inflight_changed.emit(False)
        try:
            payload = future.result()
        except Exception as exc:
            self._logger.exception("Partial transcription future failed unexpectedly")
            payload = {"text": "", "speech_frames": 0, "error": str(exc)}
        self.partial_transcription_ready.emit(payload)

    @Slot(int)
    def _on_pipeline_count_delta(self, delta: int) -> None:
        # Sole owner-thread writer for `_active_pipeline_count` when the
        # update originates on a worker thread. Runs via QueuedConnection
        # so it always executes on the thread that owns this QObject.
        new_count = max(0, self._active_pipeline_count + delta)
        self._active_pipeline_count = new_count
        self._logger.info(
            "Voice pipeline count updated via queued bridge delta=%s active_pipeline_count=%s",
            delta,
            new_count,
        )

    @Slot(bool)
    def _on_partial_inflight_changed(self, inflight: bool) -> None:
        # Sole owner-thread writer for `_partial_inflight` when the update
        # originates on a worker thread. Runs via QueuedConnection.
        self._partial_inflight = inflight

    def _handle_partial_transcription_ready(self, payload: object) -> None:
        if not isinstance(payload, dict):
            return
        if not self._voice_connection_enabled or self._playing_response or self._active_pipeline_count > 0:
            return

        transcript = str(payload.get("text", "")).strip()
        turn_frames = int(payload.get("turn_frames", 0) or 0)
        generation = int(payload.get("generation", 0) or 0)
        error = str(payload.get("error", "")).strip()
        if generation != self._partial_generation:
            self._logger.info(
                "Ignoring stale partial transcription generation=%s current_generation=%s",
                generation,
                self._partial_generation,
            )
            return
        if error:
            self._logger.info(
                "Partial transcription yielded no usable text error=%s turn_frames=%s generation=%s",
                error,
                turn_frames,
                generation,
            )
            return
        if not transcript:
            self._logger.info(
                "Partial transcription empty turn_frames=%s generation=%s",
                turn_frames,
                generation,
            )
            return

        now = time.monotonic()
        if transcript != self._partial_last_text:
            self._partial_last_text = transcript
            self._partial_last_speech_frames = turn_frames
            self._partial_last_change_monotonic = now
            self._set_live_transcript(transcript)
            self._logger.info(
                "Partial transcription updated transcript_chars=%s turn_frames=%s generation=%s",
                len(transcript),
                turn_frames,
                generation,
            )
            return

        if self._partial_last_change_monotonic == 0.0:
            self._partial_last_change_monotonic = now
        idle_seconds = now - self._partial_last_change_monotonic
        self._logger.info(
            "Partial transcription unchanged transcript_chars=%s idle_seconds=%.2f turn_frames=%s generation=%s",
            len(transcript),
            idle_seconds,
            turn_frames,
            generation,
        )
        if idle_seconds >= self._PARTIAL_STALE_SECONDS and self.recorder.force_finalize_active_segment("stale_partial"):
            self._logger.info("Forced segment finalization from stale partial transcript")

    def _apply_state(self, state: str, status: str) -> None:
        self._logger.info("Controller state update state=%s status=%s", state, status)
        self._set_state(AppState(state))
        self.status_changed.emit(status)

    def _set_state(self, state: AppState) -> None:
        self.state = state
        self.state_changed.emit(state.value)

    def _resume_listening_if_possible(self) -> None:
        self._logger.info(
            "Resume listening check voice_connection_enabled=%s active_pipeline_count=%s playing_response=%s aux_playback_active=%s recorder_active=%s",
            self._voice_connection_enabled,
            self._active_pipeline_count,
            self._playing_response,
            self._aux_playback_active,
            self.recorder.is_recording,
        )
        if self._voice_connection_enabled:
            if self._active_pipeline_count == 0 and not self._playing_response and not self._aux_playback_active:
                self._apply_state(AppState.RECORDING.value, "Listening for next turn")
            return
        if self._active_pipeline_count == 0 and not self._playing_response and not self._aux_playback_active:
            self._apply_state(AppState.IDLE.value, "Ready")

    def _start_listening(self) -> bool:
        if self.recorder.is_recording:
            self._logger.info("Microphone start skipped because recorder is already active")
            return True
        self._logger.info("Starting microphone stream for voice connection")
        started = time.monotonic()
        try:
            self.recorder.start(segment_ready_callback=self.segment_ready.emit)
        except Exception as exc:
            self._logger.exception("Failed to start microphone recording")
            self.error_changed.emit(str(exc))
            self._voice_connection_enabled = False
            self.connection_changed.emit(False)
            self._apply_state(AppState.IDLE.value, "Microphone unavailable")
            return False
        log_ui_timing(self._logger, "recorder.start", started)
        self._logger.info("Microphone stream started for voice connection")
        return True

    def _reset_partial_tracking(self) -> None:
        self._set_live_transcript("")
        self._partial_last_text = ""
        self._partial_last_change_monotonic = 0.0
        self._partial_last_speech_frames = 0
        self._partial_generation = 0
        self._partial_last_skip_reason = ""
        self._partial_last_skip_log_monotonic = 0.0

    def _set_live_transcript(self, text: str) -> None:
        normalized = text.strip()
        if normalized == self._live_transcript:
            return
        self._live_transcript = normalized
        self.live_transcript_changed.emit(normalized)

    def _resume_input_after_holdoff(self, generation: int) -> None:
        if generation != self._resume_input_generation:
            self._logger.info(
                "Skipping holdoff resume because generation is stale generation=%s current_generation=%s",
                generation,
                self._resume_input_generation,
            )
            return
        if not self._voice_connection_enabled:
            self._logger.info("Skipping holdoff resume because voice connection is disabled")
            return
        if self._playing_response:
            self._logger.info("Skipping holdoff resume because playback is active")
            return
        if not self.recorder.is_recording:
            self._logger.info("Skipping holdoff resume because recorder is inactive")
            return
        self._logger.info("Resuming microphone input after post-finalize holdoff")
        self.recorder.resume_input(reason="post_finalize_holdoff")

    def _schedule_input_resume_after_cooldown(self, reason: str) -> None:
        self._resume_input_generation += 1
        generation = self._resume_input_generation
        self._logger.info(
            "Scheduling microphone input resume reason=%s cooldown_seconds=%.2f generation=%s",
            reason,
            self._POST_PLAYBACK_COOLDOWN_SECONDS,
            generation,
        )
        QTimer.singleShot(
            int(self._POST_PLAYBACK_COOLDOWN_SECONDS * 1000),
            lambda: self._resume_input_after_pipeline(generation, reason),
        )

    def _resume_input_after_pipeline(self, generation: int, reason: str) -> None:
        if generation != self._resume_input_generation:
            self._logger.info(
                "Skipping pipeline resume because generation is stale generation=%s current_generation=%s reason=%s",
                generation,
                self._resume_input_generation,
                reason,
            )
            return
        if not self._voice_connection_enabled:
            self._logger.info("Skipping pipeline resume because voice connection is disabled reason=%s", reason)
            return
        if self._playing_response:
            self._logger.info("Skipping pipeline resume because playback is active reason=%s", reason)
            return
        if self._active_pipeline_count > 0:
            self._logger.info(
                "Skipping pipeline resume because another pipeline is active reason=%s active_pipeline_count=%s",
                reason,
                self._active_pipeline_count,
            )
            return
        if not self.recorder.is_recording:
            self._logger.info("Skipping pipeline resume because recorder is inactive reason=%s", reason)
            return
        self._logger.info(
            "Resuming microphone input after pipeline cooldown reason=%s interval_ms=%s warmup_seconds=%.2f",
            reason,
            self._partial_timer.interval(),
            self._POST_PLAYBACK_INPUT_WARMUP_SECONDS,
        )
        warmup_seconds = self._POST_PLAYBACK_INPUT_WARMUP_SECONDS if reason.startswith("playback_") else 0.0
        self.recorder.resume_input(warmup_seconds=warmup_seconds, reason=reason)
        self._partial_timer.start()

    def _partial_skip_reason(self) -> str | None:
        if not self._voice_connection_enabled:
            return "voice_connection_disabled"
        if self._playing_response:
            return "playing_response"
        if self._aux_playback_active:
            return "aux_playback_active"
        if self._active_pipeline_count > 0:
            return "pipeline_active"
        if self._partial_inflight:
            return "partial_inflight"
        if not self.recorder.is_recording:
            return "recorder_inactive"
        if time.monotonic() < self._ignore_segment_until_monotonic:
            return "post_finalize_holdoff"
        return None

    def _log_partial_skip(self, reason: str) -> None:
        now = time.monotonic()
        if (
            reason == self._partial_last_skip_reason
            and (now - self._partial_last_skip_log_monotonic) < self._PARTIAL_SKIP_LOG_INTERVAL_SECONDS
        ):
            return
        self._partial_last_skip_reason = reason
        self._partial_last_skip_log_monotonic = now
        self._logger.info("Skipping partial transcription check reason=%s", reason)
