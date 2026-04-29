from __future__ import annotations

import json
import logging
import socket
from urllib import error, request


_LOGGER = logging.getLogger(__name__)


class LmStudioClient:
    """HTTP client for LM Studio's two API surfaces.

    Wraps `urllib.request` with project-specific semantics over the
    OpenAI-compatible `/v1/*` endpoints (`/models`, `/chat/completions`)
    and LM Studio's native `/api/v1/*` endpoints (`/models`,
    `/models/load`, `/models/unload`). Maintains a single "currently
    selected" model pointer that the controller writes through on
    selection changes; HTTP itself is stateless and re-resolves
    `self.model` on every call. `complete()` SSE-streams chat
    completions with optional per-chunk callbacks for live UI updates.
    """

    def __init__(self, base_url: str, model: str, system_prompt: str, timeout_seconds: int = 60) -> None:
        self.base_url = ""
        self.model = model
        self.system_prompt = system_prompt
        self.timeout_seconds = timeout_seconds
        self.set_base_url(base_url)

    @staticmethod
    def normalize_base_url(value: str) -> str:
        base_url = value.strip()
        if not base_url:
            return ""
        if "://" not in base_url:
            base_url = f"http://{base_url}"
        base_url = base_url.rstrip("/")
        if not base_url.endswith("/v1"):
            base_url = f"{base_url}/v1"
        return base_url

    def set_base_url(self, base_url: str) -> None:
        self.base_url = self.normalize_base_url(base_url)

    def _native_api_root(self) -> str:
        if not self.base_url:
            return ""
        if self.base_url.endswith("/v1"):
            return f"{self.base_url[:-3]}/api/v1"
        return f"{self.base_url}/api/v1"

    def _json_request(self, url: str, payload: dict | None = None, method: str = "GET") -> dict:
        body = None
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
        req = request.Request(
            url,
            data=body,
            headers={"Content-Type": "application/json"},
            method=method,
        )
        try:
            with request.urlopen(req, timeout=self.timeout_seconds) as response:
                return json.load(response)
        except error.HTTPError as exc:
            details = exc.read().decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"{method} {url} failed: HTTP {exc.code}. {details}".strip()) from exc
        except error.URLError as exc:
            raise RuntimeError(f"{method} {url} failed: {self._format_url_error(exc)}") from exc

    def _format_url_error(self, exc: error.URLError) -> str:
        reason = getattr(exc, "reason", exc)
        if isinstance(reason, (TimeoutError, socket.timeout)):
            return f"timed out after {self.timeout_seconds} seconds"
        return str(exc)

    def set_model(self, model: str) -> None:
        self.model = model.strip()

    def list_loaded_models(self) -> list[str]:
        if not self.base_url:
            raise RuntimeError("LLM URL is not configured.")

        return [model_key for model_key, _instance_id in self.list_loaded_model_instances()]

    def list_loaded_model_instances(self) -> list[tuple[str, str]]:
        native_api_root = self._native_api_root()
        if not native_api_root:
            raise RuntimeError("LM Studio native API root could not be determined.")

        data = self._json_request(f"{native_api_root}/models", method="GET")
        models = data.get("models", [])
        if not isinstance(models, list):
            raise RuntimeError("LM Studio returned an unexpected /api/v1/models payload.")

        loaded_instances: list[tuple[str, str]] = []
        for item in models:
            if not isinstance(item, dict):
                continue
            if item.get("type") != "llm":
                continue
            model_key = item.get("key")
            if not isinstance(model_key, str) or not model_key.strip():
                continue
            instances = item.get("loaded_instances", [])
            if not isinstance(instances, list):
                continue
            for instance in instances:
                if not isinstance(instance, dict):
                    continue
                instance_id = instance.get("id")
                if isinstance(instance_id, str) and instance_id.strip():
                    loaded_instances.append((model_key.strip(), instance_id.strip()))
        return loaded_instances

    def refresh_loaded_model(self) -> str:
        loaded_models = self.list_loaded_models()
        if not loaded_models:
            self.model = ""
            raise RuntimeError("No LLM is currently loaded on the server.")

        self.model = loaded_models[0]
        return self.model

    def list_models(self) -> list[str]:
        if not self.base_url:
            raise RuntimeError("LLM URL is not configured.")

        try:
            data = self._json_request(f"{self.base_url}/models", method="GET")
        except RuntimeError as exc:
            raise RuntimeError(f"Failed to fetch models from {self.base_url}: {exc}") from exc

        models = data.get("data", [])
        if not isinstance(models, list):
            raise RuntimeError("LLM server returned an unexpected /models payload.")

        ids: list[str] = []
        for item in models:
            if not isinstance(item, dict):
                continue
            model_id = item.get("id")
            if isinstance(model_id, str) and model_id.strip():
                ids.append(model_id.strip())
        if not ids:
            raise RuntimeError(f"No models were returned by {self.base_url}.")
        return ids

    def ensure_model(self) -> str:
        if self.model:
            return self.model

        return self.refresh_loaded_model()

    def load_model(self, model: str | None = None) -> str:
        """Switch the loaded LLM to ``model`` (or ``self.model``).

        Memory note: while a new model is loading, the previously
        selected model stays loaded too — we only unload it once
        ``/models/load`` confirms the new instance is ready. Peak
        memory is briefly 2x, but the trade is worth it because a
        failed load no longer evicts the user's working model and
        leaves them in an empty state.
        """
        model_name = (model or self.model).strip()
        if not self.base_url:
            raise RuntimeError("LLM URL is not configured.")
        if not model_name:
            raise RuntimeError("LLM model is not configured.")

        # Already loaded? Unload others (safe — the keep model is
        # confirmed available) and claim it as current.
        if any(loaded_model == model_name for loaded_model in self.list_loaded_models()):
            self.unload_other_models(keep_model=model_name)
            self.model = model_name
            return model_name

        native_api_root = self._native_api_root()
        if not native_api_root:
            raise RuntimeError("LM Studio native API root could not be determined.")

        # Snapshot loaded instances before the load so we can identify
        # the newly-loaded one as the diff after `/models/load` returns.
        # The request `model_name` is what the user typed; LM Studio's
        # canonical key (the one /api/v1/models reports) may differ
        # (alias vs fully-qualified key). Comparing pre/post lets us
        # keep the actual new instance regardless of name resolution.
        pre_load = set(self.list_loaded_model_instances())

        # Load FIRST. Previous models stay loaded until the server
        # confirms the new one — that way a failed load can't strand
        # the user with no working LLM.
        payload = {"model": model_name}
        response = self._json_request(f"{native_api_root}/models/load", payload=payload, method="POST")
        status = response.get("status")
        if status != "loaded":
            raise RuntimeError(f"LM Studio did not confirm model load for '{model_name}'.")

        # Identify the newly-loaded instance(s) by set-diff against the
        # pre-load snapshot. If LM Studio reports a different canonical
        # key than what we requested (alias resolution), we use THAT key
        # as the unload-others keep filter — otherwise `unload_other_models`
        # would evict the model we just loaded.
        post_load = self.list_loaded_model_instances()
        new_instances = [pair for pair in post_load if pair not in pre_load]
        if new_instances:
            keep_key = new_instances[0][0]
        elif post_load:
            # Empty diff but something is loaded — `/models/load` reported
            # "loaded" without adding a fresh instance. Most likely the
            # alias resolved to a model that was already loaded under its
            # canonical key. Disambiguation cascade:
            #   1. Requested name matches a loaded key exactly → use it.
            #   2. Exactly one LLM is loaded → unambiguous; use that key.
            #   3. Requested name appears as a case-insensitive substring
            #      of exactly one canonical key → assume that's the alias
            #      target. Common pattern: alias "qwen" maps to canonical
            #      "Qwen/qwen2.5-coder-14b-instruct".
            #   4. Otherwise: multiple loaded, no fuzzy match → raise so
            #      the caller doesn't silently end up on the wrong model.
            #      Better to surface the ambiguity than guess at random.
            loaded_keys = [k for k, _ in post_load]
            if model_name in loaded_keys:
                keep_key = model_name
            elif len(loaded_keys) == 1:
                keep_key = loaded_keys[0]
            else:
                needle = model_name.lower()
                fuzzy_matches = [k for k in loaded_keys if needle in k.lower()]
                if len(fuzzy_matches) == 1:
                    keep_key = fuzzy_matches[0]
                else:
                    raise RuntimeError(
                        f"LM Studio confirmed load for '{model_name}' but did "
                        f"not create a new instance, and {len(loaded_keys)} "
                        f"LLMs are already loaded under canonical keys "
                        f"({', '.join(sorted(set(loaded_keys)))}). Cannot "
                        f"determine which model the alias resolves to."
                    )
        else:
            # Server confirmed the load but nothing is loaded. Defensive
            # — treat as failure so the caller doesn't think we have a
            # working LLM.
            raise RuntimeError(
                f"LM Studio confirmed load for '{model_name}' but no model is loaded."
            )

        # Promote the new model FIRST, then attempt the cleanup unload.
        # If the cleanup fails, the load itself still stands — losing
        # the new selection because of an unload error would be the
        # very regression P2 #3 was meant to prevent.
        self.model = keep_key
        try:
            self.unload_other_models(keep_model=keep_key)
        except Exception:
            _LOGGER.exception(
                "unload_other_models failed after successful load (model=%s); "
                "new model is current but stale instances may still be loaded",
                keep_key,
            )
        return keep_key

    def unload_model_instance(self, instance_id: str) -> None:
        native_api_root = self._native_api_root()
        if not native_api_root:
            raise RuntimeError("LM Studio native API root could not be determined.")
        self._json_request(
            f"{native_api_root}/models/unload",
            payload={"instance_id": instance_id},
            method="POST",
        )

    def unload_other_models(self, keep_model: str | None = None) -> None:
        keep_value = (keep_model or "").strip()
        for model_key, instance_id in self.list_loaded_model_instances():
            if keep_value and model_key == keep_value:
                continue
            self.unload_model_instance(instance_id)

    def unload_all_models(self) -> None:
        self.unload_other_models(keep_model=None)
        self.model = ""

    def fetch_loaded_context_length(self) -> int:
        """Return the context-window size of the currently-loaded LLM in
        tokens, via LM Studio's native `/api/v1/models` endpoint. Returns
        0 when nothing is loaded or the field is absent (so callers can
        treat 0 as "unknown" rather than a divide-by-zero hazard)."""
        native_api_root = self._native_api_root()
        if not native_api_root:
            return 0
        try:
            data = self._json_request(f"{native_api_root}/models", method="GET")
        except RuntimeError:
            return 0
        models = data.get("models", [])
        if not isinstance(models, list):
            return 0
        for item in models:
            if not isinstance(item, dict) or item.get("type") != "llm":
                continue
            model_key = item.get("key")
            if not isinstance(model_key, str) or model_key.strip() != self.model:
                continue
            instances = item.get("loaded_instances", [])
            if not isinstance(instances, list):
                continue
            for instance in instances:
                if not isinstance(instance, dict):
                    continue
                ctx = instance.get("loaded_context_length") or instance.get("context_length")
                if isinstance(ctx, int) and ctx > 0:
                    return ctx
        return 0

    def complete(
        self,
        user_text: str,
        *,
        on_content_chunk=None,
        on_thinking_chunk=None,
        on_usage=None,
    ) -> str:
        """Stream a chat completion. Returns the full assembled answer
        text. Optional callbacks fire from this thread as chunks arrive:

        - `on_content_chunk(text)` — incremental answer text
        - `on_thinking_chunk(text)` — incremental reasoning_content for
          R1-style thinking models (LM Studio exposes thinking on the
          `delta.reasoning_content` field of each SSE chunk)
        - `on_usage(usage_dict)` — fires once on the final chunk that
          carries `usage` (driven by `stream_options.include_usage`)

        Switching to streaming closes the v0.9.x timeout class —
        `timeout_seconds` is now a per-read gap, not a total-response
        cap, so multi-minute generations are fine as long as tokens
        keep arriving."""
        if not self.base_url:
            raise RuntimeError("LLM URL is not configured.")
        model = self.ensure_model()

        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": self.system_prompt},
                {"role": "user", "content": user_text},
            ],
            "temperature": 0.2,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        body = json.dumps(payload).encode("utf-8")
        req = request.Request(
            f"{self.base_url}/chat/completions",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        accumulated_content: list[str] = []
        accumulated_thinking: list[str] = []

        try:
            with request.urlopen(req, timeout=self.timeout_seconds) as response:
                for raw_line in response:
                    line = raw_line.strip()
                    if not line or not line.startswith(b"data:"):
                        continue
                    payload_bytes = line[5:].strip()
                    if payload_bytes == b"[DONE]":
                        break
                    try:
                        chunk = json.loads(payload_bytes)
                    except (json.JSONDecodeError, ValueError):
                        continue

                    usage = chunk.get("usage")
                    if isinstance(usage, dict) and on_usage is not None:
                        on_usage(usage)

                    choices = chunk.get("choices")
                    if not isinstance(choices, list) or not choices:
                        continue
                    delta = choices[0].get("delta") if isinstance(choices[0], dict) else None
                    if not isinstance(delta, dict):
                        continue

                    content_chunk = delta.get("content")
                    if isinstance(content_chunk, str) and content_chunk:
                        accumulated_content.append(content_chunk)
                        if on_content_chunk is not None:
                            on_content_chunk(content_chunk)

                    thinking_chunk = delta.get("reasoning_content")
                    if isinstance(thinking_chunk, str) and thinking_chunk:
                        accumulated_thinking.append(thinking_chunk)
                        if on_thinking_chunk is not None:
                            on_thinking_chunk(thinking_chunk)
        except error.URLError as exc:
            raise RuntimeError(f"LM Studio request failed: {self._format_url_error(exc)}") from exc

        message = "".join(accumulated_content)

        message = message.strip()
        if not message:
            raise RuntimeError("LM Studio returned an empty response.")

        return message
