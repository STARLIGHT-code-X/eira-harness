# From developer foundation to a serious harness

The initial goal is an inspectable, provider-configurable local harness that can do real filesystem work and numerical strategy testing. Shipping more tool names is not evidence of better task performance.

## Next: prove coding and research quality

1. Done in 0.4: `eira eval` with a starter suite. Next, publish suites for repository bug fixes, data investigations, cited research, and strategy revisions, and record results per model with `--repeat` variance.
2. Done in 0.4: summary compaction with originals preserved and a frozen, cache-friendly session prefix. Next: streaming, and Docker-executed behavioral checks for evals.
3. Done in 0.4: exact-match `edit_file` with reviewed diffs. Next: multi-hunk patches and rollback artifacts.
4. Done in 0.4: Docker integration tests on Linux, in CI. Next: macOS, rootless Docker, and a reproducible development image.

## Then: integrate external tools

1. Add stdio/HTTP MCP adapters with server allowlists, explicit credentials, per-tool capability classification, and approval gates before spawning servers or invoking mutations.
2. Add native model adapters where compatible endpoints cannot preserve the provider's tool or reasoning protocol.
3. Add search and browser capabilities with source capture and user-controlled network access.
4. Add explicit skill loading and versioned workspace guidance. Skills must never silently grant permissions.

## Financial depth

1. Extend the v0.2 Alpha Vantage and Coinbase daily-price connectors with adjusted data and validation for splits, dividends, missing observations, and exchange calendars.
2. More deterministic strategy families, parameter sweeps, walk-forward splits, turnover and exposure reporting, and out-of-sample evaluation.
3. A persistent forward paper-trading service with event timestamps, replayable fills, and failure recovery.
4. Only after validation: separately reviewed live execution adapters with broker-side limits and explicit authorization. This is not part of v0.4.

## Operations

Long-running jobs need leases, durable queues, idempotency, cancellation, and independent observability. Multi-agent work needs authority propagation and isolated workspaces. Team use needs authentication and encrypted storage. These are separate engineering projects, not hidden features of the local SQLite loop.

The [Minara Harness product description](https://minara.ai/blog/introducing-minara-harness/) informed the broad competitor scope. Eira's implemented capabilities and test results should be measured directly; no comparative benchmark claim is made.
