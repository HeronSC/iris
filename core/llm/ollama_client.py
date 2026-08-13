from __future__ import annotations

import json
import socket
from typing import Any
from urllib import error as urllib_error, request


class OllamaClientError(Exception):
    pass


class OllamaClient:
    def __init__(self, base_url: str, model: str, timeout_seconds: float | None = 30.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout_seconds = timeout_seconds

    def generate(self, system_prompt: str, user_prompt: str) -> str:
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "stream": False,
        }

        data = json.dumps(payload).encode("utf-8")
        req = request.Request(
            f"{self.base_url}/api/chat",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        try:
            with request.urlopen(req, timeout=self.timeout_seconds) as response:
                response_text = response.read().decode("utf-8")
        except urllib_error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise OllamaClientError(
                f"Ollama returned HTTP {exc.code}: {detail}"
            ) from exc
        except urllib_error.URLError as exc:
            if self._is_timeout_error(exc):
                raise OllamaClientError(f"Ollama request timed out: {exc}") from exc
            raise OllamaClientError(f"Ollama connection failed: {exc}") from exc
        except TimeoutError as exc:
            raise OllamaClientError(f"Ollama request timed out: {exc}") from exc
        except socket.timeout as exc:
            raise OllamaClientError(f"Ollama request timed out: {exc}") from exc
        except OSError as exc:
            raise OllamaClientError(f"Ollama connection failed: {exc}") from exc

        if not response_text.strip():
            raise OllamaClientError("Ollama returned an empty response")

        try:
            parsed: dict[str, Any] = json.loads(response_text)
        except json.JSONDecodeError as error:
            raise OllamaClientError(f"Invalid JSON response from Ollama: {error}") from error

        message = parsed.get("message", {})
        content = message.get("content", "")
        if not isinstance(content, str) or not content.strip():
            raise OllamaClientError("Ollama response did not contain usable content")
        return content.strip()

    def _is_timeout_error(self, error_obj: urllib_error.URLError) -> bool:
        reason = getattr(error_obj, "reason", None)
        if isinstance(reason, TimeoutError):
            return True
        if isinstance(reason, socket.timeout):
            return True
        text = str(reason or error_obj).lower()
        return "timed out" in text or "timeout" in text
