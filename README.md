# Eira

**A local agent harness for building, researching, and testing ideas.**

Eira gives a tool-capable language model a working directory, an execution loop, durable sessions, reviewed file edits, and financial research tools. You choose the model and endpoint. Conversations and tool traces stay in a local SQLite database; relevant conversation content is sent to the model endpoint you configure.

This is a working **v0.4 developer release**, inspired by the general-agent and financial-workflow scope of Minara Harness. It is independently implemented. It does not claim Minara feature parity or benchmark superiority.

```text
EIRA / offline-scripted-demo
Session …

  → set_plan
  → read_file
  → backtest_sma
Synthetic-data backtest complete. SMA 5/20 returned 22.13%;
maximum drawdown 5.93%; 4 round trips after fees and slippage.
```

The example above is a deterministic demo on synthetic prices, not evidence of investment performance or an LLM evaluation.

## Install with curl

Requires **Python 3.11+ on Linux, macOS, or WSL**:

```bash
curl --fail --silent --show-error --location --proto '=https' --proto-redir '=https' \
  https://raw.githubusercontent.com/STARLIGHT-code-X/eira-harness/main/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"
Eira
```

The installer downloads a pinned source commit and installs into your own Python environment under `~/.local/share/eira`. It requires no sudo. The installer adds its command directory to supported bash, zsh, or fish startup files. Open a new terminal, or use the printed PATH command in your current terminal. Both `Eira` and `eira` work. To inspect the installer first, download it to a file and read it before running `sh install.sh`. See [installation and upgrades](docs/INSTALL.md).

## Open your coding workspace

```bash
cd /path/to/project
Eira
```

No subcommand is required. On first launch, choose a provider and paste its tool-capable model ID. Eira remembers your provider, model, and optional endpoint in your user configuration directory. If a key is needed, enter it at the hidden prompt for this process or provide the provider's environment variable. Keys are never saved by setup.

The terminal has a frost-and-lavender welcome panel, workspace and permission context, arrow-key input history, tool progress, model timing, and saved conversations. `NO_COLOR` disables styling. Use `Eira setup` to configure preferences separately.

| Command | Action |
|---|---|
| `/help` | Show chat commands |
| `/model` / `/provider` | Choose a model or provider interactively |
| `/new` | Start a fresh conversation |
| `/sessions` / `/resume ID` | Find and resume workspace conversations |
| `/status` | Show the current endpoint and permissions |
| `/instructions` | List the instruction files loaded for this workspace |
| `/clear` | Clear the display while retaining history |
| `/exit` | Leave chat |

Repositories set up for other agents work out of the box: alongside `EIRA.md`, Eira reads `AGENTS.md` (and `AGENTS.override.md`) as Codex does, falls back to `CLAUDE.md` or `GEMINI.md`, and walks from the git root down to the workspace. Instruction files in subdirectories are delivered the first time the model works there. Run `Eira instructions` to see what is loaded and why; `--instructions workspace` or `none` limits it. See [docs/INSTRUCTIONS.md](docs/INSTRUCTIONS.md).

Ctrl+C during a task interrupts it and returns to the prompt; Ctrl+C at the prompt exits. Existing tool approval rules remain in effect. Switching providers retains the conversation and sends that history to the newly selected endpoint on your next task; use `/new` for a fresh conversation.

```bash
Eira --read-only                       # open chat with writes denied
Eira --shell docker                    # approved commands in Docker
Eira run 'Explain this project.'       # one task, also usable in scripts
```

Long sessions keep working: the session prefix is frozen so provider prompt caches stay warm, and near the context limit Eira summarizes older turns and continues, keeping every original message in the local trace. Streaming model tokens, a full-screen editor, MCP, and coding-agent benchmark parity are not included in this release.

## Try it immediately

Requires **Python 3.11+ on Linux, macOS, or WSL**. There are no third-party runtime dependencies. From this repository:

```bash
python3 -m eira_harness demo --workspace /tmp/eira-demo
python3 -m eira_harness sessions --workspace /tmp/eira-demo
python3 -m eira_harness --help
```

The demo calls real runtime tools through a scripted provider. It needs no account, API key, or network connection. It creates a synthetic CSV and local session state in the selected demo directory.

For the `eira` command, install into a virtual environment:

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -e .
eira --version
```

If you use uv, `uv tool install .` also installs the command. Build dependencies come from your configured Python package registry; the running harness uses only the Python standard library.

## Connect a model

Choose a tool-capable model from a supported provider. Run `eira providers` to list profiles, endpoints, and credential variables; exact model IDs come from your provider. See [provider setup](docs/PROVIDERS.md).

```bash
export EIRA_MODEL='your-tool-capable-model-id'
# Set OPENAI_API_KEY securely for the default OpenAI profile.
# Never put a real key in a project file or a committed shell script.

eira init --workspace /path/to/project
eira run 'Inspect this project and explain its architecture.' \
  --workspace /path/to/project --read-only
eira chat --workspace /path/to/project
```

The default endpoint is `https://api.openai.com/v1`. To use a compatible remote or local server:

```bash
export EIRA_PROVIDER='ollama'
# Ollama defaults to http://127.0.0.1:11434/v1
export EIRA_MODEL='your-local-model-id'
eira run 'Read the README and identify one concrete improvement.' --read-only
```

HTTPS is required for remote model endpoints. HTTP is accepted only for loopback hosts. The adapter uses the documented [Chat Completions tool-calling format](https://developers.openai.com/api/docs/guides/function-calling). Compatibility is verified with a local HTTP test server. A real provider requires your credentials and model; no live-provider test was performed during initial development.

## What works

| Capability | Implementation |
|---|---|
| Agent execution | Sequential tool loop, schema validation, bounded steps/calls/context/reported tokens |
| Models | OpenAI, Anthropic (thinking blocks, prompt caching), OpenRouter, Gemini, Ollama, and custom endpoints |
| Coding | Exact-match `edit_file`, paged reads of large files, glob (`**`, rooted `./`) and case-insensitive search with match columns, reviewed create/replace with fast, readable diffs and stale-content checks |
| Long sessions | Frozen per-session prefix, append-only history, summary compaction with originals preserved |
| Evaluation | `eira eval`: task suites in throwaway workspaces with pass rate, tool errors, tokens, and time |
| Execution | Disabled by default; per-command approval in Docker, integration-tested against a real daemon |
| Financial data | Daily stock CSV from Alpha Vantage; daily crypto CSV from Coinbase |
| Research | Approved public HTTPS URL fetching, textual extraction, source URLs |
| Strategy testing | Long/cash SMA crossover on daily CSV bars, costs, exposure limit, drawdown stop |
| Continuity | Resume conversations, workspace notes, project guidance in `EIRA.md` |
| Inspection | SQLite history, JSONL runtime events, JSON trace export |
| Recovery | Interrupted tool outcomes marked unknown; no automatic side-effect replay |
| Extensibility | Python provider interface and validated tool registry |

**Not implemented:** autonomous web search, browser/desktop control, MCP transport, scheduled jobs, multi-agent delegation, streaming market feeds, forward paper trading, brokerage/wallet execution, or production-grade security isolation. The roadmap is in [docs/ROADMAP.md](docs/ROADMAP.md).

## Coding and approvals

```bash
eira run 'Fix the parser bug and run the relevant tests.' \
  --workspace /path/to/project --shell docker
```

Docker must be installed and the image must already be pulled:

```bash
docker pull python:3.11-slim
```

By default, every shell command displays JSON-escaped review text that preserves invisible characters and requires `y` from an interactive terminal. Docker mode runs as your UID, drops capabilities, disables container networking, limits CPU/memory/processes, makes the container filesystem read-only, and mounts the workspace writable. It does not pull images automatically. Choose an image containing your project's tools with `--docker-image`.

Inside the container, secret files that the file tools block (such as `.env` and keys) read as empty. VCS metadata, agent instructions, and IDE, hook and CI config are read-only, and the approval text summarizes these protections; see [the sandbox mount plan](docs/SANDBOX.md) for the exact lists and what remains writable.

Host shell mode is removed in v0.2. Approved Docker commands can still access the mounted workspace; use a dedicated project directory without secrets. The file-tool restrictions do not constrain arbitrary approved shell code. `tests/test_docker_integration.py` runs the real shell tool against a Docker daemon in CI and checks the network, capability, read-only-root, state-hiding, timeout, output-limit, and cleanup behavior described here.

`edit_file` replaces an exact, unique piece of text, so the model sends only the change; `write_file` creates files or replaces a whole file. File edits show a unified diff as JSON-escaped lines before approval. Files with recognized secret values are marked uneditable to prevent redaction placeholders from corrupting them. `write_file` requires the SHA-256 from `read_file` for existing files and `expected_sha256: "new"` for new ones; `edit_file` accepts the hash optionally and always rechecks the file after approval. Concurrent edits cancel the write. Large files are read in pages; the hash always covers the whole file.

`list_files` shows safe dotfiles such as `.github/workflows` and `.pre-commit-config.yaml`, honors `.gitignore`, skips dependency and cache directories, and pages large repositories with `offset` and `next_offset`; pass `ignored: true` to include ignored files. Credential, state and VCS paths are never listed or searched, whatever the flags. `search_files` matches literal text or, with `regex: true`, a Python regular expression run in a separate, killable interpreter with a 10-second timeout. It can add `context` lines, return per-file counts (`output: "files"`) or totals (`"count"`), and reports every skipped file by reason. Only the workspace's `.gitignore` files are read, not `.git/info/exclude` or a global excludes file. Every tool numbers lines the same way: only `\n`, `\r\n` and `\r` end a line. See [docs/CODE-SEARCH.md](docs/CODE-SEARCH.md).
Every edit to a Python, JSON or TOML file is parsed before approval. An edit that breaks a file that parsed is refused, and the file is unchanged. The error shows the failing line inside its enclosing `class` and `def`. New files and files that were already broken get a warning instead, so dialects such as JSONC and newer Python syntax are not blocked. `--syntax-guard warn` downgrades refusals to warnings and `off` disables the check. `--lint-cmd '*.py=ruff check --quiet {path}'` runs a linter in the Docker sandbox after each write and returns its output to the model; a lint run needs the same approval as any shell command. See [edit checks](docs/CHECKS.md).

For controlled automation, `--approve-writes` preapproves workspace file and memory changes. It never preapproves shell commands or network requests. `--read-only` denies file/memory writes and shell calls; it still saves session history. Prompts and tool data still go to your selected model endpoint.

When stdin is piped, approvals fail closed. Use explicit write/hostname flags for intended automation. There is no global `--yes` or automatic host-shell approval.

For an autonomous test-fix loop, `--shell-approval sandboxed` runs container commands without a prompt, but only while the protected mount plan is active. Destructive commands, such as `rm -rf .` or `git clean -fdx`, still ask, and so does the next command after one creates protected config such as `.vscode/`. A headless run denies those. File edits keep their own approval:

```bash
eira run 'Run the tests, fix the failures, and repeat until they pass.' \
  --workspace /path/to/project --shell docker --shell-approval sandboxed --approve-writes
```

Commands that run automatically can still change unprotected workspace files. Use a project under version control, and enable checkpoints if available so automatic changes can be reverted. The rules, alerts and limits are in [docs/SANDBOX.md](docs/SANDBOX.md#approval-modes).

## Research

```bash
eira run 'Read https://www.python.org/about/ and summarize it with a source link.' \
  --allow-host www.python.org
```

Without `--allow-host`, each URL request needs approval. Host grants are exact matches and cover GET requests to that hostname, including their paths and queries. Research fetching blocks private and link-local addresses, pins the checked public IP for the TLS connection, and refuses redirects. It sends no browser cookies or model API credentials. Some sites block automated clients; this is a text fetcher, not a browser or search engine.

## Download daily financial data

```bash
eira prices coinbase BTC-USD --output btc-daily.csv
eira backtest btc-daily.csv --fast 10 --slow 30 --periods-per-year 365

# Set ALPHAVANTAGE_API_KEY securely in your environment first.
eira prices alphavantage IBM --output ibm-daily.csv
```

Each command contacts only its named source. Existing output files are never overwritten. Coinbase needs no key; Alpha Vantage uses its own key. Data-source limits, raw-price adjustments, and incomplete-bar handling are described in [the data guide](docs/MARKET-DATA.md). Downloaded data is for your use under the source's terms; Eira does not redistribute a bundled live dataset.

The agent can call `market_prices` after approval. `--allow-data-source coinbase` or `--allow-data-source alphavantage` grants that source in advance, separately from `--approve-writes`. Fetching CSV does not itself authorize saving it.

## Backtest without a model

The included example prices are **synthetic**. Download daily data as above or supply your own cleaned daily data:

```csv
date,close
2025-01-01,100.00
2025-01-02,101.25
2025-01-03,100.75
```

Dates must be strictly increasing ISO dates. Prices must be finite and between `1e-12` and `1e12`. You need more bars than the slow window.

```bash
eira backtest examples/synthetic.csv --fast 5 --slow 20 \
  --fee-bps 10 --slippage-bps 5 --periods-per-year 365 \
  --output examples/my-backtest.json

eira backtest examples/synthetic.csv --fast 5 --slow 20 \
  --fee-bps 10 --slippage-bps 5 --periods-per-year 365 \
  --output examples/my-backtest.md
```

Outputs never overwrite an existing file. JSON includes the entire equity curve, order ledger, parameters, assumptions, and a hash of the exact input text. Markdown includes metrics and orders. The agent's `backtest_sma` tool returns a bounded summary.

Signals calculated through bar `t-1` execute at the close of bar `t`. The strategy holds a fixed number of fractional units between entry and exit, with cash left over according to `--exposure`. Entry and exit both incur fees and adverse slippage. The final position is liquidated. The drawdown stop checks closing marks and can overshoot after gaps and costs. Once triggered, the strategy remains in cash for the rest of the test.

Sharpe uses arithmetic bar returns, sample standard deviation, zero risk-free rate, and the explicit annualization factor (`252` by default; `365` for calendar-day crypto data). A zero-variance series returns `null`. The benchmark is 100% buy-and-hold with the same costs. There are no taxes, dividends, funding rates, shorts, leverage, intraday fills, or liquidity modeling.

## Sessions and traceability

```bash
eira sessions --workspace /path/to/project
eira run 'Continue with the next change.' --session SESSION_ID --workspace /path/to/project
eira trace SESSION_ID --workspace /path/to/project > trace.json
eira memory --workspace /path/to/project
eira memory --forget old-note --workspace /path/to/project
```

State is workspace-local in `.eira/state.db`. The database stores user messages, assistant messages, tool arguments/results, and execution events. It is permission-restricted but **not encrypted or tamper-proof**. Known environment secrets are redacted on a best-effort basis; arbitrary secrets embedded in project files may not be recognized. Do not commit `.eira/` or share unreviewed traces.

Before each batch of file edits or shell commands, Eira snapshots the workspace into `.eira/history`, so changes made by shell commands are covered too. It never runs git or touches `.git`. Rewind the code, the conversation, or both to any turn:

```bash
eira checkpoints --workspace /path/to/project          # turns and steps, with file counts
eira diff --stat --workspace /path/to/project          # what changed since the first checkpoint
eira rewind turn:2 --both --workspace /path/to/project # shows a summary, then asks to confirm
```

In chat, use `/checkpoints`, `/diff`, and `/rewind N`. A conversation rewind only hides later messages from the model, and the original prompt comes back in input history. Every code rewind first takes a backup checkpoint, so it can itself be undone. `--no-checkpoints` turns snapshots off. See [docs/CHECKPOINTS.md](docs/CHECKPOINTS.md).

A per-session process lock prevents concurrent writers. Each assistant message is journaled before its tool calls run. On resume, any call without a recorded result is marked `outcome_unknown` and is not replayed. Inspect the filesystem or external state before retrying such an action. Resuming a session uses the current CLI permissions and model settings, not saved authority from the old conversation.

## Automation and limits

```bash
eira run 'Inspect the tests and summarize coverage gaps.' --read-only --json \
  --max-steps 12 --max-tool-calls 30 --max-context-chars 100000
```

JSONL events go to stdout; interactive approval prompts go to stderr. Exit codes are `0` for completion, `2` for an error, `3` for a runtime budget stop, and `130` for interruption. Agent completion means the model ended its turn; it is not an independent correctness guarantee. Tool failures remain visible in the trace even if the model ends normally.

Long tool output keeps its first and last lines with a marker in between, so failures at the bottom stay visible. Shell results report `exit_code`, `output`, `truncated`, `stopped`, `total_bytes`, `total_lines` and `output_id`. A command is stopped once it prints more than 4 MiB. When output is shortened, the full redacted text is saved under `.eira/outputs` for 7 days (at most 64 MiB per session), and the model pages or searches it with the `read_output` tool instead of rerunning the command.

`--max-tokens` sums provider-reported usage across requests in the current run, including cached prompt tokens. It is checked between requests, can overshoot by one response, and cannot enforce usage when a provider omits it. It is **not a billing cap**; use provider-side spend limits for that. Context is bounded in characters and includes tool schemas.

At 80% of `--max-context-chars`, Eira asks the model to summarize the conversation and continues from that summary, the latest workspace guidance and memory, and the user's latest request, quoted in full. The summary is labelled as a record of earlier work rather than new instructions. Nothing is deleted: earlier messages stay in `.eira/state.db` and in `eira trace`, and the `context_compacted` event records what was summarized. A summary can omit details, so for work that must not lose evidence use `--no-compact`, which stops at the limit with history preserved.

The system prompt, `EIRA.md`, and memory are captured when a session starts. If they change later, the next task in that session appends a labelled update instead of rewriting earlier context, which keeps prompt caches valid. A consequence is that a forgotten memory or removed guidance text stays in that session's frozen prompt; start a new session to drop it. `--model-timeout` (default 600 seconds) bounds each model request, including retries. For the Anthropic profile, `--max-output-tokens` (default 16,000) sets the per-response limit and `--no-prompt-cache` turns off cache markers. Requests are not streamed yet, so a very large output limit on a slow model can run into the timeout.

## Measure it

```bash
eira eval --provider anthropic --model "$MODEL" --output report.json
```

`eira eval` runs a task suite against your model, each task in a throwaway workspace where only file writes are preapproved, and scores it with declarative checks on files and the final answer. The built-in starter suite covers bug fixing, adding code, a multi-file rename, answering from code, creating a file, finding a line in a large file, and honestly reporting a protected file it cannot edit. Write your own suites for decisions that matter; see [the evaluation guide](docs/EVALS.md).

## Develop and verify

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q eira_harness
```

The tests cover financial accounting, lagged signals, risk stops, permissions, path/credential protections, exact and stale edits, paged reads, recovery, memory, locks, budgets, compaction, prefix stability, Anthropic thinking and caching, evaluation scoring, CLI reports, and HTTP request/response behavior against a local test server. They need localhost socket access. Real model quality needs your own `eira eval` runs.

Docker shell tests run when you name a pre-pulled image:

```bash
EIRA_DOCKER_IMAGE=python:3.11-slim python3 -m unittest tests.test_docker_integration -v
```

Read [the architecture and extension guide](docs/ARCHITECTURE.md) and [security boundaries](docs/SECURITY.md) before adding powerful tools.

See [the v0.4 changes](docs/RELEASE-0.4.md), [the v0.3 terminal changes](docs/RELEASE-0.3.md), and [the v0.2 changes](docs/RELEASE-0.2.md) for the audit remediation summary.

## License

MIT. See [LICENSE](LICENSE).
