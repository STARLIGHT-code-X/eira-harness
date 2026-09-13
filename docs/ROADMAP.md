# From developer foundation to a serious harness

The initial goal is an inspectable, provider-configurable local harness that can do real filesystem work and numerical strategy testing. Shipping more tool names is not evidence of better task performance.

## Next: prove coding and research quality

1. Run a fixed evaluation set against chosen tool-capable models: repository bug fixes, data investigations, cited research, and strategy revisions. Record task success, invalid tool calls, cost, latency, and denied-action handling.
2. Add streaming and reversible context compaction that preserves original messages and source references. v0.2 normalizes supported provider usage counters.
3. Expand file editing to structured patches with independent review and rollback artifacts.
4. Integration-test Docker execution on Linux and macOS; support a reproducible development image and rootless environments.

## Then: integrate external tools

1. Add stdio/HTTP MCP adapters with server allowlists, explicit credentials, per-tool capability classification, and approval gates before spawning servers or invoking mutations.
2. Add native model adapters where compatible endpoints cannot preserve the provider's tool or reasoning protocol.
3. Add search and browser capabilities with source capture and user-controlled network access.
4. Add explicit skill loading and versioned workspace guidance. Skills must never silently grant permissions.

## Financial depth

1. Extend the v0.2 Alpha Vantage and Coinbase daily-price connectors with adjusted data and validation for splits, dividends, missing observations, and exchange calendars.
2. More deterministic strategy families, parameter sweeps, walk-forward splits, turnover and exposure reporting, and out-of-sample evaluation.
3. A persistent forward paper-trading service with event timestamps, replayable fills, and failure recovery.
4. Only after validation: separately reviewed live execution adapters with broker-side limits and explicit authorization. This is not part of v0.2.

## Operations

Long-running jobs need leases, durable queues, idempotency, cancellation, and independent observability. Multi-agent work needs authority propagation and isolated workspaces. Team use needs authentication and encrypted storage. These are separate engineering projects, not hidden features of the local SQLite loop.

The [Minara Harness product description](https://minara.ai/blog/introducing-minara-harness/) informed the broad competitor scope. Eira's implemented capabilities and test results should be measured directly; no comparative benchmark claim is made.
