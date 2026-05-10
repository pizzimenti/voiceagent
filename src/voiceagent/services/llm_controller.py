"""LLM connection / model orchestration extracted from MainWindow.

The controller owns the LM Studio connection state machine and the
ThreadPoolExecutor that drives blocking HTTP calls off the GUI thread. It
emits high-level signals describing state transitions so the QML-facing
properties on MainWindow can stay stable while the implementation lives
here.
"""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from typing import Callable

from PySide6.QtCore import QObject, QSettings, Signal, Slot

from voiceagent.services.chat import LmStudioClient


class LlmController(QObject):
    """Orchestrates the LM Studio connection state machine.

    Emits high-level signals so MainWindow can update QML-facing properties
    without owning any of the LLM mechanics directly.
    """

    # State transitions only — not chatty per-mutation signals.
    urls_changed = Signal(list)
    current_url_changed = Signal(str)
    connection_state_changed = Signal(bool)
    connection_busy_changed = Signal(bool)
    model_busy_changed = Signal(bool)
    models_changed = Signal(list, str)
    selected_model_changed = Signal(str)
    status_message = Signal(str)
    error = Signal(str, str)

    # Internal worker-thread → main-thread bridge.
    _operation_finished = Signal(str, object)

    def __init__(
        self,
        chat_client: LmStudioClient,
        settings: QSettings,
        parent: QObject | None = None,
        default_url: str = "silverthread:1234",
    ) -> None:
        super().__init__(parent)
        self._chat_client = chat_client
        self._settings = settings
        self._default_llm_url = default_url
        self._llm_models: list[str] = []
        self._llm_server_connected = False
        self._llm_connection_busy = False
        self._llm_model_busy = False
        self._llm_executor = ThreadPoolExecutor(
            max_workers=2, thread_name_prefix="voiceagent-llm"
        )
        # Set BEFORE `_llm_executor.shutdown()` in `shutdown()` so that any
        # Qt slot driving a fresh `_submit_operation` after teardown has
        # begun sees the flag and bails out instead of racing with the
        # dying pool.
        self._shutdown_started = False
        self._llm_refresh_request_id = 0
        self._llm_active_refresh_request_id = 0
        self._startup_llm_connect_scheduled = False
        self._operation_finished.connect(self._handle_operation_finished)

        # Drop legacy keys that were never reused after the LLM rework. They
        # used to live in MainWindow.__init__; keeping the cleanup here keeps
        # the QSettings hygiene responsibility tied to the controller that
        # owns the URL/model state.
        self._settings.remove("current_llm_model")
        self._settings.remove("llm_model_history")

        self._populate_urls()

    # ------------------------------------------------------------------
    # Properties / accessors
    # ------------------------------------------------------------------
    @property
    def is_ready(self) -> bool:
        return bool(self._chat_client.base_url and self._chat_client.model)

    @property
    def models(self) -> list[str]:
        return list(self._llm_models)

    @property
    def server_connected(self) -> bool:
        return self._llm_server_connected

    @property
    def connection_busy(self) -> bool:
        return self._llm_connection_busy

    @property
    def model_busy(self) -> bool:
        return self._llm_model_busy

    @property
    def startup_connect_scheduled(self) -> bool:
        return self._startup_llm_connect_scheduled

    def mark_startup_connect_scheduled(self) -> None:
        self._startup_llm_connect_scheduled = True

    def current_url(self) -> str:
        return (
            self._initial_url()
            if not self._chat_client.base_url
            else self._chat_client.base_url
        )

    def url_options(self) -> list[str]:
        history = self._settings.value("llm_url_history", [], list) or []
        entries = [
            entry for entry in history if isinstance(entry, str) and entry.strip()
        ]
        current_base_url = self._chat_client.base_url
        if current_base_url:
            entries.insert(0, current_base_url)
        entries.insert(0, self._default_llm_url)
        unique_entries: list[str] = []
        for entry in entries:
            normalized = entry.strip()
            if normalized and normalized not in unique_entries:
                unique_entries.append(normalized)
        return unique_entries

    # ------------------------------------------------------------------
    # Public slots
    # ------------------------------------------------------------------
    @Slot(str)
    def set_current_url(self, value: str) -> None:
        normalized_value = self._chat_client.normalize_base_url(value)
        if normalized_value == self._chat_client.base_url:
            return
        self._chat_client.set_base_url(value)
        self._chat_client.set_model("")
        self._llm_models = []
        previous_connected = self._llm_server_connected
        self._llm_server_connected = False
        self.current_url_changed.emit(self.current_url())
        self.urls_changed.emit(self.url_options())
        self.models_changed.emit(list(self._llm_models), self._chat_client.model)
        self.selected_model_changed.emit(self._chat_client.model)
        if previous_connected:
            self.connection_state_changed.emit(False)

    @Slot()
    def persist_current_url(self) -> None:
        value = self._chat_client.base_url.strip()
        if not value:
            return
        self._settings.setValue("current_llm_url", value)
        history = self._settings.value("llm_url_history", [], list) or []
        entries = [
            entry for entry in history if isinstance(entry, str) and entry.strip()
        ]
        updated_entries = [value, *[entry for entry in entries if entry != value]]
        self._settings.setValue("llm_url_history", updated_entries[:10])
        self.urls_changed.emit(self.url_options())

    @Slot(bool)
    def refresh_models(self, show_error: bool = False) -> None:
        self._start_refresh(show_error=show_error)

    @Slot(str)
    def select_model(self, model_name: str) -> None:
        if not self._llm_server_connected:
            self.error.emit(
                "Connect to the LLM server before selecting a model.",
                "",
            )
            return
        if self._llm_connection_busy or self._llm_model_busy:
            return
        selected_model = model_name.strip()
        self._set_model_busy(True)
        if not selected_model:
            self.status_message.emit("Unloading the active LLM model...")
            self._submit_operation("select_model", self._unload_model_task)
            return
        self.status_message.emit(f"Loading LLM model {selected_model}...")
        self._submit_operation(
            "select_model", lambda: self._load_model_task(selected_model)
        )

    @Slot(str)
    def toggle_server_connection(self, value: str) -> None:
        if self._llm_connection_busy and self._llm_server_connected:
            return
        if self._llm_server_connected:
            self.disconnect_server()
            return
        self.connect_server(value, show_error=True)

    @Slot(str, bool)
    def connect_server(self, value: str, show_error: bool = True) -> None:
        if self._llm_connection_busy and self._llm_server_connected:
            return
        if value.strip():
            self.set_current_url(value)
        self._start_refresh(show_error=show_error)

    @Slot()
    def disconnect_server(self) -> None:
        if self._llm_connection_busy or self._llm_model_busy:
            return
        self._set_connection_busy(True)
        self._submit_operation("disconnect", self._disconnect_task)

    @Slot()
    def autoconnect_server(self) -> None:
        self._start_refresh(show_error=False)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def shutdown(self) -> None:
        # Flip the flag BEFORE the executor.shutdown call so any submitter
        # that checks `_shutdown_started` (the public `_submit_operation`
        # gate below) sees the no-op signal even before the executor
        # formally rejects further submissions.
        self._shutdown_started = True
        self._llm_executor.shutdown(wait=False, cancel_futures=True)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _initial_url(self) -> str:
        stored = self._settings.value("current_llm_url", "", str) or ""
        if stored:
            return stored
        return self._default_llm_url

    def _populate_urls(self) -> None:
        self._chat_client.set_base_url(self._initial_url())
        self.current_url_changed.emit(self.current_url())
        self.urls_changed.emit(self.url_options())

    def _populate_model_selector(
        self, models: list[str], loaded_model: str
    ) -> None:
        unique_models: list[str] = []
        for model in models:
            normalized = model.strip()
            if normalized and normalized not in unique_models:
                unique_models.append(normalized)
        if loaded_model and loaded_model not in unique_models:
            unique_models.insert(0, loaded_model)
        self._llm_models = unique_models
        previous_model = self._chat_client.model
        self._chat_client.set_model(loaded_model)
        self.models_changed.emit(list(self._llm_models), self._chat_client.model)
        if previous_model != self._chat_client.model:
            self.selected_model_changed.emit(self._chat_client.model)

    def _show_error(self, title: str, message: str) -> None:
        self.error.emit(title, message)

    def _set_connection_busy(self, busy: bool) -> None:
        if self._llm_connection_busy == busy:
            return
        self._llm_connection_busy = busy
        self.connection_busy_changed.emit(busy)

    def _set_model_busy(self, busy: bool) -> None:
        if self._llm_model_busy == busy:
            return
        self._llm_model_busy = busy
        self.model_busy_changed.emit(busy)

    def _set_connected(self, connected: bool) -> None:
        if self._llm_server_connected == connected:
            return
        self._llm_server_connected = connected
        self.connection_state_changed.emit(connected)

    def _start_refresh(self, show_error: bool) -> None:
        if self._llm_connection_busy and self._llm_server_connected:
            return
        self._llm_refresh_request_id += 1
        request_id = self._llm_refresh_request_id
        self._llm_active_refresh_request_id = request_id
        self.persist_current_url()
        self._set_connection_busy(True)
        self._submit_operation(
            "refresh",
            lambda: self._refresh_models_task(
                request_id=request_id, show_error=show_error
            ),
        )

    def _submit_operation(self, operation: str, task: Callable[[], object]) -> None:
        # Guard against a Qt slot landing here after `shutdown()` has flipped
        # `_shutdown_started`. The `RuntimeError` catch closes the residual
        # race between the flag check and the actual `submit` call (the
        # executor can transition to "shutdown" between the two lines).
        if self._shutdown_started:
            return
        try:
            future = self._llm_executor.submit(task)
        except RuntimeError:
            return
        future.add_done_callback(
            lambda completed: self._emit_operation_result(operation, completed)
        )

    def _emit_operation_result(
        self, operation: str, future: Future[object]
    ) -> None:
        try:
            payload = future.result()
        except Exception as exc:  # pragma: no cover - defensive bridge
            payload = {"ok": False, "error": str(exc)}
        self._operation_finished.emit(operation, payload)

    @Slot(str, object)
    def _handle_operation_finished(self, operation: str, payload: object) -> None:
        result = (
            payload
            if isinstance(payload, dict)
            else {"ok": False, "error": "Unexpected LLM operation result."}
        )
        ok = bool(result.get("ok"))
        if operation == "select_model":
            self._set_model_busy(False)
            loaded_model_raw = result.get("loaded_model")
            loaded_model = (
                str(loaded_model_raw).strip()
                if isinstance(loaded_model_raw, str)
                else None
            )
            if not ok:
                if loaded_model is not None:
                    self._populate_model_selector(self._llm_models, loaded_model)
                self._show_error(
                    "Unable to update LLM model", str(result.get("error", ""))
                )
                return
            loaded_model = loaded_model or ""
            self._populate_model_selector(self._llm_models, loaded_model)
            if loaded_model:
                self.status_message.emit(f"Loaded LLM model {loaded_model}.")
            else:
                self.status_message.emit("No LLM model loaded.")
            return

        # Drop stale refresh completions BEFORE touching the busy state.
        # If the earlier refresh's payload landed here while a newer refresh
        # is still in flight, clearing `connection_busy` here would cause a
        # spurious "connected, idle" UI flash until the live refresh resolved
        # and then tried (and failed) to clear an already-cleared busy.
        # Only the live refresh's outcome should touch the busy state.
        if operation == "refresh":
            request_id = int(result.get("request_id", 0) or 0)
            if request_id and request_id != self._llm_active_refresh_request_id:
                return
        self._set_connection_busy(False)
        if operation == "disconnect":
            if ok:
                self._set_connected(False)
                self._llm_models = []
                self._chat_client.set_model("")
                self.models_changed.emit(
                    list(self._llm_models), self._chat_client.model
                )
                self.selected_model_changed.emit(self._chat_client.model)
                self.status_message.emit("Disconnected from LLM server.")
            else:
                self._show_error(
                    "Unable to disconnect from LLM server",
                    str(result.get("error", "")),
                )
            return

        if not ok:
            self._set_connected(False)
            self._llm_models = []
            self._chat_client.set_model("")
            self.models_changed.emit(
                list(self._llm_models), self._chat_client.model
            )
            self.selected_model_changed.emit(self._chat_client.model)
            failure_message = (
                f"Unable to connect to LLM server: {str(result.get('error', ''))}"
            ).strip()
            if bool(result.get("show_error", True)):
                self._show_error(
                    "Unable to connect to LLM server", str(result.get("error", ""))
                )
            elif failure_message:
                self.status_message.emit(failure_message)
            return

        models = result.get("models", [])
        loaded_model = str(result.get("loaded_model", "")).strip()
        previous_models = list(self._llm_models)
        previous_loaded_model = self._chat_client.model
        self._set_connected(True)
        self._populate_model_selector(
            list(models) if isinstance(models, list) else [], loaded_model
        )
        self.status_message.emit(
            f"Connected to LLM server at {self.current_url()}."
        )

        added_models = [
            model for model in self._llm_models if model not in previous_models
        ]
        removed_models = [
            model for model in previous_models if model not in self._llm_models
        ]
        if added_models or removed_models:
            parts: list[str] = []
            if added_models:
                parts.append(f"added {len(added_models)}")
            if removed_models:
                parts.append(f"removed {len(removed_models)}")
            self.status_message.emit(
                f"LLM models refreshed: {', '.join(parts)}."
            )
        elif loaded_model and loaded_model != previous_loaded_model:
            self.status_message.emit(
                f"LLM models refreshed. Loaded model is now {loaded_model}."
            )
        elif self._llm_models:
            self.status_message.emit(
                f"LLM models refreshed. {len(self._llm_models)} model(s) available."
            )
        else:
            self.status_message.emit("LLM models refreshed. No models loaded.")

    # ------------------------------------------------------------------
    # Worker-thread tasks (run on the executor)
    # ------------------------------------------------------------------
    def _refresh_models_task(
        self, request_id: int, show_error: bool
    ) -> dict[str, object]:
        base_url = self._chat_client.base_url
        snapshot_client = LmStudioClient(
            base_url=base_url,
            model=self._chat_client.model,
            system_prompt=self._chat_client.system_prompt,
            timeout_seconds=self._chat_client.timeout_seconds,
            load_timeout_seconds=self._chat_client.load_timeout_seconds,
        )
        try:
            models = snapshot_client.list_models()
            try:
                loaded_models = snapshot_client.list_loaded_models()
            except RuntimeError:
                loaded_models = []
            # LM Studio bug workaround (May 2026): the native
            # `/api/v1/models` endpoint can report `loaded_instances:
            # []` for every model even when one is actually serving
            # chat completions. When that happens, fall back to the
            # OpenAI `/v1/models` entry that the native API also
            # marks as `type: llm` (excludes embeddings) — but ONLY
            # when there is exactly one such candidate. With multiple
            # candidates the heuristic can't tell which (if any) is
            # actually loaded; surfacing "no model loaded" is more
            # honest than picking arbitrarily and failing at the next
            # /chat/completions call. See chat.py:refresh_loaded_model
            # for the same pattern on the other detection path.
            if not loaded_models and models:
                try:
                    llm_keys = snapshot_client._llm_keys_from_native()
                except Exception:
                    llm_keys = set()
                llm_candidates = [
                    m for m in models if not llm_keys or m in llm_keys
                ]
                if len(llm_candidates) == 1:
                    loaded_models = [llm_candidates[0]]
            return {
                "ok": True,
                "models": models,
                "loaded_model": loaded_models[0] if loaded_models else "",
                "request_id": request_id,
                "show_error": show_error,
            }
        except RuntimeError as exc:
            return {
                "ok": False,
                "error": str(exc),
                "request_id": request_id,
                "show_error": show_error,
            }

    def _disconnect_task(self) -> dict[str, object]:
        return {"ok": True}

    def _load_model_task(self, model_name: str) -> dict[str, object]:
        try:
            loaded_model = self._chat_client.load_model(model_name)
        except RuntimeError as exc:
            try:
                loaded_models = self._chat_client.list_loaded_models()
            except RuntimeError:
                loaded_models = []
            return {
                "ok": False,
                "error": str(exc),
                "loaded_model": loaded_models[0] if loaded_models else "",
            }
        return {"ok": True, "loaded_model": loaded_model}

    def _unload_model_task(self) -> dict[str, object]:
        try:
            self._chat_client.unload_all_models()
        except RuntimeError as exc:
            try:
                loaded_models = self._chat_client.list_loaded_models()
            except RuntimeError:
                loaded_models = []
            result: dict[str, object] = {"ok": False, "error": str(exc)}
            if loaded_models:
                result["loaded_model"] = loaded_models[0]
            return result
        self._chat_client.set_model("")
        return {"ok": True, "loaded_model": ""}
