# Security boundaries

Eira 0.2 is an early local developer tool. It has received an internal code review and regression testing, not an independent security certification. Use a dedicated project workspace without secrets.

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

Docker must be installed and its chosen image pre-pulled. The Docker integration is implemented and its command construction is tested, but it was not exercised against a real Docker daemon in this development environment. A selected image remains a trusted dependency. Docker and the host OS must be maintained by the operator.

The workspace mount is writable and visible to approved shell commands, including files that file tools would block. The `.eira` directory is covered by a container tmpfs, but other workspace secrets are not automatically hidden. Workspace disk consumption by arbitrary programs is not quota-controlled. Do not mount your home directory or a workspace containing credentials. A container is not equivalent to a VM, and these controls do not establish production-grade isolation.

Path checks can race a hostile local process. The state database is permission-restricted but not encrypted, tamper-proof, or an authority boundary against the same OS account. Redaction is best-effort: unknown, transformed, encoded, or pasted secrets may remain. Guidance, fetched text, and memory are untrusted context; prompt instructions alone are not a complete prompt-injection defense.

Changing providers or resuming sessions sends existing relevant history to the newly selected provider. Choose endpoints and data handling deliberately. Never store credentials in `EIRA.md`, saved notes, examples, or traces intended for sharing.

Financial adapters only retrieve daily prices from fixed sources. They do not place orders, transfer assets, or connect to a brokerage account. Prices may be delayed, incomplete, or unadjusted. Backtests omit dividends, taxes, liquidity, leverage, and intraday execution; historical results do not establish future returns. Adding trading requires a separate execution and risk-control design.

## Reporting

Report suspected issues privately to the repository owner. Do not include real credentials or private traces in public issues. The owner may enable GitHub private vulnerability reporting for coordinated reports.
