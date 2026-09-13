# Security boundaries

Eira is an early local developer tool. It has not received an external security audit. Its default capabilities are narrow; this does not make an LLM or arbitrary generated code trustworthy.

## Enforced by application code

- Workspace file tools reject absolute paths, parent traversal, symlinks, hard links, known credential locations, and internal state/VCS paths.
- File writes require approval and an expected content hash; the hash is rechecked after approval. `--approve-writes` is an explicit exception for file and memory writes only.
- `--read-only` takes precedence over write grants and blocks shell execution. Session logging still writes to `.eira/`.
- Shell is disabled by default. Enabling it still requires interactive approval for every command. Piped input cannot approve.
- Research GETs need approval or an exact hostname grant. URLs must use HTTPS port 443 and no embedded credentials. All resolved addresses must be public; TLS connects to a checked IP using the original hostname for certificate validation. Redirects are refused.
- Model endpoint configuration is user-controlled. Remote endpoints require HTTPS. Model API credentials are used only by that adapter.
- Tool calls and context have bounded sizes. Runtime steps, tool calls, and reported token usage are bounded. These are not a strict billing guarantee.
- Known environment credential values are redacted before journal serialization. Terminal control sequences are stripped from rendered text.
- Interrupted tool calls are never automatically replayed.

## Important limits

Path checks and atomic writes are not a hardened filesystem sandbox. A hostile process concurrently swapping filesystem objects can race checks. Do not run Eira against a workspace controlled by an adversary. The `.eira/` database is not encrypted, tamper-proof, or an authority boundary against someone who controls your OS account.

Host shell mode has your OS user's filesystem and network permissions. Removing credential environment variables does not stop a program from reading files or connecting to services available to that user. Docker mode isolates execution more strongly, but the workspace is writable and its files are visible to commands. Use a dedicated workspace with no secrets, review the image and commands, and keep Docker patched. Rootless Docker or a VM can improve isolation. A malicious custom image may have behavior beyond the supplied command.

The mount at `/workspace/.eira` hides Eira's state directory inside Docker. It does not hide every sensitive path in the workspace, nor can Docker isolate secrets you intentionally mount. The initial Docker path is implemented but has not been integration-tested in the development environment because Docker was unavailable.

Prompt instructions tell the agent to treat retrieved content and memory as untrusted, and to respect denials. Prompt instructions are not a complete prompt-injection defense. Native policy gates remain outside the model. An approved shell command can bypass file-tool boundaries, so review commands as programs, not just their natural-language explanations.

Secret redaction is best effort. Unknown secrets in files, encodings, transformed values, and pasted data can remain in model input or traces. Your model provider receives the relevant conversation, file excerpts, tool schemas, results, project guide, and remembered notes. Pick an endpoint whose data handling fits your needs. Never store API keys in `EIRA.md` or memory.

## Financial boundary

There is no native tool for placing trades or transferring funds. The stock backtester uses supplied CSV data and simulated capital. It does not connect to wallets or brokers. Host commands can technically access arbitrary software, so the harness also instructs the agent not to use shell execution for financial transactions. That instruction cannot replace an OS-level restriction or a broker-side risk control.

Before adding any execution connector, implement a separate capability and approval system, exact asset resolution, idempotent intent records, position/exposure limits enforced by code, a kill switch, and independent security review. Do not infer permission to move money from permission to research or code.

## Reporting issues

Use a private channel to the repository owner for vulnerabilities. Do not publish credentials, private traces, or exploitable details in an ordinary public issue. GitHub security advisories can be enabled by the owner when the repository is ready for external collaboration.
