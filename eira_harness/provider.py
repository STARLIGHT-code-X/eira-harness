"""OpenAI-compatible Chat Completions transport; no third-party runtime dependency."""
from __future__ import annotations

import http.client
import json
import time
from urllib.parse import urlsplit

from .security import HarnessError


class Provider:
    def __init__(self, model: str, base_url: str, api_key: str = "", timeout: int = 90):
        parsed = urlsplit(base_url)
        if not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise HarnessError("Model endpoint must be an HTTP(S) base URL without credentials, query, or fragment.")
        if parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in
                                               {"localhost", "127.0.0.1", "::1"}):
            raise HarnessError("Model endpoints require HTTPS, except for local loopback servers.")
        if not model:
            raise HarnessError("Set EIRA_MODEL or pass --model to choose a tool-capable model.")
        if parsed.hostname == "api.openai.com" and not api_key:
            raise HarnessError("Set OPENAI_API_KEY or EIRA_API_KEY before using the OpenAI endpoint.")
        self.parsed, self.model, self.api_key, self.timeout = parsed, model, api_key, timeout

    def complete(self, messages: list[dict], tools: list[dict]) -> tuple[dict, dict]:
        payload = json.dumps({"model": self.model, "messages": messages,
                              "tools": tools, "tool_choice": "auto"}, allow_nan=False).encode()
        headers = {"Content-Type": "application/json", "User-Agent": "eira-harness/0.1"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        for attempt in range(3):
            connection_type = http.client.HTTPSConnection if self.parsed.scheme == "https" else http.client.HTTPConnection
            conn = connection_type(self.parsed.hostname, self.parsed.port, timeout=self.timeout)
            try:
                conn.request("POST", self.parsed.path.rstrip("/") + "/chat/completions", payload, headers)
                response = conn.getresponse()
                raw = response.read(2_000_001)
                if response.status in (429, 500, 502, 503, 504) and attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                if response.status != 200:
                    # Never echo a provider response that might contain credentials or prompts.
                    raise HarnessError(f"Model endpoint returned HTTP {response.status}. Check credentials, model, quota, and endpoint.")
                if len(raw) > 2_000_000:
                    raise HarnessError("Model response exceeded 2 MB.")
                try:
                    data = json.loads(raw)
                    choice = data["choices"][0]
                    message = choice["message"]
                    if choice.get("finish_reason") in ("length", "content_filter"):
                        raise HarnessError("Model response was truncated or filtered; no tools were executed.")
                    if not isinstance(message, dict) or message.get("role") != "assistant":
                        raise ValueError()
                    content = message.get("content")
                    if content is not None and not isinstance(content, str):
                        raise ValueError()
                    calls = message.get("tool_calls") or []
                    if not isinstance(calls, list) or len(calls) > 32:
                        raise ValueError()
                    ids = set()
                    for call in calls:
                        if (call.get("type") != "function" or not isinstance(call.get("id"), str)
                                or not call["id"] or call["id"] in ids
                                or not isinstance(call.get("function", {}).get("name"), str)
                                or not isinstance(call["function"].get("arguments"), str)):
                            raise ValueError()
                        ids.add(call["id"])
                    result = {"role": "assistant", "content": content or message.get("refusal") or ""}
                    if calls:
                        result["tool_calls"] = calls
                    usage = data.get("usage")
                    return result, usage if isinstance(usage, dict) else {}
                except (ValueError, KeyError, IndexError, TypeError, AttributeError) as exc:
                    raise HarnessError("Model endpoint returned a malformed response; no tools were executed.") from exc
            except (OSError, http.client.HTTPException) as exc:
                # Do not automatically retry uncertain transport failures (possible duplicate billing).
                raise HarnessError("Model connection failed or timed out. Session is saved; retry explicitly.") from exc
            finally:
                conn.close()
        raise HarnessError("Model endpoint is unavailable.")
