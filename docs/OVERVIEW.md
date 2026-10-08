# What Eira is

Eira is a local agent harness. It gives a tool-calling language model a working directory, a bounded execution loop, durable sessions, reviewed file edits, a Docker sandbox for commands, and research and financial-data tools. You choose the model and endpoint; everything else runs on your machine, in Python 3.11+ with no third-party dependencies.

It is built around one idea: **an agent should be trusted only as far as the harness can show what it did and undo or refuse what it should not do.** Every write is reviewed or explicitly preapproved. Every command runs in a locked-down container. Every turn is journaled, and work can be rewound.

## How a task flows

```text
you ──► eira (CLI / chat) ──► Agent loop ──► model endpoint (OpenAI-compatible or Anthropic)
                                  │   ▲
                       tool calls │   │ results (redacted, size-bounded, head + tail kept)
                                  ▼   │
                     Toolbox ── policy: approve / preapprove / deny / sandbox
                                  │
            files · patches · search · shell (Docker) · web fetch · market data · memory
                                  │
                     SQLite journal (.eira/state.db) + checkpoints (.eira/history)
```

1. The system prompt, workspace instructions (`AGENTS.md`, `EIRA.md`, `CLAUDE.md`, `GEMINI.md`) and memory are frozen when a session starts. Each request is the previous one plus new turns, so provider prompt caches stay warm and models that sign their reasoning can keep it.
2. The model replies with text or tool calls. Each call is schema-validated, checked against policy, run, and journaled before the next request.
3. File-changing batches are checkpointed first. Long results keep their beginning and end; the full output is saved for `read_output`.
4. Near the context limit, Eira summarizes the conversation and continues. Originals are never deleted.
5. The run ends when the model answers without tool calls, or a step, tool, token or context budget stops it.

## What the model can do

| Tool | Purpose |
|---|---|
| `list_files`, `read_file`, `search_files` | Navigate: `.gitignore`-aware listing, paged reads of files up to 5 MB, literal or regex search with context |
| `edit_file`, `apply_patch`, `write_file` | Change code: exact replacements, Codex-format multi-file patches applied all-or-nothing, whole-file writes. Each shows a diff and passes a syntax guard before approval |
| `shell` | Run commands in Docker: no network, no capabilities, a read-only root, secrets masked, VCS/CI/agent config read-only |
| `read_output` | Page or search the saved full output of an earlier command or fetch |
| `fetch_url` | Read an approved public HTTPS page (private addresses, redirects and cookies refused) |
| `market_prices`, `backtest_sma` | Download daily prices (Coinbase, Alpha Vantage) and run a deterministic long/cash SMA backtest with costs |
| `remember`, `set_plan` | Keep reviewed workspace notes; record a plan in the trace |

## What you control

- **Approvals.** Writes ask by default; `--approve-writes` preapproves workspace writes, except protected config files, which always ask. `--read-only` denies writes and shell. Shell is off unless `--shell docker`. `--shell-approval sandboxed` runs protected container commands without prompts, but destructive ones still ask. Network fetches need approval or `--allow-host`.
- **Undo.** `eira checkpoints`, `eira diff`, and `eira rewind --code|--conversation|--both` (or `/checkpoints`, `/diff` and `/rewind` in chat).
- **Budgets.** `--max-steps`, `--max-tool-calls`, `--max-tokens`, `--max-context-chars`, `--max-output-tokens`, `--model-timeout`; `--no-compact` to stop at the context limit instead of summarizing.
- **Inspection.** `eira sessions`, `eira trace` (the frozen prompt, every message and event), `eira instructions`, `eira doctor`, and JSONL events with `--json` ([EVENTS.md](EVENTS.md)).
- **Measurement.** `eira eval` runs task suites in throwaway workspaces and reports pass rate, tool errors, tokens and time ([EVALS.md](EVALS.md)).

## Models

OpenAI, Anthropic (native Messages API, with thinking blocks replayed verbatim and prompt caching), OpenRouter, Gemini, Ollama, and any OpenAI-compatible endpoint. Keys come from the environment or a hidden prompt and are never saved. Remote endpoints must use HTTPS. Provider error bodies are never echoed.

## Safety model, in one paragraph

File tools refuse traversal, symlinks, hard links, VCS metadata, Eira's own state and credential files. Recognized secrets are redacted from everything stored or shown. Files containing them cannot be edited by the model, and stored reasoning that redaction would alter is withheld rather than replayed altered. Approval text is JSON-escaped, so hidden characters stay visible, and piped stdin can never approve anything. The shell exists only in Docker, and its isolation is integration-tested against a real daemon as root and non-root. Interrupted tool calls are recorded as having an unknown outcome and are never replayed. Details and residual risks: [SECURITY.md](SECURITY.md) and [SANDBOX.md](SANDBOX.md).

## Where it stops

Eira is a developer release, not a hardened multi-tenant service. The SQLite journal is private but not encrypted or tamper-evident. Redaction is best effort. A container is not a VM. There is no token streaming, MCP, web search, browser control or multi-agent delegation yet, and the financial tools retrieve and backtest only; they never trade. No comparative benchmark claim is made: measure your model with `eira eval`. What comes next is in [ROADMAP.md](ROADMAP.md).

## Map of the code

| Module | Role |
|---|---|
| `cli.py`, `terminal.py`, `settings.py` | Commands, flags, approvals, chat interface, rendering, saved model preferences |
| `agent.py` | The loop: frozen prefix, compaction, budgets, recovery, events |
| `provider.py`, `network.py` | Wire formats, retries, deadlines, address-pinned HTTPS |
| `tools.py` | Tool registry, policy, file tools, shell pipeline |
| `patch.py`, `syntax.py`, `navigate.py`, `search_worker.py`, `outputs.py` | `apply_patch`, the syntax guard, search and listing (regex in a killable worker), saved output |
| `sandbox.py`, `approvals.py` | Container mount plan; sandboxed-autorun decisions |
| `checkpoints.py`, `instructions.py` | Snapshots and rewind; instruction discovery |
| `store.py`, `security.py`, `text.py` | Journal, redaction, path checks, line splitting |
| `evals.py`, `finance.py`, `market_data.py`, `demo.py` | Evaluation, backtesting, price data, offline demo |
