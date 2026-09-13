# Eira

**A local agent harness for building, researching, and testing ideas.**

Eira gives a tool-capable language model a working directory, an execution loop, durable sessions, reviewed file edits, and financial research tools. You choose the model and endpoint. Conversations and tool traces stay in a local SQLite database; relevant conversation content is sent to the model endpoint you configure.

This is a working **v0.2 developer release**, inspired by the general-agent and financial-workflow scope of Minara Harness. It is independently implemented. It does not claim Minara feature parity or benchmark superiority.

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
~/.local/bin/eira --version
```

The installer downloads a pinned source commit and installs into your own Python environment under `~/.local/share/eira`. It requires no sudo. Add `~/.local/bin` to `PATH` if it is not already present. To inspect the installer first, download it to a file and read it before running `sh install.sh`. See [installation and upgrades](docs/INSTALL.md).

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
| Models | OpenAI, Anthropic, OpenRouter, Gemini, Ollama, and custom endpoints |
| Coding | File listing, literal search, reading, reviewed create/replace with stale-content checks |
| Execution | Disabled by default; per-command approval in Docker |
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

Every shell command displays JSON-escaped review text that preserves invisible characters and requires `y` from an interactive terminal. Docker mode runs as your UID, drops capabilities, disables container networking, limits CPU/memory/processes, makes the container filesystem read-only, and mounts the workspace writable. It does not pull images automatically. Choose an image containing your project's tools with `--docker-image`.

Host shell mode is removed in v0.2. Approved Docker commands can still access the mounted workspace; use a dedicated project directory without secrets. The file-tool restrictions do not constrain arbitrary approved shell code. Docker execution has not been integration-tested against a daemon in this development environment.

File edits show a unified diff as JSON-escaped lines before approval. Files with recognized secret values are marked uneditable to prevent redaction placeholders from corrupting them. Existing files require a SHA-256 from `read_file`; concurrent edits cancel the write. New files require `expected_sha256: "new"`.

For controlled automation, `--approve-writes` preapproves workspace file and memory changes. It never preapproves shell commands or network requests. `--read-only` denies file/memory writes and shell calls; it still saves session history. Prompts and tool data still go to your selected model endpoint.

When stdin is piped, approvals fail closed. Use explicit write/hostname flags for intended automation. There is no global `--yes` or automatic host-shell approval.

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

A per-session process lock prevents concurrent writers. Each assistant message is journaled before its tool calls run. On resume, any call without a recorded result is marked `outcome_unknown` and is not replayed. Inspect the filesystem or external state before retrying such an action. Resuming a session uses the current CLI permissions and model settings, not saved authority from the old conversation.

## Automation and limits

```bash
eira run 'Inspect the tests and summarize coverage gaps.' --read-only --json \
  --max-steps 12 --max-tool-calls 30 --max-context-chars 100000
```

JSONL events go to stdout; interactive approval prompts go to stderr. Exit codes are `0` for completion, `2` for an error, `3` for a runtime budget stop, and `130` for interruption. Agent completion means the model ended its turn; it is not an independent correctness guarantee. Tool failures remain visible in the trace even if the model ends normally.

`--max-tokens` sums provider-reported usage across requests in the current run. It is checked between requests, can overshoot by one response, and cannot enforce usage when a provider omits it. It is **not a billing cap**; use provider-side spend limits for that. Context is bounded in characters and includes tool schemas. At the limit, Eira stops and preserves history instead of silently dropping evidence. Start a new session with reviewed notes for longer work.

## Develop and verify

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q eira_harness
```

The tests cover financial accounting, lagged signals, risk stops, permissions, path/credential protections, stale edits, recovery, memory, locks, budgets, CLI reports, and HTTP request/response behavior against a local test server. They need localhost socket access. Docker execution and real model quality need environment-specific validation.

Read [the architecture and extension guide](docs/ARCHITECTURE.md) and [security boundaries](docs/SECURITY.md) before adding powerful tools.

See [the v0.2 changes](docs/RELEASE-0.2.md) for the audit remediation summary.

## License

MIT. See [LICENSE](LICENSE).
