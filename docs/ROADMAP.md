# From developer foundation to a serious harness

The initial goal is an inspectable, provider-configurable local harness that can do real filesystem work and numerical strategy testing. Shipping more tool names is not evidence of better task performance.

## Next: prove coding quality

Built through 0.5: `eira eval` with a starter suite; summary compaction with a frozen, cache-friendly prefix; `edit_file` and all-or-nothing `apply_patch`; a pre-approval syntax guard; checkpoints and rewind covering shell side effects; a Docker mount plan that masks secrets and makes config read-only; sandboxed autorun; regex code search; head-and-tail output with `read_output`; `AGENTS.md` discovery; Docker integration tests in CI.

1. **Measure**: behavioral evals shipped in 0.5 (`command_succeeds`, the `coding` suite, pass@k and Wilson intervals, `--jobs`, `--harness codex`, `--compare`). Next: run them against real models, publish Eira and Codex reports side by side on the same model, and grow the suite with real-bug tasks from open repositories.
2. **Provider wave**: token streaming for both wire formats, reasoning-effort control, live tool output, mid-turn steering and queued follow-ups.
3. **Plan and goal modes** with a machine-checkable definition of done built on sandboxed checks; a full JSON Schema subset in the tool registry.
4. Docker on macOS and rootless, a reproducible development image, and a non-Docker sandbox backend (bubblewrap) that reuses the mount plan.

## Then: integrate external tools

1. Add stdio/HTTP MCP adapters with server allowlists, explicit credentials, per-tool capability classification, and approval gates before spawning servers or invoking mutations.
2. Add native model adapters where compatible endpoints cannot preserve the provider's tool or reasoning protocol.
3. Add search and browser capabilities with source capture and user-controlled network access.
4. Add explicit skill loading and versioned workspace guidance. Skills must never silently grant permissions.

## Financial depth

1. Extend the v0.2 Alpha Vantage and Coinbase daily-price connectors with adjusted data and validation for splits, dividends, missing observations, and exchange calendars.
2. More deterministic strategy families, parameter sweeps, walk-forward splits, turnover and exposure reporting, and out-of-sample evaluation.
3. A persistent forward paper-trading service with event timestamps, replayable fills, and failure recovery.
4. Only after validation: separately reviewed live execution adapters with broker-side limits and explicit authorization. This is not part of v0.5.

## Operations

Long-running jobs need leases, durable queues, idempotency, cancellation, and independent observability. Multi-agent work needs authority propagation and isolated workspaces. Team use needs authentication and encrypted storage. These are separate engineering projects, not hidden features of the local SQLite loop.

The [Minara Harness product description](https://minara.ai/blog/introducing-minara-harness/) informed the broad competitor scope. Eira's implemented capabilities and test results should be measured directly; no comparative benchmark claim is made.
