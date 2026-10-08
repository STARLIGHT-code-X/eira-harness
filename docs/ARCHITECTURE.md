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
| `agent.py` | Bounded loop, frozen session prefix, compaction, budget checks, crash recovery |
| `provider.py` | HTTP transport, Chat Completions and Anthropic Messages translation, prompt caching |
| `tools.py` | Tool definitions, argument validation, workspace operations, execution policy |
| `patch.py` | `apply_patch`: Codex-format parser, tolerant matching, one combined approval, all-or-nothing commit with rollback ([PATCHES.md](PATCHES.md)) |
| `outputs.py` | Head-and-tail shortening of long output; saved, redacted copies paged by `read_output` |
| `instructions.py` | Instruction-file discovery (EIRA.md, AGENTS.md, fallbacks), the prompt budget, and just-in-time subdirectory guidance |
| `text.py` | Shared line splitting: only `\r\n`, `\r` and `\n` end a line, as in the file tools |
| `navigate.py` | Bounded workspace walk with `.gitignore` support, list paging, literal and regex line search |
| `search_worker.py` | Standalone stdlib script that runs regex search in an isolated, killable interpreter |
| `security.py` | Path checks, atomic file writes, best-effort secret redaction, terminal sanitization |
| `network.py` | Bounded HTTP transport, total deadlines, address pinning, and text retrieval |
| `market_data.py` | Fixed-source daily price retrieval and CSV normalization |
| `finance.py` | Deterministic SMA simulation, metrics, report formatting |
| `store.py` | SQLite journal, workspace memory, per-session file locks |
| `evals.py` | Task suites, throwaway workspaces, declarative checks, reports |
| `demo.py` | Synthetic data generator and offline fixture provider |

## Provider contract

A provider exposes `model: str` and `complete(messages, tools) -> (assistant_message, usage)`.

Messages use the Chat Completions conversation format. A tool result has `role: "tool"`, the original `tool_call_id`, and JSON-encoded content. The stock provider validates roles, text content, function-call envelopes, unique call IDs within each response, and completion status before returning a response to the runtime. Custom providers must honor that contract.

Model HTTP errors `429`, `500`, `502`, `503`, `504`, and `529` receive at most two retries within `--model-timeout`. Authentication failures and malformed responses fail immediately. Uncertain connection failures are not retried automatically. HTTP error bodies are not echoed; errors name the status and, when present, a lowercase provider error-type token. Streaming is future work.

An assistant message may carry `anthropic_content`: the provider's original content blocks, kept when the turn includes `thinking` or `redacted_thinking` blocks. The Anthropic transport replays them verbatim and in order when their `tool_use` ids still match the normalized `tool_calls`, and otherwise rebuilds the turn from `content` and `tool_calls`. Journal redaction must not alter signed blocks: if redacting a turn would change any block, the turn is stored without them and marked `reasoning_withheld`, and the transport then replays no reasoning up to and including that turn. Removing a leading run of reasoning is accepted by the API; replaying a modified block is not. The Chat Completions transport sends only standard message fields, so local metadata never reaches those servers.

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

`Tool` takes two optional fields that never reach the model; `Tool.schema()` is unchanged by them:

- `effects`: a set drawn from `read`, `write`, `exec`, `network` and `memory` (default empty). `register` rejects any other name. `Toolbox.mutating(name)` is true when a tool's effects include `write` or `exec`, which features such as checkpoints use to decide what to snapshot.
- `describe(arguments) -> str`: the one-line progress summary shown in `tool_started`. Its result is redacted, whitespace-collapsed and cut to 100 characters; an exception renders an empty summary.

`Toolbox` also exposes seams that features attach to instead of editing tool bodies:

- `write_guards`: callables `guard(path, old, new)`, with `old` None for a new file. `edit_file` and `write_file` run them through `check_write` after computing the new content and before approval. A guard refuses the write by raising `HarnessError`, which leaves the file untouched and asks nothing; a returned dict is reported in the result's `checks` list.
- `review_paths(path) -> bool`: true forces a human prompt for that write even under `--approve-writes`, through `Policy.require(..., always_ask=True)`.
- `after_call`: hooks `hook(name, arguments, result) -> result`, run in order after a tool returns.
- `notify(kind, **fields)`: raise an event that the owning `Agent` redacts, journals and emits (see [EVENTS.md](EVENTS.md)).
- `Toolbox.shell` runs as phases, `_shell_plan`, `_shell_approve`, `_shell_run` and `_shell_result`, after the mode, credential and read-only checks.

Metadata and hooks never bypass `Policy.require`. Effects are advisory, `always_ask` can add a prompt but never remove one, read-only is evaluated first, and every default is a no-op.

## Code navigation

`list_files` and `search_files` share `navigate.walk`: `os.scandir` without following symlinks, sorted, at most 32 levels, 200,000 entries and 5 s. Every entry passes `Workspace.path` (with one snapshot of the protected home-configuration locations per walk), so blocked names never appear whatever the flags. Dot entries such as `.github` and `.gitignore` are listed; dependency and cache directories (`node_modules`, `__pycache__`, `venv`, `.venv`, `.tox`, `.nox`, `.mypy_cache`, `.pytest_cache`, `.ruff_cache`) and `.gitignore` matches are skipped unless `ignored` is true. Results carry `skipped` counts (`ignored`, `heavy_dirs`, `blocked`, and for search `binary`, `too_large`, `non_utf8`, `unreadable`) instead of skipping silently. `list_files` pages with `offset`, `limit` (default 500, at most 2,000) and `next_offset`, in sorted path order.

Literal search runs in-process with a 5 s deadline. A regular expression never runs in Eira's process, because Python's `re` has no timeout: the pattern is compiled in the parent first (invalid patterns fail without a subprocess), then `search_worker.py` runs as `python -I` with cwd `/`, an empty environment and only parent-validated absolute paths on stdin, and its process group is killed after 10 s. Both modes share `search_worker.scan`, number lines with the `text.split_lines` rule used by `read_file` and `apply_patch`, and report `matches` (optional `context` lines), `files` (per-file counts) or `count` totals. Details are in [CODE-SEARCH.md](CODE-SEARCH.md).

## Session prefix and compaction

Providers cache, and newer models bind their reasoning to, the exact request prefix: system prompt, tools, then earlier messages. Eira therefore keeps every request in a session append-only:

- The system prompt, `EIRA.md`, and memory are captured in the `session_context` table on the session's first run. Later changes are detected by digest and appended as a labelled user message on the next task. The original prompt is never rewritten.
- Instruction files are discovered by `instructions.discover()` (global file, then the git root down to the workspace) and rendered into that frozen prompt. Subdirectory files are attached to tool results by an `after_call` hook (`instructions.attach_jit`), so they never change the prefix. See [INSTRUCTIONS.md](INSTRUCTIONS.md).
- Tools are registered in a fixed order. Recovery closes interrupted calls by appending results.
- Tool results are fitted to `max_tool_output_chars` once, when they are journaled, so replays are identical.

Compaction is whole-history: at 80% of `max_context_chars`, the current conversation plus a summarization instruction is sent as one request (the same prefix and tools, so it can hit the cache, with `tool_choice` set to `none` for providers whose `complete()` accepts it). Text that arrives with stray tool calls is used and the calls are dropped; a reply with no text fails with a `compaction_failed` event that carries its usage. The full summary is kept in the marker's metadata, and the latest guidance and memory are recomputed at compaction time. The reply becomes a user message tagged `eira_compaction` that also quotes the latest user request. `Store.model_messages` starts the model's view at the most recent such message, so nothing older, including reasoning blocks, is replayed. If the summary request itself would exceed the limit, the largest tool results are omitted from that one request only. The journal keeps every original message. At most three compactions run per task; `Limits(compact=False)` or `--no-compact` restores the hard stop.

## Tool output budgets

Long output keeps its beginning and its end, because test failures, stack traces and summaries are usually at the bottom. `outputs.head_tail(text, budget)` keeps about half the budget from each end, moves each cut inward to a line boundary when one is within 200 characters (so the kept text never exceeds the budget), and puts one marker line in between, for example `…[48,213 characters truncated (lines 212-4,977); read_output(output_id="o-3fa9c1d2e4b5", start_line=212) shows them]…`. Line numbers follow the `text.split_lines` rule.

- `shell` captures up to 4 MiB (`SHELL_CAPTURE_BYTES`) and stops the command past that. The result shows the first and last 10,000 characters and adds `total_bytes`, `total_lines` and `output_id` (null when nothing was shortened). A stopped command's result says the saved copy is incomplete.
- `fetch_url` reads up to 1,000,000 characters of a page and shows the first and last 15,000 when it is longer than 30,000; it adds `total_chars` and `output_id`.
- `Agent.fit` shortens a result's longest strings to head and tail halves when the encoded result exceeds `max_tool_output_chars`, saves the complete encoded result as indented JSON (so it pages by line) and adds `output_id` at the top level. It still runs once, at journal time, so replays and prompt prefixes stay identical.

Saved copies live in `.eira/outputs/<session>/<output_id>.txt`. `OutputStore.save` redacts the text, cuts it to 4 MiB with a closing note, writes it atomically with mode 0600 in 0700 directories that must not be symlinks, evicts the session's oldest copies past 64 MiB, and sweeps copies older than 7 days from every session (at most 1,000 entries per save). The `read_output` tool accepts only ids matching `o-` and 12 hex digits and resolves them in the current session's directory alone. It returns pages of at most 400 lines or 24,000 characters with `next_start_line`, or, with `query`, up to 100 literal-substring matches as `{line, text}`. A missing copy reports that it expired or was never saved. A successful save from `shell` or `fetch_url` raises the `output_saved` event, and the terminal prints `· long output saved (N lines)`. `Toolbox.outputs` is created lazily; saving is best effort, and a failed save only removes the pointer from the marker.

## Journal semantics

SQLite stores sessions, ordered messages, ordered events, and workspace notes. The state directory is private to the user. Assistant responses are committed before tool execution; each tool result is committed after completion. Filesystem side effects and SQLite cannot share a transaction. A crash can therefore leave an unknown side effect. Recovery closes missing tool results as unknown, and never replays them automatically.

The trace is inspectable, not cryptographically tamper-evident. Stronger audit guarantees need an external append-only log and identities. Locking prevents two Eira processes from running one session concurrently, but does not lock every workspace file or coordinate independent sessions. Optimistic content hashes protect ordinary file edits.

## Reproducible financial work

The deterministic engine is callable without a model. Reports bind parameters to a SHA-256 of the input CSV text. The same data and parameters produce the same result. LLM narration is kept outside the accounting engine. The initial strategy family is deliberately narrow: long/cash SMA crossover with explicit costs and a closing drawdown stop. Strategy code is not dynamically imported by the backtest tool.

This separates language-model assistance from numerical execution, but does not validate the economic merit of a strategy. Dataset provenance, survivorship, corporate actions, market microstructure, and out-of-sample testing remain research responsibilities.
