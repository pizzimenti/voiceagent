from __future__ import annotations

from concurrent.futures import Future
from typing import Optional

from PySide6.QtCore import QObject, Qt, Signal

from voiceagent.backends import TextToSpeechBackend
from voiceagent.parallel_item_loader import ParallelItemLoader


class TtsVoiceLoader(ParallelItemLoader):
    """Piper-flavored loader. The state machine lives in `ParallelItemLoader`."""

    # Emitted once the deferred remote catalog refresh completes. Payload
    # is the full sorted voice-name list (on-disk ∪ cache ∪ remote). Fires
    # only when the refreshed list actually differs from the eager set,
    # so QML doesn't churn its delegates on a no-op refresh.
    catalog_changed = Signal(list)

    # Worker-thread → owner-thread bridge for the catalog refresh. The
    # done-callback runs on the executor thread; this queued signal lands
    # the result on the owner thread, matching the `_progress_tick`
    # pattern already used by the base class.
    _catalog_refresh_finished = Signal(list)

    def __init__(
        self, tts_service: TextToSpeechBackend, parent: QObject | None = None
    ) -> None:
        super().__init__(
            tts_service,
            max_workers=3,
            thread_name_prefix="voiceagent-tts-loader",
            parent=parent,
        )
        self.tts_service = tts_service
        self._catalog_refresh_scheduled = False
        self._catalog_refresh_finished.connect(
            self._handle_catalog_refresh_finished,
            Qt.ConnectionType.QueuedConnection,
        )
        self._emit_initial_state()

    # -- deferred catalog refresh ------------------------------------------

    @property
    def catalog_refresh_scheduled(self) -> bool:
        return self._catalog_refresh_scheduled

    def refresh_catalog_async(self) -> None:
        """Kick off the network refresh of the voice catalog.

        Idempotent: only the first call actually schedules work. Callers
        invoke this after QML first paint so the blocking `voices.json`
        fetch never stalls the UI thread. See AGENTS.md: "keep
        network/model refreshes off the first paint path."
        """
        if self._catalog_refresh_scheduled:
            return
        # Capture the eager snapshot on the owner thread *before* the
        # worker runs. The worker's `refresh_catalog` call writes to the
        # on-disk cache as a side effect, so calling `available_items()`
        # after the worker would always match the post-fetch list and
        # the change check would miss real additions. Snapshot first so
        # `_catalog_refresh_scheduled` only goes True once we know we'll
        # actually submit (otherwise an `available_items()` raise would
        # leak the flag and lock out future refreshes).
        pre_refresh = list(self.tts_service.available_items())
        try:
            future = self.executor.submit(self._refresh_catalog_worker)
        except RuntimeError:
            # Executor has been shut down — typically because MainWindow.show()
            # scheduled this via QTimer.singleShot(0, …) and shutdown ran
            # before the timer fired. Don't latch the flag; a future
            # re-init could still attempt a refresh.
            self._logger.debug(
                "TTS catalog refresh skipped: executor already shut down"
            )
            return
        # Invariant: flag set ⇔ work scheduled. Set only after submit
        # succeeded.
        self._catalog_refresh_scheduled = True
        future.add_done_callback(
            lambda f, snapshot=pre_refresh: self._dispatch_catalog_refresh_result(
                f, snapshot
            )
        )

    def _refresh_catalog_worker(self) -> list[str]:
        refresh = getattr(self.tts_service, "refresh_catalog", None)
        if not callable(refresh):
            return list(self.tts_service.available_items())
        return list(refresh())

    def _dispatch_catalog_refresh_result(
        self, future: "Future[list[str]]", pre_refresh: list[str]
    ) -> None:
        # Reset the scheduled flag once the worker has resolved (success,
        # exception, or no-op). The flag is a "do not stack concurrent
        # refreshes" guard, not a once-per-session latch — a transient
        # network failure on first paint must not lock out future
        # user-triggered re-fetches.
        try:
            try:
                names = future.result()
            except Exception as exc:  # pragma: no cover - defensive, logged for triage
                self._logger.warning("TTS catalog refresh failed: %s", exc)
                return
            # Only dispatch to the owner thread when the refresh actually
            # added/removed entries relative to the eager snapshot captured
            # before the fetch. The worker rewrites `voices.json` as a side
            # effect, so an identity check against `available_items()`
            # post-fetch would always look like "no change."
            if list(names) == list(pre_refresh):
                return
            self._catalog_refresh_finished.emit(list(names))
        finally:
            self._catalog_refresh_scheduled = False

    def _handle_catalog_refresh_finished(self, names: list[str]) -> None:
        # The delta has already been validated on the worker side against
        # the eager pre-refresh snapshot. Forward to QML so the catalog
        # dropdown rebinds.
        if not names:
            return
        self.catalog_changed.emit(list(names))

    @property
    def is_enabled(self) -> bool:
        return self.tts_service.enabled

    @property
    def selected_model(self) -> str | None:
        return self.tts_service.selected_item

    def select_model(self, model_name: str | None) -> None:
        self.tts_service.set_selected_item(model_name)
        self._log_state("select_model")
        self.selection_changed.emit(model_name or "")
        self._emit_initial_state()

    def load_voice(self) -> None:
        if (
            not self.is_enabled
            or not self.selected_model
            or self.selected_model in self._active_items
            or self.is_ready
        ):
            return
        self.download_voice(self.selected_model)

    def download_voice(self, model_name: str) -> None:
        self.download_item(model_name)

    def select_and_load(self, model_name: str) -> None:
        """Select a model and immediately start downloading it."""
        self.tts_service.set_selected_item(model_name)
        self._log_state("select_and_load")
        self.selection_changed.emit(model_name)
        self._emit_initial_state()
        self.download_voice(model_name)

    def delete_voice(self, model_name: str) -> None:
        self.delete_item(model_name)

    # -- subclass overrides ------------------------------------------------

    def _can_download(self, name: str) -> bool:
        # Piper requires its CLI command to be present before downloading.
        return bool(getattr(self.tts_service, "command", None))

    def _verify_download(self, name: str) -> Optional[str]:
        """Adds **layer 4 (smoke-load)** on top of the base aria2-sidecar
        check.

        The corrupt-ONNX bug that motivated this work showed that even
        when the aria2 sidecar is gone, the `.onnx` payload can be
        truncated/malformed (e.g. an interrupted transfer resumed by a
        different tool). The cheap fix is to open it once with
        `onnxruntime.InferenceSession`: a corrupt file raises immediately
        (typically `InvalidProtobuf`) rather than deferring the crash to
        first synthesis where it surfaces as a baffling
        `wave.Error: # channels not specified`.

        Cost: ~50–100 ms per voice. Acceptable for a one-time
        post-download check; would NOT be acceptable on every
        `is_item_available` poll, so we do not wire it there.
        """
        base_error = super()._verify_download(name)
        if base_error is not None:
            return base_error

        try:
            artifact_paths = list(self.tts_service.artifact_paths(name))
        except Exception as exc:
            return f"could not determine artifact paths: {exc}"

        onnx_path = next(
            (p for p in artifact_paths if p.suffix == ".onnx"), None
        )
        if onnx_path is None or not onnx_path.exists():
            return f"Piper onnx artifact missing for {name}"

        try:
            import onnxruntime  # local import: heavy, only needed at verify time
        except Exception as exc:  # pragma: no cover - onnxruntime is a hard dep
            # onnxruntime is a hard project dependency. If the import
            # fails (broken install, ABI mismatch, missing system lib),
            # fail closed: a corrupt download must NOT pass smoke-load
            # verification just because the verifier itself is broken.
            # The user gets a clear error message naming the import
            # failure rather than a downstream `wave.Error` at synthesis.
            return (
                f"onnxruntime unavailable for smoke-load "
                f"({exc.__class__.__name__}): {exc}"
            )

        try:
            onnxruntime.InferenceSession(str(onnx_path))
        except Exception as exc:
            return (
                f"onnx smoke-load failed ({exc.__class__.__name__}): "
                f"{exc}"
            )

        return None

    def _emit_initial_state(self) -> None:
        self._log_state("emit_initial_state")
        self.ready_changed.emit(self.is_ready)
        self.loading_changed.emit(self.is_loading)
        if not self.selected_model:
            self.status_changed.emit(self._status_idle_prompt())
        elif self.is_ready:
            self.status_changed.emit(self._status_ready())
        else:
            self.status_changed.emit(self._status_select_to_enable())
        self.progress_changed.emit(self._aggregate_progress())

    def _log_state(self, context: str) -> None:
        details_getter = getattr(self.tts_service, "describe_selection_state", None)
        if callable(details_getter):
            details = details_getter()
            self._logger.info(
                "TTS state context=%s enabled=%s ready=%s loading=%s active_items=%s "
                "selected_model=%s available=%s can_download=%s resolved_model_path=%s "
                "direct_candidate=%s local_candidate=%s onnx_candidate=%s json_candidate=%s",
                context,
                self.is_enabled,
                self.is_ready,
                self.is_loading,
                sorted(self._active_items),
                details.get("selected_model", ""),
                details.get("available", False),
                details.get("can_download", False),
                details.get("resolved_model_path", ""),
                details.get("direct_candidate", ""),
                details.get("local_candidate", ""),
                details.get("onnx_candidate", ""),
                details.get("json_candidate", ""),
            )
            return

        self._logger.info(
            "TTS state context=%s enabled=%s ready=%s loading=%s active_items=%s selected_model=%s",
            context,
            self.is_enabled,
            self.is_ready,
            self.is_loading,
            sorted(self._active_items),
            self.selected_model,
        )

    # -- subclass status hooks --------------------------------------------

    def _status_checking(self) -> str:
        return (
            f"Preparing {self.tts_service.backend_name} "
            f"{self.tts_service.selection_label.lower()} download"
        )

    def _status_downloading(self) -> str:
        return (
            f"Downloading {self.tts_service.backend_name} "
            f"{self.tts_service.selection_label.lower()} with aria2"
        )

    def _status_removing(self) -> str:
        return (
            f"Removing {self.tts_service.backend_name} "
            f"{self.tts_service.selection_label.lower()}"
        )

    def _status_ready(self) -> str:
        return (
            f"{self.tts_service.backend_name} "
            f"{self.tts_service.selection_label.lower()} ready"
        )

    def _status_load_failed(self) -> str:
        return f"{self.tts_service.backend_name} load failed"

    def _status_remove_failed(self) -> str:
        return f"{self.tts_service.backend_name} remove failed"

    def _status_idle_prompt(self) -> str:
        return (
            f"Select a {self.tts_service.backend_name} "
            f"{self.tts_service.selection_label.lower()}"
        )

    def _status_removed_ok(self) -> str:
        return f"{self.tts_service.backend_name} voice removed"

    def _status_select_to_enable(self) -> str:
        return (
            f"Load {self.tts_service.backend_name} "
            f"{self.tts_service.selection_label.lower()} to enable speech"
        )
