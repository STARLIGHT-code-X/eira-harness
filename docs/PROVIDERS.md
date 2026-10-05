# Model providers

Eira requires an explicit model identifier. Set `EIRA_PROVIDER` and `EIRA_MODEL`,
then run `eira run` (or pass `--provider` and `--model` on the command line).
Credentials are read from the profile-specific variable below; a key from one
profile is never sent to another profile.

If `--base-url` overrides a named profile's built-in endpoint, use the
`custom` profile and `EIRA_API_KEY` (or set `EIRA_API_KEY` for that override).

| Profile | Credential variable | Default endpoint | Wire format |
| --- | --- | --- | --- |
| `openai` | `OPENAI_API_KEY` | `https://api.openai.com/v1` | OpenAI Chat Completions |
| `anthropic` | `ANTHROPIC_API_KEY` | `https://api.anthropic.com/v1` | Native Messages |
| `openrouter` | `OPENROUTER_API_KEY` | `https://openrouter.ai/api/v1` | OpenAI Chat Completions |
| `gemini` | `GEMINI_API_KEY` | `https://generativelanguage.googleapis.com/v1beta/openai/` | OpenAI compatibility |
| `ollama` | `OLLAMA_API_KEY` (optional) | `http://127.0.0.1:11434/v1` | OpenAI compatibility |
| `custom` | `EIRA_API_KEY` (optional) | `EIRA_BASE_URL` | OpenAI Chat Completions |

For example:

```sh
export EIRA_PROVIDER=openai
export EIRA_MODEL='<your-tool-capable-model-id>'
export OPENAI_API_KEY='<your-openai-api-key>'
eira run 'Inspect the project README' --read-only
```

Anthropic uses the native `/messages` endpoint and translates Eira tool calls
to `tool_use` blocks and tool results to `tool_result` blocks. OpenRouter,
Gemini, and Ollama use their documented OpenAI-compatible Chat Completions
endpoints.

Current Claude models think by default and return `thinking` blocks, whose
text is empty unless the request asks for a summary. Eira stores them with the turn and sends them back unchanged and in
their original positions. Requests carry two `cache_control` breakpoints: one
on the system prompt, which also covers the tool definitions, and one on the
newest turn. Because Eira keeps each session's prefix append-only, every
request can read the previous one from the cache. `usage` cache counters are
preserved and included in `total_tokens`.

| Flag | Default | Meaning |
| --- | --- | --- |
| `--max-output-tokens` | 16000 | Anthropic `max_tokens`; thinking counts toward it. Lower it for older models with smaller limits. |
| `--model-timeout` | 300 | Seconds per model request including retries, for every profile (max 900) |
| `--no-prompt-cache` | off | Send no `cache_control` markers |

Eira does not send a `thinking` parameter, so each model uses its own default.
Server tools, streaming, and beta features are not enabled.

Use a custom endpoint explicitly:

```sh
export EIRA_PROVIDER=custom
export EIRA_MODEL='<your-model-id>'
export EIRA_BASE_URL='https://your-model.example/v1'
export EIRA_API_KEY='<your-api-key>'
eira run 'Inspect the project README' --read-only
```

Remote model endpoints must use HTTPS. HTTP is accepted only for loopback
model servers such as Ollama. Endpoint URLs cannot contain credentials, query
strings, fragments, or control characters. Provider responses are size and
shape checked before any returned tool call can reach the runtime.

Profiles are tested against local and mocked protocol fixtures. Real account access, model quality, multimodal features, and every model's tool compatibility are not certified by these tests. Thinking blocks are supported as described above; server tools are not enabled.

Official references: [Anthropic tool results](https://platform.claude.com/docs/en/agents-and-tools/tool-use/handle-tool-calls), [Gemini OpenAI compatibility](https://ai.google.dev/gemini-api/docs/openai), [OpenRouter quickstart](https://openrouter.ai/docs/quickstart), and [Ollama compatibility](https://docs.ollama.com/api/openai-compatibility).
