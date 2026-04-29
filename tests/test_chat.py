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


class _FakeSSEResponse:
    """Streaming-mode `urlopen` stand-in for `complete()` tests.

    Iterates SSE-formatted lines (`data: {...}\\n`) terminated by
    `data: [DONE]\\n`. Each chunk is a chat-completion delta dict
    (matching the OpenAI / LM Studio shape).
    """

    def __init__(self, chunks: list[dict]) -> None:
        lines: list[bytes] = []
        for chunk in chunks:
            lines.append(f"data: {json.dumps(chunk)}\n".encode("utf-8"))
        lines.append(b"data: [DONE]\n")
        self._lines = lines

    def __enter__(self) -> "_FakeSSEResponse":
        return self

    def __exit__(self, *_exc: object) -> bool:
        return False

    def __iter__(self):
        return iter(self._lines)


def _content_chunk(text: str) -> dict:
    return {"choices": [{"delta": {"content": text}}]}


def _thinking_chunk(text: str) -> dict:
    return {"choices": [{"delta": {"reasoning_content": text}}]}


def _usage_chunk(prompt: int, completion: int) -> dict:
    return {
        "choices": [],
        "usage": {
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "total_tokens": prompt + completion,
        },
    }


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
        return _FakeSSEResponse([
            _content_chunk("  hi"),
            _content_chunk(" there  "),
        ])

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
    assert captured["body"]["stream"] is True
    assert captured["body"]["stream_options"] == {"include_usage": True}


def test_complete_streams_content_chunks_to_callback(monkeypatch):
    client = LmStudioClient(
        base_url="http://localhost:1234", model="m", system_prompt="p"
    )
    received: list[str] = []
    _install_fake_urlopen(
        monkeypatch,
        lambda req, timeout: _FakeSSEResponse([
            _content_chunk("Hello"),
            _content_chunk(", "),
            _content_chunk("world"),
        ]),
    )
    result = client.complete("hi", on_content_chunk=received.append)
    assert result == "Hello, world"
    assert received == ["Hello", ", ", "world"]


def test_complete_streams_thinking_chunks_to_callback(monkeypatch):
    client = LmStudioClient(
        base_url="http://localhost:1234", model="m", system_prompt="p"
    )
    thinking: list[str] = []
    content: list[str] = []
    _install_fake_urlopen(
        monkeypatch,
        lambda req, timeout: _FakeSSEResponse([
            _thinking_chunk("Let me consider this."),
            _thinking_chunk(" Two plus two."),
            _content_chunk("4"),
        ]),
    )
    result = client.complete(
        "ignored",
        on_content_chunk=content.append,
        on_thinking_chunk=thinking.append,
    )
    assert result == "4"
    assert thinking == ["Let me consider this.", " Two plus two."]
    assert content == ["4"]


def test_complete_invokes_on_usage_with_final_usage_dict(monkeypatch):
    client = LmStudioClient(
        base_url="http://localhost:1234", model="m", system_prompt="p"
    )
    captured_usage: list[dict] = []
    _install_fake_urlopen(
        monkeypatch,
        lambda req, timeout: _FakeSSEResponse([
            _content_chunk("ok"),
            _usage_chunk(prompt=120, completion=8),
        ]),
    )
    client.complete("hi", on_usage=captured_usage.append)
    assert captured_usage == [
        {"prompt_tokens": 120, "completion_tokens": 8, "total_tokens": 128},
    ]


def test_complete_ignores_malformed_sse_payloads(monkeypatch):
    """Garbage data: lines, blank deltas, or non-JSON payloads are
    silently skipped — the stream continues and content keeps
    accumulating from valid chunks."""
    client = LmStudioClient(
        base_url="http://localhost:1234", model="m", system_prompt="p"
    )

    class _MixedSSE:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def __iter__(self):
            yield b"\n"
            yield b": comment line\n"
            yield b"data: not-json\n"
            yield b"data: " + json.dumps({"choices": [{}]}).encode() + b"\n"
            yield b"data: " + json.dumps(_content_chunk("ok")).encode() + b"\n"
            yield b"data: [DONE]\n"

    _install_fake_urlopen(monkeypatch, lambda req, timeout: _MixedSSE())
    assert client.complete("hi") == "ok"


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


def test_complete_no_chunks_raises_empty_response(monkeypatch):
    """A stream that emits zero content deltas is treated as an empty
    response — the user-visible failure mode is identical to the
    pre-streaming `empty content` case."""
    client = LmStudioClient(
        base_url="http://localhost:1234", model="m", system_prompt="p"
    )
    _install_fake_urlopen(
        monkeypatch, lambda req, timeout: _FakeSSEResponse([]),
    )
    with pytest.raises(RuntimeError, match="empty response"):
        client.complete("hi")


def test_complete_only_whitespace_chunks_raises_empty_response(monkeypatch):
    """All-whitespace deltas accumulate to a strippable string and
    fail the empty-response guard the same way."""
    client = LmStudioClient(
        base_url="http://localhost:1234", model="m", system_prompt="p"
    )
    _install_fake_urlopen(
        monkeypatch,
        lambda req, timeout: _FakeSSEResponse([
            _content_chunk("   "),
            _content_chunk("\n"),
        ]),
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
    pre_load_payload = {"models": []}
    post_load_payload = {
        "models": [
            {
                "type": "llm",
                "key": "alpha",
                "loaded_instances": [{"id": "a1"}],
            }
        ]
    }
    load_fired = {"yes": False}

    def _handler(req, timeout):
        if req.get_method() == "POST":
            body = json.loads(req.data.decode("utf-8"))
            posts.append({"url": req.full_url, "body": body})
            if req.full_url.endswith("/api/v1/models/load"):
                load_fired["yes"] = True
            return _FakeResponse({"status": "loaded"})
        return _FakeResponse(post_load_payload if load_fired["yes"] else pre_load_payload)

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


def test_load_model_keeps_previous_loaded_when_new_load_fails(monkeypatch):
    """Regression for P2 #3: a failed `/models/load` must NOT evict
    the previously-loaded model. The reorder is load-first-then-unload
    precisely so that a queued/refused load leaves the user's working
    LLM intact rather than dropping them into an empty state."""
    client = LmStudioClient(
        base_url="http://localhost:1234", model="previous", system_prompt="p"
    )

    serve_payload = {
        "models": [
            {
                "type": "llm",
                "key": "previous",
                "loaded_instances": [{"id": "p1"}],
            }
        ]
    }
    posts: list[dict] = []
    unloads: list[str] = []

    def _handler(req, timeout):
        if req.get_method() == "POST":
            url = req.full_url
            body = json.loads(req.data.decode("utf-8"))
            posts.append({"url": url, "body": body})
            if url.endswith("/api/v1/models/load"):
                # Simulate a load that does not confirm.
                return _FakeResponse({"status": "queued"})
            if url.endswith("/api/v1/models/unload"):
                unloads.append(body.get("instance_id", ""))
                return _FakeResponse({"status": "ok"})
            return _FakeResponse({"status": "ok"})
        return _FakeResponse(serve_payload)

    _install_fake_urlopen(monkeypatch, _handler)

    with pytest.raises(RuntimeError, match="did not confirm model load"):
        client.load_model("alpha")

    # The previous selection must NOT have been evicted: no
    # /models/unload call should have fired at all.
    assert unloads == [], (
        f"unload_other_models ran before load was confirmed: {unloads}"
    )
    # And `self.model` must not have been overwritten with the failed
    # candidate — the working model pointer survives.
    assert client.model == "previous"


def test_load_model_unloads_previous_only_after_load_confirmed(monkeypatch):
    """The new ordering: a successful load fires the load POST FIRST,
    then unloads the prior selection. Order matters — if these flipped
    back, P2 #3 regresses."""
    client = LmStudioClient(
        base_url="http://localhost:1234", model="previous", system_prompt="p"
    )

    # Pre-load: only `previous` is loaded. Post-load: `alpha` joins
    # `previous` with a fresh instance id `a1`. The set-diff identifies
    # `a1` as the new instance; the unload step then evicts `p1` while
    # keeping `alpha`.
    pre_load_payload = {
        "models": [
            {
                "type": "llm",
                "key": "previous",
                "loaded_instances": [{"id": "p1"}],
            }
        ]
    }
    post_load_payload = {
        "models": [
            {
                "type": "llm",
                "key": "previous",
                "loaded_instances": [{"id": "p1"}],
            },
            {
                "type": "llm",
                "key": "alpha",
                "loaded_instances": [{"id": "a1"}],
            },
        ]
    }
    call_log: list[str] = []
    unloaded_instances: list[str] = []
    load_fired = {"yes": False}

    def _handler(req, timeout):
        if req.get_method() == "POST":
            url = req.full_url
            body = json.loads(req.data.decode("utf-8"))
            if url.endswith("/api/v1/models/load"):
                call_log.append("load")
                load_fired["yes"] = True
                return _FakeResponse({"status": "loaded"})
            if url.endswith("/api/v1/models/unload"):
                call_log.append("unload")
                unloaded_instances.append(body["instance_id"])
                return _FakeResponse({"status": "ok"})
            return _FakeResponse({"status": "ok"})
        call_log.append("list")
        return _FakeResponse(
            post_load_payload if load_fired["yes"] else pre_load_payload
        )

    _install_fake_urlopen(monkeypatch, _handler)

    assert client.load_model("alpha") == "alpha"
    # The first POST must be the load, and any unload must happen
    # strictly after a confirmed load.
    post_calls = [c for c in call_log if c in ("load", "unload")]
    assert post_calls and post_calls[0] == "load", (
        f"unload fired before /models/load: {call_log}"
    )
    # And the previous model's instance got unloaded after confirm.
    assert unloaded_instances == ["p1"]
    assert client.model == "alpha"


def test_load_model_keeps_canonical_key_when_alias_resolves(monkeypatch):
    """When `/api/v1/models/load` accepts an alias and reports the
    canonical key on the post-load `/api/v1/models` listing, the unload
    step must keep by the canonical key — not the requested alias —
    or it evicts the just-loaded instance.

    The pre/post snapshot diff identifies the new instance regardless
    of name resolution; this test locks that behavior so a future
    "simplify" doesn't reintroduce the alias-bypass bug Codex flagged.
    """
    client = LmStudioClient(
        base_url="http://localhost:1234", model="previous", system_prompt="p"
    )

    # Pre-load: only `previous` is loaded.
    pre_load_payload = {
        "models": [
            {
                "type": "llm",
                "key": "previous",
                "loaded_instances": [{"id": "p1"}],
            }
        ]
    }
    # Post-load: server resolves the requested alias `nemo` to canonical
    # key `org/nemotron-3-nano-4b`, returns it under that name with a
    # NEW instance id `n1`. `previous` is still there too — the unload
    # step has not run yet at this point.
    post_load_payload = {
        "models": [
            {
                "type": "llm",
                "key": "previous",
                "loaded_instances": [{"id": "p1"}],
            },
            {
                "type": "llm",
                "key": "org/nemotron-3-nano-4b",
                "loaded_instances": [{"id": "n1"}],
            },
        ]
    }

    list_call_count = {"n": 0}
    unloaded_instances: list[str] = []
    load_fired = {"yes": False}

    def _handler(req, timeout):
        if req.get_method() == "POST":
            url = req.full_url
            body = json.loads(req.data.decode("utf-8"))
            if url.endswith("/api/v1/models/load"):
                load_fired["yes"] = True
                return _FakeResponse({"status": "loaded"})
            if url.endswith("/api/v1/models/unload"):
                unloaded_instances.append(body["instance_id"])
                return _FakeResponse({"status": "ok"})
            return _FakeResponse({"status": "ok"})
        # GET on /api/v1/models — returns pre-load until /models/load
        # has fired, then post-load. This sidesteps having to count
        # exactly which GET is the pre-snapshot vs the post-snapshot
        # (`list_loaded_models()` from the "already loaded" check
        # also hits this path before the load).
        list_call_count["n"] += 1
        return _FakeResponse(
            post_load_payload if load_fired["yes"] else pre_load_payload
        )

    _install_fake_urlopen(monkeypatch, _handler)

    # Caller asks for the alias `nemo`; server resolves to a different
    # canonical key. Without the snapshot-diff fix, the keep filter
    # would be `nemo`, which doesn't match `org/nemotron-3-nano-4b`,
    # and the just-loaded `n1` would get unloaded.
    assert client.load_model("nemo") == "org/nemotron-3-nano-4b"
    # The previous model `p1` got unloaded; the just-loaded `n1` did NOT.
    assert "p1" in unloaded_instances
    assert "n1" not in unloaded_instances
    # And `client.model` is now the canonical key, not the alias —
    # subsequent `complete()` requests must use what the server
    # actually has.
    assert client.model == "org/nemotron-3-nano-4b"


def test_load_model_uses_response_instance_id_to_resolve_alias(monkeypatch):
    """When `/api/v1/models/load` returns an `instance_id` for the
    resolved instance, look up its canonical key directly. Bypasses
    the diff / substring heuristics — the server has already told
    us exactly which instance the alias resolved to.
    """
    client = LmStudioClient(
        base_url="http://localhost:1234", model="initial", system_prompt="p"
    )

    # Multiple loaded LLMs; the alias is "qwen" but the canonical key
    # is something the substring heuristic could plausibly match more
    # than once. Without the instance_id path, this would either
    # raise (multi-match) or guess. With it, the lookup is exact.
    same_payload = {
        "models": [
            {
                "type": "llm",
                "key": "Qwen/qwen2.5-7b",
                "loaded_instances": [{"id": "q7"}],
            },
            {
                "type": "llm",
                "key": "Qwen/qwen2.5-14b",
                "loaded_instances": [{"id": "q14"}],
            },
        ]
    }
    unloaded: list[str] = []

    def _handler(req, timeout):
        if req.get_method() == "POST":
            url = req.full_url
            body = json.loads(req.data.decode("utf-8"))
            if url.endswith("/api/v1/models/load"):
                # Server returns the canonical instance_id for the
                # resolved alias.
                return _FakeResponse({"status": "loaded", "instance_id": "q14"})
            if url.endswith("/api/v1/models/unload"):
                unloaded.append(body["instance_id"])
                return _FakeResponse({"status": "ok"})
            return _FakeResponse({"status": "ok"})
        return _FakeResponse(same_payload)

    _install_fake_urlopen(monkeypatch, _handler)

    # The response.instance_id == "q14" → canonical key "Qwen/qwen2.5-14b".
    # Substring "qwen" would otherwise match BOTH 7b and 14b and raise.
    assert client.load_model("qwen") == "Qwen/qwen2.5-14b"
    assert client.model == "Qwen/qwen2.5-14b"
    # The 7b instance gets unloaded; the 14b stays.
    assert unloaded == ["q7"]


def test_load_model_alias_to_already_loaded_skips_unload(monkeypatch):
    """When `/models/load` reports success but no new instance appears
    in `/api/v1/models` (alias resolves to a model that was already
    loaded under its canonical key), skip the unload step entirely —
    a key-mismatch unload would otherwise evict the very instance the
    alias points at, leaving no LLM loaded after a "successful" load.
    """
    client = LmStudioClient(
        base_url="http://localhost:1234",
        model="org/canonical-model",
        system_prompt="p",
    )

    same_payload = {
        "models": [
            {
                "type": "llm",
                "key": "org/canonical-model",
                "loaded_instances": [{"id": "c1"}],
            }
        ]
    }
    unloaded: list[str] = []

    def _handler(req, timeout):
        if req.get_method() == "POST":
            url = req.full_url
            body = json.loads(req.data.decode("utf-8"))
            if url.endswith("/api/v1/models/load"):
                return _FakeResponse({"status": "loaded"})
            if url.endswith("/api/v1/models/unload"):
                unloaded.append(body["instance_id"])
                return _FakeResponse({"status": "ok"})
            return _FakeResponse({"status": "ok"})
        # Same payload pre and post — load was a no-op because the
        # alias resolved to an already-loaded canonical key.
        return _FakeResponse(same_payload)

    _install_fake_urlopen(monkeypatch, _handler)

    # User asks for the alias `canonical`; server resolves to existing
    # `org/canonical-model` without spawning a new instance.
    result = client.load_model("canonical")

    # Skip-unload-when-empty-diff invariant: the canonical instance
    # must NOT be unloaded, and the model is now whatever was already
    # loaded.
    assert "c1" not in unloaded
    assert unloaded == []
    assert result == "org/canonical-model"
    assert client.model == "org/canonical-model"


def test_load_model_empty_diff_with_multiple_loaded_raises_on_ambiguity(monkeypatch):
    """Empty post-load diff with multiple LLMs already loaded and no
    name match — the previous heuristic (`loaded_keys[0]`) would
    silently switch the client to an arbitrary model. Now: raise so
    the ambiguity is surfaced rather than papered over.
    """
    client = LmStudioClient(
        base_url="http://localhost:1234", model="initial", system_prompt="p"
    )

    # Two unrelated LLMs already loaded; alias `mystery` doesn't match
    # either canonical key.
    same_payload = {
        "models": [
            {
                "type": "llm",
                "key": "alpha",
                "loaded_instances": [{"id": "a1"}],
            },
            {
                "type": "llm",
                "key": "beta",
                "loaded_instances": [{"id": "b1"}],
            },
        ]
    }

    def _handler(req, timeout):
        if req.get_method() == "POST":
            url = req.full_url
            if url.endswith("/api/v1/models/load"):
                return _FakeResponse({"status": "loaded"})
            return _FakeResponse({"status": "ok"})
        return _FakeResponse(same_payload)

    _install_fake_urlopen(monkeypatch, _handler)

    with pytest.raises(RuntimeError, match="Cannot determine which model"):
        client.load_model("mystery")
    # Critical: we did NOT silently overwrite client.model with an
    # arbitrary loaded key.
    assert client.model == "initial"


def test_load_model_empty_diff_fuzzy_match_resolves_alias(monkeypatch):
    """Empty post-load diff + multiple loaded LLMs + the requested
    alias appears as a case-insensitive substring of exactly one
    canonical key → use that one. Common alias-resolution case
    (e.g. 'qwen' → 'Qwen/qwen2.5-coder-14b-instruct') that would
    otherwise raise on ambiguity from round 3.
    """
    client = LmStudioClient(
        base_url="http://localhost:1234", model="initial", system_prompt="p"
    )
    same_payload = {
        "models": [
            {
                "type": "llm",
                "key": "Qwen/qwen2.5-coder-14b-instruct",
                "loaded_instances": [{"id": "q1"}],
            },
            {
                "type": "llm",
                "key": "meta/llama-3.1-8b",
                "loaded_instances": [{"id": "l1"}],
            },
        ]
    }
    unloaded: list[str] = []

    def _handler(req, timeout):
        if req.get_method() == "POST":
            url = req.full_url
            body = json.loads(req.data.decode("utf-8"))
            if url.endswith("/api/v1/models/load"):
                return _FakeResponse({"status": "loaded"})
            if url.endswith("/api/v1/models/unload"):
                unloaded.append(body["instance_id"])
                return _FakeResponse({"status": "ok"})
            return _FakeResponse({"status": "ok"})
        return _FakeResponse(same_payload)

    _install_fake_urlopen(monkeypatch, _handler)

    # Alias 'qwen' substring-matches exactly one canonical key — use it.
    assert client.load_model("qwen") == "Qwen/qwen2.5-coder-14b-instruct"
    assert client.model == "Qwen/qwen2.5-coder-14b-instruct"
    # The OTHER loaded model gets unloaded (we kept by Qwen's key).
    assert unloaded == ["l1"]
    assert "q1" not in unloaded


def test_load_model_empty_diff_fuzzy_match_multiple_hits_raises(monkeypatch):
    """If the alias substring-matches multiple canonical keys, that's
    ambiguous — raise rather than guess. Distinct from the
    no-fuzzy-match case but the resolution is the same: surface the
    ambiguity rather than silently picking one.
    """
    client = LmStudioClient(
        base_url="http://localhost:1234", model="initial", system_prompt="p"
    )
    same_payload = {
        "models": [
            {
                "type": "llm",
                "key": "Qwen/qwen2.5-7b",
                "loaded_instances": [{"id": "q7"}],
            },
            {
                "type": "llm",
                "key": "Qwen/qwen2.5-14b",
                "loaded_instances": [{"id": "q14"}],
            },
        ]
    }

    def _handler(req, timeout):
        if req.get_method() == "POST" and req.full_url.endswith("/api/v1/models/load"):
            return _FakeResponse({"status": "loaded"})
        if req.get_method() == "POST":
            return _FakeResponse({"status": "ok"})
        return _FakeResponse(same_payload)

    _install_fake_urlopen(monkeypatch, _handler)

    with pytest.raises(RuntimeError, match="Cannot determine which model"):
        client.load_model("qwen")
    assert client.model == "initial"


def test_load_model_empty_diff_with_single_loaded_promotes_unambiguous(monkeypatch):
    """Empty post-load diff with exactly one LLM loaded is
    unambiguous — promote that one. This covers the common case
    where an alias points at the only loaded model and the load was
    a no-op.
    """
    client = LmStudioClient(
        base_url="http://localhost:1234", model="initial", system_prompt="p"
    )
    same_payload = {
        "models": [
            {
                "type": "llm",
                "key": "org/canonical-only-loaded",
                "loaded_instances": [{"id": "x1"}],
            }
        ]
    }
    unloaded: list[str] = []

    def _handler(req, timeout):
        if req.get_method() == "POST":
            url = req.full_url
            body = json.loads(req.data.decode("utf-8"))
            if url.endswith("/api/v1/models/load"):
                return _FakeResponse({"status": "loaded"})
            if url.endswith("/api/v1/models/unload"):
                unloaded.append(body["instance_id"])
                return _FakeResponse({"status": "ok"})
            return _FakeResponse({"status": "ok"})
        return _FakeResponse(same_payload)

    _install_fake_urlopen(monkeypatch, _handler)

    assert client.load_model("alias-for-only-one") == "org/canonical-only-loaded"
    assert client.model == "org/canonical-only-loaded"
    # The lone loaded instance was NOT unloaded.
    assert "x1" not in unloaded


def test_load_model_promotes_before_unload_so_cleanup_error_does_not_rollback(monkeypatch):
    """If `/models/load` succeeds but the subsequent `unload_other_models`
    cleanup raises (network blip, server bug, anything), the load itself
    still stands — `self.model` is the new key and `load_model()` returns
    success rather than propagating the cleanup failure as a load failure.
    """
    client = LmStudioClient(
        base_url="http://localhost:1234", model="previous", system_prompt="p"
    )

    pre_load_payload = {
        "models": [
            {
                "type": "llm",
                "key": "previous",
                "loaded_instances": [{"id": "p1"}],
            }
        ]
    }
    post_load_payload = {
        "models": [
            {
                "type": "llm",
                "key": "previous",
                "loaded_instances": [{"id": "p1"}],
            },
            {
                "type": "llm",
                "key": "alpha",
                "loaded_instances": [{"id": "a1"}],
            },
        ]
    }
    load_fired = {"yes": False}

    def _handler(req, timeout):
        if req.get_method() == "POST":
            url = req.full_url
            if url.endswith("/api/v1/models/load"):
                load_fired["yes"] = True
                return _FakeResponse({"status": "loaded"})
            if url.endswith("/api/v1/models/unload"):
                # Simulate a cleanup-side server error.
                raise error.URLError(reason=ConnectionResetError("transient"))
            return _FakeResponse({"status": "ok"})
        return _FakeResponse(
            post_load_payload if load_fired["yes"] else pre_load_payload
        )

    _install_fake_urlopen(monkeypatch, _handler)

    # Load itself returns success; the unload-cleanup error is logged,
    # not raised.
    assert client.load_model("alpha") == "alpha"
    assert client.model == "alpha"


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


# --- fetch_loaded_context_length -----------------------------------------


def test_fetch_loaded_context_length_returns_loaded_context(monkeypatch):
    client = LmStudioClient(
        base_url="http://localhost:1234",
        model="my-model",
        system_prompt="p",
    )
    _install_fake_urlopen(
        monkeypatch,
        lambda req, timeout: _FakeResponse({
            "models": [
                {
                    "type": "llm",
                    "key": "my-model",
                    "loaded_instances": [
                        {"id": "i1", "loaded_context_length": 32768},
                    ],
                },
            ],
        }),
    )
    assert client.fetch_loaded_context_length() == 32768


def test_fetch_loaded_context_length_falls_back_to_context_length(monkeypatch):
    client = LmStudioClient(
        base_url="http://localhost:1234",
        model="my-model",
        system_prompt="p",
    )
    _install_fake_urlopen(
        monkeypatch,
        lambda req, timeout: _FakeResponse({
            "models": [
                {
                    "type": "llm",
                    "key": "my-model",
                    "loaded_instances": [
                        {"id": "i1", "context_length": 8192},
                    ],
                },
            ],
        }),
    )
    assert client.fetch_loaded_context_length() == 8192


def test_fetch_loaded_context_length_skips_non_matching_models(monkeypatch):
    client = LmStudioClient(
        base_url="http://localhost:1234",
        model="my-model",
        system_prompt="p",
    )
    _install_fake_urlopen(
        monkeypatch,
        lambda req, timeout: _FakeResponse({
            "models": [
                {
                    "type": "llm",
                    "key": "different-model",
                    "loaded_instances": [
                        {"id": "i1", "loaded_context_length": 99999},
                    ],
                },
            ],
        }),
    )
    assert client.fetch_loaded_context_length() == 0


def test_fetch_loaded_context_length_returns_zero_when_endpoint_fails(monkeypatch):
    client = LmStudioClient(
        base_url="http://localhost:1234",
        model="my-model",
        system_prompt="p",
    )

    def _raise(req, timeout):
        raise error.URLError(reason=ConnectionRefusedError("nope"))

    _install_fake_urlopen(monkeypatch, _raise)
    assert client.fetch_loaded_context_length() == 0


def test_fetch_loaded_context_length_returns_zero_without_base_url():
    client = LmStudioClient(base_url="", model="m", system_prompt="p")
    assert client.fetch_loaded_context_length() == 0
