from __future__ import annotations

import pytest
from PySide6.QtCore import QSettings

from voiceagent.services.llm_controller import LlmController


class FakeChatClient:
    """Minimal stand-in for LmStudioClient.

    The real client makes blocking HTTP calls; we trade them for in-memory
    fixtures so the controller's state machine can be exercised without any
    network dependency.
    """

    def __init__(self) -> None:
        self.base_url = ""
        self.model = ""
        self.system_prompt = "test"
        self.timeout_seconds = 5
        self.list_models_result: list[str] = []
        self.list_loaded_models_result: list[str] = []
        self.load_model_calls: list[str] = []
        self.unload_calls = 0
        self.raise_on_list: Exception | None = None

    @staticmethod
    def normalize_base_url(value: str) -> str:
        from voiceagent.services.chat import LmStudioClient

        return LmStudioClient.normalize_base_url(value)

    def set_base_url(self, value: str) -> None:
        self.base_url = self.normalize_base_url(value)

    def set_model(self, value: str) -> None:
        self.model = value.strip()

    def list_models(self) -> list[str]:
        if self.raise_on_list is not None:
            raise self.raise_on_list
        return list(self.list_models_result)

    def list_loaded_models(self) -> list[str]:
        return list(self.list_loaded_models_result)

    def load_model(self, name: str) -> str:
        self.load_model_calls.append(name)
        self.list_loaded_models_result = [name]
        self.model = name
        return name

    def unload_all_models(self) -> None:
        self.unload_calls += 1
        self.list_loaded_models_result = []
        self.model = ""


@pytest.fixture(autouse=True)
def _isolated_settings(tmp_path, monkeypatch):
    # QSettings normally writes to ~/.config; redirect it so each test gets a
    # blank slate and never touches the developer's real preferences.
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(
        QSettings.Format.IniFormat,
        QSettings.Scope.UserScope,
        str(tmp_path),
    )
    yield


@pytest.fixture
def chat_client() -> FakeChatClient:
    return FakeChatClient()


@pytest.fixture
def settings() -> QSettings:
    # A unique org/app per test keeps the on-disk Ini files non-overlapping.
    import uuid

    org = f"voiceagent-test-{uuid.uuid4().hex[:8]}"
    return QSettings(org, "llm-controller")


@pytest.fixture
def controller(qtbot, chat_client, settings):
    # The fake controller uses the real LlmController but bypasses the
    # background executor for refresh by patching _refresh_models_task to a
    # synchronous closure wherever a test wants deterministic results. In
    # general we let qtbot wait on signals.
    ctrl = LlmController(chat_client, settings)
    yield ctrl
    ctrl.shutdown()


def _patch_refresh_to_return(controller, payload):
    """Make `_start_refresh` always resolve to `payload` synchronously enough
    for `qtbot.waitSignal` to observe the result."""

    def fake_task(request_id: int, show_error: bool):
        out = dict(payload)
        out.setdefault("request_id", request_id)
        out.setdefault("show_error", show_error)
        return out

    controller._refresh_models_task = fake_task  # type: ignore[assignment]


def test_set_current_url_normalizes_and_emits(qtbot, controller, chat_client):
    with qtbot.waitSignal(controller.current_url_changed, timeout=1000) as blocker:
        controller.set_current_url("example.local:1234")
    assert blocker.args == ["http://example.local:1234/v1"]
    assert chat_client.base_url == "http://example.local:1234/v1"


def test_set_current_url_no_op_when_unchanged(qtbot, controller, chat_client):
    controller.set_current_url("example.local:1234")
    # Second call with same URL must NOT emit current_url_changed again. The
    # easiest way to assert that is to invert waitSignal with a short timeout.
    with qtbot.assertNotEmitted(controller.current_url_changed, wait=200):
        controller.set_current_url("example.local:1234")
    assert chat_client.base_url == "http://example.local:1234/v1"


def test_persist_current_url_writes_qsettings(qtbot, controller, chat_client, settings):
    controller.set_current_url("persist.host:1234")
    controller.persist_current_url()
    assert settings.value("current_llm_url") == "http://persist.host:1234/v1"
    history = settings.value("llm_url_history", [], list)
    assert "http://persist.host:1234/v1" in history


def test_refresh_models_success_emits_models_changed(qtbot, controller, chat_client):
    _patch_refresh_to_return(
        controller,
        {"ok": True, "models": ["alpha", "beta"], "loaded_model": "alpha"},
    )
    controller.set_current_url("host:1234")
    with qtbot.waitSignal(controller.models_changed, timeout=2000) as blocker:
        controller.refresh_models(False)
    models, loaded = blocker.args
    assert models == ["alpha", "beta"]
    assert loaded == "alpha"
    assert controller.server_connected is True


def test_refresh_models_failure_emits_error_and_disconnects(
    qtbot, controller, chat_client
):
    _patch_refresh_to_return(
        controller, {"ok": False, "error": "connection refused"}
    )
    controller.set_current_url("host:1234")
    with qtbot.waitSignal(controller.error, timeout=2000) as blocker:
        controller.refresh_models(True)
    title, message = blocker.args
    assert "Unable to connect" in title
    assert "connection refused" in message
    assert controller.server_connected is False


def test_refresh_models_failure_silent_when_show_error_false(
    qtbot, controller, chat_client
):
    _patch_refresh_to_return(
        controller, {"ok": False, "error": "timeout"}
    )
    controller.set_current_url("host:1234")
    # show_error=False should NOT fire the error signal, but should still log
    # a status message describing the failure. Using assertNotEmitted ensures
    # the dialog-level error signal stays quiet.
    with qtbot.assertNotEmitted(controller.error, wait=400):
        with qtbot.waitSignal(controller.status_message, timeout=2000):
            controller.refresh_models(False)
    assert controller.server_connected is False


def test_stale_refresh_request_is_dropped(qtbot, controller, chat_client):
    # Bump the active request id past 1 by issuing a real refresh first, then
    # synthesise an old-id payload through the operation_finished signal and
    # assert no models_changed fires for it.
    controller.set_current_url("host:1234")
    controller._llm_refresh_request_id = 7
    controller._llm_active_refresh_request_id = 7
    with qtbot.assertNotEmitted(controller.models_changed, wait=300):
        controller._operation_finished.emit(
            "refresh",
            {
                "ok": True,
                "models": ["stale"],
                "loaded_model": "stale",
                "request_id": 3,
                "show_error": False,
            },
        )


def test_disconnect_server_clears_state(qtbot, controller, chat_client):
    # First connect.
    _patch_refresh_to_return(
        controller,
        {"ok": True, "models": ["alpha"], "loaded_model": "alpha"},
    )
    controller.set_current_url("host:1234")
    with qtbot.waitSignal(controller.connection_state_changed, timeout=2000) as blocker:
        controller.refresh_models(False)
    assert blocker.args == [True]

    # Then disconnect.
    with qtbot.waitSignal(controller.connection_state_changed, timeout=2000) as blocker:
        controller.disconnect_server()
    assert blocker.args == [False]
    assert controller.server_connected is False
    assert controller.models == []


def test_select_model_loads_via_chat_client(qtbot, controller, chat_client):
    # Force a connected state without going through refresh.
    controller._set_connected(True)
    # Pre-populate the model list so select_model does not reject input.
    controller._llm_models = ["foo"]

    with qtbot.waitSignal(controller.model_busy_changed, timeout=2000) as busy_on:
        controller.select_model("foo")
    assert busy_on.args == [True]

    # Wait for busy to go back to False after the executor task completes.
    qtbot.waitUntil(lambda: controller.model_busy is False, timeout=2000)
    assert "foo" in chat_client.load_model_calls
    assert chat_client.model == "foo"


def test_select_model_without_connection_emits_error_log(qtbot, controller):
    # Not connected → controller should emit an error signal complaining about
    # the missing connection without ever flipping model_busy.
    with qtbot.waitSignal(controller.error, timeout=1000) as blocker:
        controller.select_model("foo")
    title, message = blocker.args
    assert "Connect" in title
    assert message == ""
    assert controller.model_busy is False


def test_two_rapid_refreshes_only_emit_latest(qtbot, controller, chat_client):
    # First refresh's payload is "stale"; second is "fresh". Both go through
    # the operation_finished bridge synchronously here, so we can fire them in
    # order and verify only the active one updates models.
    controller.set_current_url("host:1234")
    controller._llm_refresh_request_id = 2
    controller._llm_active_refresh_request_id = 2

    received: list[tuple[list[str], str]] = []
    controller.models_changed.connect(lambda models, loaded: received.append((list(models), loaded)))

    controller._operation_finished.emit(
        "refresh",
        {
            "ok": True,
            "models": ["stale-model"],
            "loaded_model": "stale-model",
            "request_id": 1,
            "show_error": False,
        },
    )
    controller._operation_finished.emit(
        "refresh",
        {
            "ok": True,
            "models": ["fresh-model"],
            "loaded_model": "fresh-model",
            "request_id": 2,
            "show_error": False,
        },
    )

    # Only the request_id == active emission should have produced models_changed.
    assert len(received) == 1
    assert received[0] == (["fresh-model"], "fresh-model")
