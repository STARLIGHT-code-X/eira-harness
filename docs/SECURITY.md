# Security boundaries

Eira 0.4 is an early local developer tool. It has received an internal code review and regression testing, not an independent security certification. Use a dedicated project workspace without secrets.

## Enforced controls

- File tools reject traversal, symlinks, hard links, protected configuration paths, common credential files, and Eira/VCS state. Standard home configuration directories and configured credential paths are protected even when selected through an ancestor workspace.
- Reads and directory scans have byte, entry, depth, and time limits. These are application checks, not a hardened filesystem sandbox against concurrent object replacement.
- Approval material uses JSON-escaped lines so invisible characters remain visible. Shell and financial-source approvals are separate from file-write grants.
- Files containing recognized secret values are marked uneditable by model tools. Full-file writes containing redaction placeholders or recognized credentials are rejected. Optimistic hashes are rechecked after approval; creation fails atomically if another file already exists.
- Shell is disabled by default. The only supported execution mode is Docker with an interactive approval for each command. Host shell mode has been removed. There is no global autoapprove flag.
- Docker runs without networking, with dropped capabilities, resource limits, a read-only container root, an explicit shell entrypoint, and Docker logging disabled. Output is captured through a bounded pipe, not an unbounded temporary file. Cleanup removes the named container; failed cleanup is reported.
- Public research and financial-data requests require public HTTPS port 443, resolve and validate destination addresses, pin the connection to a checked address, and refuse redirects. Byte limits and total deadlines apply. Stalled DNS workers are capped and cannot prevent process exit.
- Model endpoints use HTTPS except actual loopback HTTP. Provider profiles select their own credential variable. Endpoint overrides use only `EIRA_API_KEY`, never a named provider's key. The selected model endpoint receives relevant conversation and tool data.
- JSON parsing and stored data have depth bounds. Provider messages and usage are validated before use. Runtime limits bound steps, tool calls, context, and reported token usage; the latter is not a strict billing cap.
- `--read-only` denies file/memory edits and shell execution. Session journaling still writes. It does not mean offline: model requests and separately granted research/data requests remain possible.
- Interrupted tools are recorded as having unknown outcomes and are never replayed automatically.

## Limits that remain

Docker must be installed and its chosen image pre-pulled. `tests/test_docker_integration.py` exercises the shell tool against a real daemon (in CI, and locally when `EIRA_DOCKER_IMAGE` is set). It verifies blocked networking, an empty effective capability set, a read-only container root, the hidden `.eira` directory, timeout and output limits, container removal, and that denied commands start nothing. It was run against a Linux daemon during development, and the CI workflow runs it on GitHub's Ubuntu runners; macOS and rootless Docker are not yet covered. A selected image remains a trusted dependency. Docker and the host OS must be maintained by the operator.

The workspace mount is writable and visible to approved shell commands, including files that file tools would block. The `.eira` directory is covered by a container tmpfs, but other workspace secrets are not automatically hidden. Workspace disk consumption by arbitrary programs is not quota-controlled. Do not mount your home directory or a workspace containing credentials. A container is not equivalent to a VM, and these controls do not establish production-grade isolation.

Path checks can race a hostile local process. The state database is permission-restricted but not encrypted, tamper-proof, or an authority boundary against the same OS account. Redaction is best-effort: unknown, transformed, encoded, or pasted secrets may remain. Guidance, fetched text, and memory are untrusted context; prompt instructions alone are not a complete prompt-injection defense.

Context compaction sends the conversation so far to the configured model to produce a summary, the same data any normal request already sends. The summary is model-written and can be incomplete or wrong; originals stay in the journal and `--no-compact` disables it. Workspace guidance and memory changes are appended to a session as labelled user messages and remain context, never authority.

`eira eval` runs each task in a fresh temporary workspace with file and memory writes preapproved. Shell, URL fetching, and market data are denied there. Suite fixture paths follow the same workspace rules as file tools. Checks never execute model-written code.

Changing providers or resuming sessions sends existing relevant history to the newly selected provider. Choose endpoints and data handling deliberately. Never store credentials in `EIRA.md`, saved notes, examples, or traces intended for sharing.

Financial adapters only retrieve daily prices from fixed sources. They do not place orders, transfer assets, or connect to a brokerage account. Prices may be delayed, incomplete, or unadjusted. Backtests omit dividends, taxes, liquidity, leverage, and intraday execution; historical results do not establish future returns. Adding trading requires a separate execution and risk-control design.

## Reporting

Report suspected issues privately to the repository owner. Do not include real credentials or private traces in public issues. The owner may enable GitHub private vulnerability reporting for coordinated reports.

## Interactive configuration

Eira 0.3 saves only provider, model, and optional base URL under `$XDG_CONFIG_HOME/eira/settings.json` (default `~/.config/eira/settings.json`), with user-only permissions. This file never grants tool permissions or stores API keys. Setup accepts credentials through a hidden prompt for the current process; environment variables remain supported. Treat user configuration as trusted local state. The normal workspace file tools protect the configuration directory.

The installer creates both `Eira` and `eira` commands and adds the command directory to supported shell startup files. It reports the path setup and preserves unmanaged commands. Terminal input history is process-local, capped at 100 entries, and redacts recognized credentials; no readline history file is written.
