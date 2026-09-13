"""Validated model-provider transports."""
from __future__ import annotations

import http.client
import json
import os
import re
import time
from urllib.parse import urlsplit

from . import network
from .security import HarnessError


def request_bytes(*args, **kwargs):
    """Indirection keeps transport tests able to replace either boundary."""
    return network.request_bytes(*args, **kwargs)


PROVIDER_PROFILES = {
    "openai": {"name": "openai", "label": "OpenAI", "api_key_env": "OPENAI_API_KEY",
               "default_base_url": "https://api.openai.com/v1", "transport": "openai", "requires_api_key": True},
    "anthropic": {"name": "anthropic", "label": "Anthropic Messages", "api_key_env": "ANTHROPIC_API_KEY",
                  "default_base_url": "https://api.anthropic.com/v1", "transport": "anthropic", "requires_api_key": True},
    "openrouter": {"name": "openrouter", "label": "OpenRouter", "api_key_env": "OPENROUTER_API_KEY",
                   "default_base_url": "https://openrouter.ai/api/v1", "transport": "openai", "requires_api_key": True},
    "gemini": {"name": "gemini", "label": "Google Gemini (OpenAI compatibility)", "api_key_env": "GEMINI_API_KEY",
               "default_base_url": "https://generativelanguage.googleapis.com/v1beta/openai/", "transport": "openai", "requires_api_key": True},
    "ollama": {"name": "ollama", "label": "Ollama", "api_key_env": "OLLAMA_API_KEY",
               "default_base_url": "http://127.0.0.1:11434/v1", "transport": "openai", "requires_api_key": False},
    "custom": {"name": "custom", "label": "Custom OpenAI-compatible endpoint", "api_key_env": "EIRA_API_KEY",
               "default_base_url": "", "transport": "openai", "requires_api_key": False},
}
PROFILES = PROVIDER_PROFILES


def _bounded_json_loads(text: str):
    from .security import bounded_json_loads
    return bounded_json_loads(text)


def _model_name(model: str) -> str:
    if not isinstance(model, str) or not model.strip() or len(model) > 512:
        raise HarnessError("Set EIRA_MODEL or pass --model to choose a tool-capable model.")
    if any(ord(c) < 32 or ord(c) == 127 for c in model):
        raise HarnessError("Model names cannot contain control characters.")
    return model


def _validate_endpoint(base_url: str):
    if not isinstance(base_url, str) or len(base_url) > 4096 or any(ord(c) < 32 or ord(c) == 127 for c in base_url):
        raise HarnessError("Model endpoint must be a valid HTTP(S) base URL.")
    try:
        parsed = urlsplit(base_url)
        port = parsed.port
    except ValueError as exc:
        raise HarnessError("Model endpoint must be a valid HTTP(S) base URL.") from exc
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment):
        raise HarnessError("Model endpoint must be an HTTP(S) base URL without credentials, query, or fragment.")
    if port is not None and not 1 <= port <= 65535:
        raise HarnessError("Model endpoint has an invalid port.")
    host = parsed.hostname.lower().rstrip(".")
    if parsed.scheme != "https" and host not in {"localhost", "127.0.0.1", "::1"}:
        raise HarnessError("Model endpoints require HTTPS, except for local loopback servers.")
    return parsed


def _endpoint(parsed, suffix: str) -> str:
    path = parsed.path.rstrip("/")
    if not path.endswith("/" + suffix):
        path += "/" + suffix
    host = parsed.hostname
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    port = f":{parsed.port}" if parsed.port else ""
    return f"{parsed.scheme}://{host}{port}{path or '/'}"


def _same_base_url(left: str, right: str) -> bool:
    try:
        a, b = urlsplit(left), urlsplit(right)
        return (a.scheme, a.hostname, a.port, a.path.rstrip("/")) == (b.scheme, b.hostname, b.port, b.path.rstrip("/"))
    except ValueError:
        return False


def _validate_usage(usage):
    if usage is None:
        return {}
    if not isinstance(usage, dict):
        raise ValueError("usage")

    result = {}
    for key, value in usage.items():
        if not isinstance(key, str) or len(key) > 100:
            raise ValueError("usage key")
        # Provider metadata can contain nulls and nested objects. Preserve only
        # bounded integer token counters in the runtime's normalized usage.
        token_counter = key in {"input_tokens", "output_tokens", "prompt_tokens", "completion_tokens",
                               "total_tokens", "cache_creation_input_tokens", "cache_read_input_tokens",
                               "reasoning_tokens", "cached_tokens"}
        if not token_counter:
            continue
        if type(value) is not int or not 0 <= value <= 2_000_000_000:
            raise ValueError("usage token")
        result[key] = value
    if "total_tokens" not in result:
        before = result.get("input_tokens", result.get("prompt_tokens"))
        after = result.get("output_tokens", result.get("completion_tokens"))
        if type(before) is int and type(after) is int:
            result["total_tokens"] = before + after
    return result


def _normalise_openai(data: dict) -> tuple[dict, dict]:
    if not isinstance(data, dict) or not isinstance(data.get("choices"), list) or not data["choices"]:
        raise ValueError("choices")
    choice = data["choices"][0]
    if not isinstance(choice, dict):
        raise ValueError("choice")
    finish = choice.get("finish_reason")
    if finish in {"length", "content_filter"}:
        raise HarnessError("Model response was truncated or filtered; no tools were executed.")
    if finish is not None and not isinstance(finish, str):
        raise ValueError("finish")
    message = choice.get("message")
    if not isinstance(message, dict) or message.get("role") != "assistant":
        raise ValueError("message")
    content, refusal = message.get("content"), message.get("refusal")
    if content is not None and not isinstance(content, str):
        raise ValueError("content")
    if refusal is not None and not isinstance(refusal, str):
        raise ValueError("refusal")
    calls = message.get("tool_calls") or []
    if not isinstance(calls, list) or len(calls) > 32:
        raise ValueError("calls")
    ids = set()
    for call in calls:
        function = call.get("function") if isinstance(call, dict) else None
        if (not isinstance(call, dict) or call.get("type") != "function" or not isinstance(call.get("id"), str)
                or not call["id"] or len(call["id"]) > 256 or call["id"] in ids or not isinstance(function, dict)
                or not isinstance(function.get("name"), str) or not function["name"]
                or not isinstance(function.get("arguments"), str) or len(function["arguments"]) > 1_000_000):
            raise ValueError("call")
        try:
            arguments = _bounded_json_loads(function["arguments"])
        except (ValueError, TypeError, UnicodeError, HarnessError) as exc:
            raise ValueError("arguments") from exc
        if not isinstance(arguments, dict):
            raise ValueError("arguments")
        ids.add(call["id"])
    result = {"role": "assistant", "content": content or refusal or ""}
    if refusal is not None:
        result["refusal"] = refusal
    if calls:
        result["tool_calls"] = calls
    return result, _validate_usage(data.get("usage"))


def _anthropic_tools(tools: list[dict]) -> list[dict]:
    if not isinstance(tools, list) or len(tools) > 128:
        raise HarnessError("Tool definitions are malformed or exceed the provider limit.")
    converted = []
    for tool in tools:
        function = tool.get("function") if isinstance(tool, dict) else None
        if (not isinstance(tool, dict) or tool.get("type") != "function" or not isinstance(function, dict)
                or not isinstance(function.get("name"), str) or not function["name"]
                or not isinstance(function.get("parameters"), dict)):
            raise HarnessError("Tool definitions are malformed or unsupported by Anthropic.")
        item = {"name": function["name"], "input_schema": function["parameters"]}
        if function.get("description") is not None:
            if not isinstance(function["description"], str):
                raise HarnessError("Tool definitions are malformed or unsupported by Anthropic.")
            item["description"] = function["description"]
        converted.append(item)
    return converted


def _anthropic_messages(messages: list[dict]) -> tuple[str | None, list[dict]]:
    if not isinstance(messages, list):
        raise HarnessError("Messages are malformed.")
    system, converted, pending = [], [], []

    def flush():
        if pending:
            converted.append({"role": "user", "content": list(pending)})
            pending.clear()
    for message in messages:
        if not isinstance(message, dict) or not isinstance(message.get("role"), str):
            raise HarnessError("Messages are malformed.")
        role = message["role"]
        if role == "system":
            flush()
            if not isinstance(message.get("content", ""), str):
                raise HarnessError("System messages must contain text.")
            system.append(message.get("content", ""))
            continue
        if role == "tool":
            call_id, content = message.get("tool_call_id"), message.get("content")
            if not isinstance(call_id, str) or not call_id or not isinstance(content, str):
                raise HarnessError("Tool results are malformed.")
            pending.append({"type": "tool_result", "tool_use_id": call_id, "content": content})
            continue
        flush()
        if role not in {"user", "assistant"} or (message.get("content") is not None and not isinstance(message.get("content"), str)):
            raise HarnessError("Messages contain an unsupported role or content.")
        content = message.get("content") or ""
        if role == "user":
            converted.append({"role": "user", "content": content})
            continue
        blocks = [{"type": "text", "text": content}] if content else []
        calls = message.get("tool_calls") or []
        if not isinstance(calls, list) or len(calls) > 32:
            raise HarnessError("Tool calls are malformed.")
        for call in calls:
            function = call.get("function") if isinstance(call, dict) else None
            if (not isinstance(call, dict) or call.get("type") != "function" or not isinstance(call.get("id"), str)
                    or not call["id"] or not isinstance(function, dict) or not isinstance(function.get("name"), str)
                    or not isinstance(function.get("arguments"), str)):
                raise HarnessError("Tool calls are malformed.")
            try:
                arguments = _bounded_json_loads(function["arguments"])
            except (ValueError, TypeError, UnicodeError, HarnessError) as exc:
                raise HarnessError("Tool call arguments are not valid bounded JSON.") from exc
            if not isinstance(arguments, dict):
                raise HarnessError("Tool call arguments must be a JSON object.")
            blocks.append({"type": "tool_use", "id": call["id"], "name": function["name"], "input": arguments})
        converted.append({"role": "assistant", "content": blocks or ""})
    flush()
    return ("\n\n".join(system) if system else None), converted


def _normalise_anthropic(data: dict) -> tuple[dict, dict]:
    if not isinstance(data, dict) or data.get("type") not in {None, "message"} or data.get("role") != "assistant":
        raise ValueError("message")
    blocks = data.get("content")
    if not isinstance(blocks, list) or len(blocks) > 64:
        raise ValueError("content")
    texts, calls, ids = [], [], set()
    for block in blocks:
        if not isinstance(block, dict):
            raise ValueError("block")
        if block.get("type") == "text":
            if not isinstance(block.get("text"), str):
                raise ValueError("text")
            texts.append(block["text"])
        elif block.get("type") == "tool_use":
            call_id, name, args = block.get("id"), block.get("name"), block.get("input")
            if (not isinstance(call_id, str) or not call_id or call_id in ids or not isinstance(name, str)
                    or not name or not isinstance(args, dict)):
                raise ValueError("tool use")
            ids.add(call_id)
            calls.append({"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False, allow_nan=False)}})
        else:
            raise ValueError("block type")
    stop = data.get("stop_reason")
    if stop in {"max_tokens", "refusal"}:
        raise HarnessError("Model response was truncated or refused; no tools were executed.")
    if stop is not None and not isinstance(stop, str):
        raise ValueError("stop")
    result = {"role": "assistant", "content": "".join(texts)}
    if calls:
        result["tool_calls"] = calls
    return result, _validate_usage(data.get("usage"))


def _request(url, body, headers, timeout, public_only=False):
    result = request_bytes(url, method="POST", body=body, headers=headers,
                           timeout=min(90, timeout), max_bytes=1_000_000,
                           public_only=public_only)
    if isinstance(result, tuple) and len(result) in {2, 3} and isinstance(result[0], int):
        return result[0], result[-1]
    return getattr(result, "status", 200), getattr(result, "body", result)


class Provider:
    def __init__(self, model: str, base_url: str, api_key: str = "", timeout: int = 90,
                 *, profile: str = "custom", public_only: bool = False):
        self.model = _model_name(model)
        self.parsed = _validate_endpoint(base_url)
        if not isinstance(api_key, str) or any(ord(c) < 32 or ord(c) == 127 for c in api_key):
            raise HarnessError("Provider credentials cannot contain control characters.")
        if type(timeout) not in (int, float) or timeout <= 0:
            raise HarnessError("Model timeout must be positive.")
        self.base_url, self.api_key, self.timeout = base_url, api_key, timeout
        if (profile == "custom" and self.parsed.hostname.lower().rstrip(".") == "api.openai.com"
                and not api_key):
            raise HarnessError("Set OPENAI_API_KEY or pass a provider-specific key before using the OpenAI endpoint.")
        self.provider_name = profile if profile in PROVIDER_PROFILES else "custom"
        self.profile = dict(PROVIDER_PROFILES[self.provider_name])
        self.metadata = dict(self.profile)
        self.public_only = public_only

    def complete(self, messages: list[dict], tools: list[dict]) -> tuple[dict, dict]:
        if self.profile["transport"] == "anthropic":
            system, native = _anthropic_messages(messages)
            payload = {"model": self.model, "max_tokens": 4096, "messages": native,
                       "tools": _anthropic_tools(tools), "tool_choice": {"type": "auto"}}
            if system is not None:
                payload["system"] = system
            headers = {"Content-Type": "application/json", "User-Agent": "eira-harness/0.2", "anthropic-version": "2023-06-01"}
            if self.api_key:
                headers["x-api-key"] = self.api_key
            normalise, url = _normalise_anthropic, _endpoint(self.parsed, "messages")
        else:
            _anthropic_tools(tools)
            payload = {"model": self.model, "messages": messages, "tools": tools, "tool_choice": "auto"}
            headers = {"Content-Type": "application/json", "User-Agent": "eira-harness/0.2"}
            if self.api_key:
                headers["Authorization"] = f"Bearer {self.api_key}"
            normalise, url = _normalise_openai, _endpoint(self.parsed, "chat/completions")
        try:
            body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise HarnessError("Model request contains malformed JSON values.") from exc
        deadline = time.monotonic() + min(float(self.timeout), 90.0)
        for attempt in range(3):
            try:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise HarnessError("Model request exceeded its total deadline.")
                remaining = min(90, remaining)
                status, raw = _request(url, body, headers, remaining, self.public_only)
                if status in (429, 500, 502, 503, 504) and attempt < 2 and time.monotonic() < deadline:
                    time.sleep(min(2 ** attempt, max(0, deadline - time.monotonic())))
                    continue
                if status != 200:
                    raise HarnessError("Model endpoint returned an error. Check credentials, model, quota, and endpoint.")
                if isinstance(raw, str):
                    raw = raw.encode("utf-8")
                if not isinstance(raw, (bytes, bytearray)) or len(raw) > 1_000_000:
                    raise HarnessError("Model response exceeded 1 MB or was not valid bytes.")
                return normalise(_bounded_json_loads(bytes(raw).decode("utf-8")))
            except HarnessError as exc:
                if re.search(r"\bHTTP\s+(429|500|502|503|504)\b", str(exc)) and attempt < 2 and time.monotonic() < deadline:
                    time.sleep(min(2 ** attempt, max(0, deadline - time.monotonic())))
                    continue
                raise
            except (json.JSONDecodeError, UnicodeError, ValueError, TypeError, KeyError, IndexError, AttributeError) as exc:
                raise HarnessError("Model endpoint returned a malformed response; no tools were executed.") from exc
            except (OSError, http.client.HTTPException) as exc:
                raise HarnessError("Model connection failed or timed out. Session is saved; retry explicitly.") from exc
        raise HarnessError("Model endpoint is unavailable.")


def build_provider(provider_name: str, model: str, base_url: str | None = None) -> Provider:
    if not isinstance(provider_name, str) or provider_name.strip().lower() not in PROVIDER_PROFILES:
        raise HarnessError(f"Unsupported provider profile: {provider_name}.")
    _model_name(model)
    name = provider_name.strip().lower()
    profile = PROVIDER_PROFILES[name]
    if base_url is None:
        base_url = profile["default_base_url"] or os.getenv("EIRA_BASE_URL", "")
    if not base_url:
        raise HarnessError("Set EIRA_BASE_URL when using the custom provider profile.")
    # A named provider's credential is valid only for its built-in endpoint.
    # Explicit endpoint overrides use the generic custom credential, so an
    # OpenAI key can never accidentally be sent to another service.
    default_endpoint = bool(profile["default_base_url"] and _same_base_url(base_url, profile["default_base_url"]))
    key_env = profile["api_key_env"] if default_endpoint else "EIRA_API_KEY"
    api_key = os.getenv(key_env, "")
    if profile["requires_api_key"] and default_endpoint and not api_key:
        raise HarnessError(f"Set {profile['api_key_env']} before using the {name} provider.")
    public_only = name not in {"ollama", "custom"} and default_endpoint
    return Provider(model, base_url, api_key, profile=name, public_only=public_only)


def provider_profiles() -> dict[str, dict]:
    return {name: dict(profile) for name, profile in PROVIDER_PROFILES.items()}
