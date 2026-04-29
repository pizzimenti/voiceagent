"""Direct unit tests for `services/chat.py` (LmStudioClient).

The client uses `urllib.request.urlopen` (not httpx); HTTP calls are
mocked by monkeypatching `voiceagent.services.chat.request.urlopen`
with a fake context manager that yields a JSON-bytes stream. URL
error paths are exercised by raising the real `urllib.error.URLError`
/ `HTTPError` types so `_format_url_error` runs against the production
isinstance checks.
"""

from __future__ import annotations

import io
import json
import socket
from typing import Any
from urllib import error

import pytest

from voiceagent.services.chat import LmStudioClient


# --- helpers -------------------------------------------------------------


class _FakeResponse:
    """Minimal `urllib.request.urlopen` context-manager stand-in.

    `json.load` reads from the response, so we expose a `read()` that
    returns the encoded payload.
    """

    def __init__(self, payload: dict | list) -> None:
        self._buffer = io.BytesIO(json.dumps(payload).encode("utf-8"))

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *_exc: object) -> bool:
        return False

    def read(self, *args: Any, **kwargs: Any) -> bytes:
        return self._buffer.read(*args, **kwargs)


def _install_fake_urlopen(monkeypatch, handler):
    """Replace `chat.request.urlopen` with `handler(req, timeout)`."""
    monkeypatch.setattr(
        "voiceagent.services.chat.request.urlopen", handler
    )


# --- normalize_base_url --------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("http://localhost:1234", "http://localhost:1234/v1"),
        ("http://localhost:1234/", "http://localhost:1234/v1"),
        ("http://localhost:1234/v1", "http://localhost:1234/v1"),
        ("http://localhost:1234/v1/", "http://localhost:1234/v1"),
        ("localhost:1234", "http://localhost:1234/v1"),
        ("localhost:1234/v1", "http://localhost:1234/v1"),
        ("https://api.example.com:8443", "https://api.example.com:8443/v1"),
        ("  http://localhost:1234  ", "http://localhost:1234/v1"),
    ],
)
def test_normalize_base_url_accepts_variants(raw, expected):
    assert LmStudioClient.normalize_base_url(raw) == expected


@pytest.mark.parametrize("raw", ["", "   ", "\t\n"])
def test_normalize_base_url_returns_empty_for_blank(raw):
    assert LmStudioClient.normalize_base_url(raw) == ""


def test_normalize_base_url_strips_trailing_slashes_before_v1_check():
    assert LmStudioClient.normalize_base_url("http://x///") == "http://x/v1"


def test_set_base_url_uses_normalizer():
    client = LmStudioClient(
        base_url="localhost:1234", model="m", system_prompt="p"
    )
    assert client.base_url == "http://localhost:1234/v1"
    client.set_base_url("https://other:9000/v1")
    assert client.base_url == "https://other:9000/v1"


def test_set_base_url_handles_empty():
    client = LmStudioClient(
        base_url="http://x:1/v1", model="m", system_prompt="p"
    )
    client.set_base_url("")
    assert client.base_url == ""


# --- _native_api_root ----------------------------------------------------


def test_native_api_root_swaps_v1_for_api_v1():
    client = LmStudioClient(
        base_url="http://localhost:1234", model="m", system_prompt="p"
    )
    assert client._native_api_root() == "http://localhost:1234/api/v1"


def test_native_api_root_empty_when_unconfigured():
    client = LmStudioClient(base_url="", model="m", system_prompt="p")
    assert client._native_api_root() == ""


# --- _format_url_error ---------------------------------------------------


def test_format_url_error_timeout_socket():
    client = LmStudioClient(
        base_url="http://x:1", model="m", system_prompt="p", timeout_seconds=42
    )
    exc = error.URLError(reason=socket.timeout("timed out"))
    msg = client._format_url_error(exc)
    assert "42 seconds" in msg
    assert "timed out" in msg


def test_format_url_error_timeout_native_TimeoutError():
    client = LmStudioClient(
        base_url="http://x:1", model="m", system_prompt="p", timeout_seconds=7
    )
    exc = error.URLError(reason=TimeoutError("slow"))
    msg = client._format_url_error(exc)
    assert "7 seconds" in msg


def test_format_url_error_generic_connection_refused():
    client = LmStudioClient(
        base_url="http://x:1", model="m", system_prompt="p"
    )
    exc = error.URLError(reason=ConnectionRefusedError("nope"))
    msg = client._format_url_error(exc)
    # Not a timeout — falls through to str(exc).
    assert "seconds" not in msg
    assert "nope" in msg


# --- _json_request: HTTPError + URLError surface as RuntimeError ---------


def test_json_request_wraps_http_error(monkeypatch):
    client = LmStudioClient(
        base_url="http://localhost:1234", model="m", system_prompt="p"
    )

    def _raise_http(req, timeout):
        raise error.HTTPError(
            url=req.full_url,
            code=503,
            msg="Service Unavailable",
            hdrs=None,  # type: ignore[arg-type]
            fp=io.BytesIO(b"backend on fire"),
        )

    _install_fake_urlopen(monkeypatch, _raise_http)

    with pytest.raises(RuntimeError) as exc_info:
        client._json_request("http://localhost:1234/v1/models")
    msg = str(exc_info.value)
    assert "HTTP 503" in msg
    assert "backend on fire" in msg


def test_json_request_wraps_url_error_with_format(monkeypatch):
    client = LmStudioClient(
        base_url="http://localhost:1234",
        model="m",
        system_prompt="p",
        timeout_seconds=5,
    )

    def _raise_timeout(req, timeout):
        raise error.URLError(reason=socket.timeout("ack"))

    _install_fake_urlopen(monkeypatch, _raise_timeout)

    with pytest.raises(RuntimeError, match="timed out after 5 seconds"):
        client._json_request("http://localhost:1234/v1/models")


def test_json_request_method_and_body_propagate(monkeypatch):
    client = LmStudioClient(
        base_url="http://localhost:1234", model="m", system_prompt="p"
    )

    captured: dict[str, Any] = {}

    def _capture(req, timeout):
        captured["url"] = req.full_url
        captured["method"] = req.get_method()
        captured["body"] = req.data
        captured["content_type"] = req.get_header("Content-type")
        captured["timeout"] = timeout
        return _FakeResponse({"status": "loaded"})

    _install_fake_urlopen(monkeypatch, _capture)

    result = client._json_request(
        "http://localhost:1234/api/v1/models/load",
        payload={"model": "foo"},
        method="POST",
    )
    assert result == {"status": "loaded"}
    assert captured["url"] == "http://localhost:1234/api/v1/models/load"
    assert captured["method"] == "POST"
    assert json.loads(captured["body"]) == {"model": "foo"}
    assert captured["content_type"] == "application/json"


# --- list_models ---------------------------------------------------------


def test_list_models_happy_path(monkeypatch):
    client = LmStudioClient(
        base_url="http://localhost:1234", model="", system_prompt="p"
    )
    payload = {
        "data": [
            {"id": "alpha"},
            {"id": "beta"},
            {"id": "  gamma  "},  # stripped
            {"id": ""},  # dropped
            "not-a-dict",  # dropped
        ]
    }

    def _serve(req, timeout):
        assert req.full_url == "http://localhost:1234/v1/models"
        return _FakeResponse(payload)

    _install_fake_urlopen(monkeypatch, _serve)

    assert client.list_models() == ["alpha", "beta", "gamma"]


def test_list_models_raises_when_unconfigured():
    client = LmStudioClient(base_url="", model="", system_prompt="p")
    with pytest.raises(RuntimeError, match="LLM URL is not configured"):
        client.list_models()


def test_list_models_empty_list_raises(monkeypatch):
    client = LmStudioClient(
        base_url="http://localhost:1234", model="", system_prompt="p"
    )
    _install_fake_urlopen(
        monkeypatch, lambda req, timeout: _FakeResponse({"data": []})
    )
    with pytest.raises(RuntimeError, match="No models were returned"):
        client.list_models()


def test_list_models_unexpected_payload_raises(monkeypatch):
    client = LmStudioClient(
        base_url="http://localhost:1234", model="", system_prompt="p"
    )
    _install_fake_urlopen(
        monkeypatch, lambda req, timeout: _FakeResponse({"data": "string"})
    )
    with pytest.raises(RuntimeError, match="unexpected /models payload"):
        client.list_models()


# --- list_loaded_model_instances -----------------------------------------


def test_list_loaded_model_instances_filters_non_llm_and_empty(monkeypatch):
    client = LmStudioClient(
        base_url="http://localhost:1234", model="", system_prompt="p"
    )
    payload = {
        "models": [
            {
                "type": "llm",
                "key": "good",
                "loaded_instances": [{"id": "abc"}, {"id": ""}, "garbage"],
            },
            {"type": "embedding", "key": "skip-me", "loaded_instances": [{"id": "x"}]},
            {"type": "llm", "key": "", "loaded_instances": [{"id": "x"}]},
            {"type": "llm", "key": "no-instances"},
            "not-a-dict",
        ]
    }
    _install_fake_urlopen(
        monkeypatch, lambda req, timeout: _FakeResponse(payload)
    )

    assert client.list_loaded_model_instances() == [("good", "abc")]


def test_list_loaded_models_uses_instances(monkeypatch):
    client = LmStudioClient(
        base_url="http://localhost:1234", model="", system_prompt="p"
    )
    payload = {
        "models": [
            {
                "type": "llm",
                "key": "alpha",
                "loaded_instances": [{"id": "i1"}, {"id": "i2"}],
            }
        ]
    }
    _install_fake_urlopen(
        monkeypatch, lambda req, timeout: _FakeResponse(payload)
    )
    # Two instances of the same key → de-listed by key in `list_loaded_models`.
    assert client.list_loaded_models() == ["alpha", "alpha"]


def test_list_loaded_models_unexpected_payload_raises(monkeypatch):
    client = LmStudioClient(
        base_url="http://localhost:1234", model="", system_prompt="p"
    )
    _install_fake_urlopen(
        monkeypatch, lambda req, timeout: _FakeResponse({"models": "oops"})
    )
    with pytest.raises(RuntimeError, match="unexpected /api/v1/models payload"):
        client.list_loaded_models()


# --- complete (chat completion) ------------------------------------------


def test_complete_happy_path(monkeypatch):
    client = LmStudioClient(
        base_url="http://localhost:1234",
        model="local-llm",
        system_prompt="be helpful",
    )

    captured: dict[str, Any] = {}

    def _serve(req, timeout):
        captured["url"] = req.full_url
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return _FakeResponse(
            {"choices": [{"message": {"content": "  hi there  "}}]}
        )

    _install_fake_urlopen(monkeypatch, _serve)

    assert client.complete("ping") == "hi there"
    assert captured["url"] == "http://localhost:1234/v1/chat/completions"
    assert captured["body"]["model"] == "local-llm"
    assert captured["body"]["messages"][0] == {
        "role": "system",
        "content": "be helpful",
    }
    assert captured["body"]["messages"][1] == {
        "role": "user",
        "content": "ping",
    }
    assert captured["body"]["stream"] is False


def test_complete_raises_when_unconfigured():
    client = LmStudioClient(base_url="", model="m", system_prompt="p")
    with pytest.raises(RuntimeError, match="LLM URL is not configured"):
        client.complete("hi")


def test_complete_raises_when_model_missing(monkeypatch):
    client = LmStudioClient(
        base_url="http://localhost:1234", model="", system_prompt="p"
    )
    # ensure_model() falls through to refresh_loaded_model() → list_loaded_models.
    _install_fake_urlopen(
        monkeypatch, lambda req, timeout: _FakeResponse({"models": []})
    )
    with pytest.raises(RuntimeError, match="No LLM is currently loaded"):
        client.complete("hi")


def test_complete_wraps_url_error_with_timeout(monkeypatch):
    client = LmStudioClient(
        base_url="http://localhost:1234",
        model="m",
        system_prompt="p",
        timeout_seconds=3,
    )

    def _raise(req, timeout):
        raise error.URLError(reason=TimeoutError("slow"))

    _install_fake_urlopen(monkeypatch, _raise)
    with pytest.raises(RuntimeError, match="timed out after 3 seconds"):
        client.complete("hi")


def test_complete_unexpected_payload_raises(monkeypatch):
    client = LmStudioClient(
        base_url="http://localhost:1234", model="m", system_prompt="p"
    )
    _install_fake_urlopen(
        monkeypatch, lambda req, timeout: _FakeResponse({"choices": []})
    )
    with pytest.raises(RuntimeError, match="unexpected response payload"):
        client.complete("hi")


def test_complete_empty_string_raises(monkeypatch):
    client = LmStudioClient(
        base_url="http://localhost:1234", model="m", system_prompt="p"
    )
    _install_fake_urlopen(
        monkeypatch,
        lambda req, timeout: _FakeResponse(
            {"choices": [{"message": {"content": "   "}}]}
        ),
    )
    with pytest.raises(RuntimeError, match="empty response"):
        client.complete("hi")


# --- model lifecycle helpers ---------------------------------------------


def test_set_model_strips_whitespace():
    client = LmStudioClient(
        base_url="http://x:1", model="", system_prompt="p"
    )
    client.set_model("  granite  ")
    assert client.model == "granite"


def test_unload_other_models_keeps_named(monkeypatch):
    client = LmStudioClient(
        base_url="http://localhost:1234", model="", system_prompt="p"
    )

    serve_payload = {
        "models": [
            {
                "type": "llm",
                "key": "keep-me",
                "loaded_instances": [{"id": "k1"}],
            },
            {
                "type": "llm",
                "key": "drop-me",
                "loaded_instances": [{"id": "d1"}],
            },
        ]
    }

    unloaded: list[str] = []

    def _handler(req, timeout):
        body = req.data.decode("utf-8") if req.data else ""
        if req.get_method() == "GET":
            return _FakeResponse(serve_payload)
        # POST: unload
        unloaded.append(json.loads(body)["instance_id"])
        return _FakeResponse({"status": "ok"})

    _install_fake_urlopen(monkeypatch, _handler)

    client.unload_other_models(keep_model="keep-me")
    assert unloaded == ["d1"]


def test_unload_all_models_clears_model(monkeypatch):
    client = LmStudioClient(
        base_url="http://localhost:1234", model="alpha", system_prompt="p"
    )

    serve_payload = {
        "models": [
            {
                "type": "llm",
                "key": "alpha",
                "loaded_instances": [{"id": "a1"}],
            }
        ]
    }
    unloaded: list[str] = []

    def _handler(req, timeout):
        if req.get_method() == "GET":
            return _FakeResponse(serve_payload)
        unloaded.append(json.loads(req.data.decode("utf-8"))["instance_id"])
        return _FakeResponse({"status": "ok"})

    _install_fake_urlopen(monkeypatch, _handler)

    client.unload_all_models()
    assert unloaded == ["a1"]
    assert client.model == ""


def test_load_model_short_circuits_when_already_loaded(monkeypatch):
    client = LmStudioClient(
        base_url="http://localhost:1234", model="alpha", system_prompt="p"
    )

    serve_payload = {
        "models": [
            {
                "type": "llm",
                "key": "alpha",
                "loaded_instances": [{"id": "a1"}],
            }
        ]
    }

    def _handler(req, timeout):
        # Only GETs to /api/v1/models should fire — never the load POST.
        assert req.get_method() == "GET", (
            f"unexpected POST to {req.full_url} with body {req.data!r}"
        )
        return _FakeResponse(serve_payload)

    _install_fake_urlopen(monkeypatch, _handler)

    assert client.load_model("alpha") == "alpha"


def test_load_model_posts_when_not_loaded(monkeypatch):
    client = LmStudioClient(
        base_url="http://localhost:1234", model="", system_prompt="p"
    )

    posts: list[dict] = []
    serve_payload: dict = {"models": []}

    def _handler(req, timeout):
        if req.get_method() == "POST":
            body = json.loads(req.data.decode("utf-8"))
            posts.append({"url": req.full_url, "body": body})
            return _FakeResponse({"status": "loaded"})
        return _FakeResponse(serve_payload)

    _install_fake_urlopen(monkeypatch, _handler)

    assert client.load_model("alpha") == "alpha"
    assert any(
        post["url"].endswith("/api/v1/models/load") and post["body"] == {"model": "alpha"}
        for post in posts
    )
    assert client.model == "alpha"


def test_load_model_raises_when_server_does_not_confirm(monkeypatch):
    client = LmStudioClient(
        base_url="http://localhost:1234", model="", system_prompt="p"
    )

    def _handler(req, timeout):
        if req.get_method() == "POST":
            return _FakeResponse({"status": "queued"})
        return _FakeResponse({"models": []})

    _install_fake_urlopen(monkeypatch, _handler)

    with pytest.raises(RuntimeError, match="did not confirm model load"):
        client.load_model("alpha")


def test_load_model_raises_without_url():
    client = LmStudioClient(base_url="", model="", system_prompt="p")
    with pytest.raises(RuntimeError, match="LLM URL is not configured"):
        client.load_model("alpha")


def test_load_model_raises_without_model_name():
    client = LmStudioClient(
        base_url="http://localhost:1234", model="", system_prompt="p"
    )
    with pytest.raises(RuntimeError, match="LLM model is not configured"):
        client.load_model("")
