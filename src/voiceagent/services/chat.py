from __future__ import annotations

import json
from urllib import error, request


class LmStudioClient:
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
            raise RuntimeError(f"{method} {url} failed: {exc}") from exc

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
        model_name = (model or self.model).strip()
        if not self.base_url:
            raise RuntimeError("LLM URL is not configured.")
        if not model_name:
            raise RuntimeError("LLM model is not configured.")

        self.unload_other_models(keep_model=model_name)
        if any(loaded_model == model_name for loaded_model in self.list_loaded_models()):
            self.model = model_name
            return model_name

        native_api_root = self._native_api_root()
        if not native_api_root:
            raise RuntimeError("LM Studio native API root could not be determined.")

        payload = {"model": model_name}
        response = self._json_request(f"{native_api_root}/models/load", payload=payload, method="POST")
        status = response.get("status")
        if status != "loaded":
            raise RuntimeError(f"LM Studio did not confirm model load for '{model_name}'.")

        self.model = model_name
        return model_name

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

    def complete(self, user_text: str) -> str:
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
            "stream": False,
        }
        body = json.dumps(payload).encode("utf-8")
        req = request.Request(
            f"{self.base_url}/chat/completions",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        try:
            with request.urlopen(req, timeout=self.timeout_seconds) as response:
                data = json.load(response)
        except error.URLError as exc:
            raise RuntimeError(f"LM Studio request failed: {exc}") from exc

        try:
            message = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError("LM Studio returned an unexpected response payload.") from exc

        message = message.strip()
        if not message:
            raise RuntimeError("LM Studio returned an empty response.")

        return message
