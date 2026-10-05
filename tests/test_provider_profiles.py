import json
import os
import unittest
from unittest.mock import patch

from eira_harness.provider import PROVIDER_PROFILES, Provider, build_provider
from eira_harness.security import HarnessError


def completion(text="ok"):
    return {"choices": [{"finish_reason": "stop", "message": {
        "role": "assistant", "content": text}}], "usage": {
            "prompt_tokens": 2, "completion_tokens": 3}}


class ProviderProfilesTests(unittest.TestCase):
    def test_profiles_use_only_their_declared_environment_key(self):
        env = {"OPENAI_API_KEY": "openai-secret", "EIRA_API_KEY": "custom-secret"}
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(build_provider("ollama", "llama", "http://127.0.0.1:11434/v1").api_key, "")
            self.assertEqual(build_provider("custom", "model", "https://model.example/v1").api_key, "custom-secret")

    def test_builtin_profile_requires_key_only_for_its_default_endpoint(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(HarnessError, "OPENAI_API_KEY"):
                build_provider("openai", "model")
            self.assertEqual(build_provider("openai", "model", "https://model.example/v1").api_key, "")

    def test_openai_compatible_request_and_usage_are_normalized(self):
        seen = {}
        def fake(url, **kwargs):
            seen.update(url=url, kwargs=kwargs)
            return 200, {}, json.dumps(completion()).encode()
        with patch("eira_harness.provider.request_bytes", fake):
            result, usage = Provider("model", "https://model.example/v1", "key").complete([], [])
        self.assertEqual(result["content"], "ok")
        self.assertEqual(usage["total_tokens"], 5)
        self.assertEqual(seen["url"], "https://model.example/v1/chat/completions")
        self.assertEqual(seen["kwargs"]["public_only"], False)
        self.assertEqual(seen["kwargs"]["headers"]["Authorization"], "Bearer key")

    def test_anthropic_messages_tool_round_trip(self):
        seen = {}
        response = {"type": "message", "role": "assistant", "stop_reason": "tool_use",
                    "content": [{"type": "text", "text": "Checking"},
                                 {"type": "tool_use", "id": "tool-1", "name": "read_file",
                                  "input": {"path": "a.txt"}}],
                    "usage": {"input_tokens": 4, "output_tokens": 6}}
        def fake(url, **kwargs):
            seen.update(url=url, kwargs=kwargs)
            return 200, {}, json.dumps(response).encode()
        messages = [{"role": "system", "content": "Be concise"},
                    {"role": "user", "content": "Read a.txt"},
                    {"role": "assistant", "content": "", "tool_calls": [{
                        "id": "old", "type": "function", "function": {
                            "name": "read_file", "arguments": '{"path":"a.txt"}'}}]},
                    {"role": "tool", "tool_call_id": "old", "content": "contents"}]
        tools = [{"type": "function", "function": {"name": "read_file", "description": "Read",
                 "parameters": {"type": "object", "properties": {"path": {"type": "string"}}}}}]
        with patch("eira_harness.provider.request_bytes", fake):
            result, usage = Provider("claude", "https://api.example/v1", "anth-key", profile="anthropic").complete(messages, tools)
        body = json.loads(seen["kwargs"]["body"])
        self.assertEqual(seen["url"], "https://api.example/v1/messages")
        self.assertEqual(seen["kwargs"]["headers"]["x-api-key"], "anth-key")
        self.assertEqual(body["system"], [{"type": "text", "text": "Be concise", "cache_control": {"type": "ephemeral"}}])
        self.assertEqual(body["max_tokens"], 16000)
        self.assertEqual(body["messages"][-1]["content"][-1]["cache_control"], {"type": "ephemeral"})
        self.assertEqual(body["tools"][0]["input_schema"]["type"], "object")
        self.assertEqual(body["messages"][-1]["content"][0]["type"], "tool_result")
        self.assertEqual(result["tool_calls"][0]["function"]["arguments"], '{"path": "a.txt"}')
        self.assertEqual(usage["total_tokens"], 10)

    def test_malformed_refusal_and_deep_tool_arguments_are_rejected(self):
        malformed = completion()
        malformed["choices"][0]["message"]["refusal"] = {"bad": True}
        deep = "{}"
        for _ in range(40):
            deep = "{" + '"x":' + deep + "}"
        calls = [{"id": "x", "type": "function", "function": {
            "name": "f", "arguments": deep}}]
        malformed["choices"][0]["message"] = {"role": "assistant", "content": None, "tool_calls": calls}
        with patch("eira_harness.provider.request_bytes", return_value=(200, {}, json.dumps(malformed).encode())):
            with self.assertRaises(HarnessError):
                Provider("model", "https://model.example/v1").complete([], [])

    def test_refusal_and_usage_types_fail_closed(self):
        for edit in ['refusal', 'usage']:
            body = completion()
            if edit == 'refusal':
                body['choices'][0]['message']['refusal'] = {'unexpected': 'object'}
            else:
                body['usage']['total_tokens'] = 1.5
            with patch('eira_harness.provider.request_bytes', return_value=(200, {}, json.dumps(body).encode())):
                with self.assertRaises(HarnessError): Provider('model', 'https://model.example/v1').complete([], [])

    def test_endpoint_override_does_not_forward_named_provider_key(self):
        with patch.dict(os.environ, {'OPENAI_API_KEY': 'provider-only', 'EIRA_API_KEY': 'explicit-custom'}, clear=True):
            self.assertEqual(build_provider('openai', 'model', 'https://custom.example/v1').api_key, 'explicit-custom')
            self.assertFalse(build_provider('ollama', 'model').public_only)

    def test_endpoint_validation_rejects_credentials_query_and_control(self):
        for url in ["https://user:pass@example.com/v1", "https://example.com/v1?key=x", "https://example.com/v1\n"]:
            with self.subTest(url=url), self.assertRaises(HarnessError):
                Provider("model", url)


if __name__ == "__main__":
    unittest.main()
