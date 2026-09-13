# Architecture

Eira has no framework dependency. The executable path is `cli → Agent → Provider / Toolbox → Store`.

```text
 User / CLI flags
       │
       ▼
 Agent loop ──────────────► OpenAI-compatible model endpoint
       │                          │
       │ ◄──── assistant text + tool calls
       ▼
 Journal assistant message
       │
       ▼
 Validate tool schema ──► Apply local policy ──► Execute
       │                                          │
       └────────── Journal result ◄───────────────┘
                          │
                          └──► Next model request / final answer
```

## Modules

| Module | Responsibility |
|---|---|
| `cli.py` | Commands, model configuration, terminal approvals, JSONL rendering |
| `agent.py` | Bounded loop, system instructions, history, budget checks, crash recovery |
| `provider.py` | HTTP transport and validation of the Chat Completions response envelope |
| `tools.py` | Tool definitions, argument validation, workspace operations, execution policy |
| `security.py` | Path checks, atomic file writes, best-effort secret redaction, terminal sanitization |
| `network.py` | Bounded HTTP transport, total deadlines, address pinning, and text retrieval |
| `market_data.py` | Fixed-source daily price retrieval and CSV normalization |
| `finance.py` | Deterministic SMA simulation, metrics, report formatting |
| `store.py` | SQLite journal, workspace memory, per-session file locks |
| `demo.py` | Synthetic data generator and offline fixture provider |

## Provider contract

A provider exposes `model: str` and `complete(messages, tools) -> (assistant_message, usage)`.

Messages use the Chat Completions conversation format. A tool result has `role: "tool"`, the original `tool_call_id`, and JSON-encoded content. The stock provider validates roles, text content, function-call envelopes, unique call IDs within each response, and completion status before returning a response to the runtime. Custom providers must honor that contract.

Model HTTP errors `429`, `500`, `502`, `503`, and `504` receive at most two retries. Authentication failures and malformed responses fail immediately. Uncertain connection failures are not retried automatically. HTTP error bodies are not echoed. Streaming and context compaction are future work.

## Adding a tool

Use `Toolbox.register(Tool(...))` from a Python entrypoint that constructs Eira. The stock CLI intentionally does not auto-import arbitrary code from the workspace. Here is a harmless example:

```python
from eira_harness.tools import Tool

toolbox.register(Tool(
    name="word_count",
    description="Count words in supplied text.",
    properties={"text": {"type": "string", "maxLength": 10000}},
    required=["text"],
    execute=lambda text: {"words": len(text.split())},
))
```

Current schemas support scalar string, integer, number, and boolean properties, required fields, enum values, simple bounds, and rejection of undeclared properties. The validator is deliberately not advertised as a full JSON Schema implementation.

New side-effecting tools must call `toolbox.policy.require(...)` before execution. `Toolbox.register` does not infer a custom tool's privileges; plugin authors are trusted application developers, not untrusted model output. Use `Workspace` for file boundaries. Never pass arbitrary tool names or arguments directly to a shell or import statement. Add behavioral tests for denials, malformed input, and interrupted execution.

## Journal semantics

SQLite stores sessions, ordered messages, ordered events, and workspace notes. The state directory is private to the user. Assistant responses are committed before tool execution; each tool result is committed after completion. Filesystem side effects and SQLite cannot share a transaction. A crash can therefore leave an unknown side effect. Recovery closes missing tool results as unknown, and never replays them automatically.

The trace is inspectable, not cryptographically tamper-evident. Stronger audit guarantees need an external append-only log and identities. Locking prevents two Eira processes from running one session concurrently, but does not lock every workspace file or coordinate independent sessions. Optimistic content hashes protect ordinary file edits.

## Reproducible financial work

The deterministic engine is callable without a model. Reports bind parameters to a SHA-256 of the input CSV text. The same data and parameters produce the same result. LLM narration is kept outside the accounting engine. The initial strategy family is deliberately narrow: long/cash SMA crossover with explicit costs and a closing drawdown stop. Strategy code is not dynamically imported by the backtest tool.

This separates language-model assistance from numerical execution, but does not validate the economic merit of a strategy. Dataset provenance, survivorship, corporate actions, market microstructure, and out-of-sample testing remain research responsibilities.
