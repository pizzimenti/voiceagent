from __future__ import annotations

import json
from urllib import error, request


class LmStudioClient:
    def __init__(self, base_url: str, model: str, system_prompt: str, timeout_seconds: int = 60) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.system_prompt = system_prompt
        self.timeout_seconds = timeout_seconds

    def complete(self, user_text: str) -> str:
        if not self.model:
            raise RuntimeError("LM_STUDIO_MODEL is not configured.")

        payload = {
            "model": self.model,
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

