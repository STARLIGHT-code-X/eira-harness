# Eira 0.5

0.5 adds the coding-agent capabilities that matter most in day-to-day work. Each was chosen from a survey of Codex CLI, Claude Code, Aider and SWE-agent and built against a written spec. Each keeps Eira's guarantees: approvals, redaction, the Docker-only shell, and an append-only session prefix.

## Editing

- **`apply_patch`** ([PATCHES.md](PATCHES.md)) accepts the `*** Begin Patch` format that OpenAI's coding models are trained on. One patch can add, update, delete and move several files. Matching tolerates whitespace drift, and a failed match reports the closest candidates with line numbers. Every file is checked before one combined approval. Then all files are written or none: if a write fails, completed steps are rolled back. Codex applies hunks one at a time and can leave earlier files changed. A `cd dir && apply_patch <<'EOF'` shell command is routed to the tool, so it is reviewed like any other write.
- **Syntax guard** ([CHECKS.md](CHECKS.md)): an edit, write or patch that would make a parseable Python, JSON or TOML file unparseable is refused before approval. The error is shown inside the enclosing `class` or `def`. `--lint-cmd` can run a linter in the sandbox after each write.
- **Checkpoints and rewind** ([CHECKPOINTS.md](CHECKPOINTS.md)): before every file-changing batch, including shell commands, Eira snapshots the workspace into a content-addressed store under `.eira/history`. It never runs git and never touches `.git`. `eira rewind --code|--conversation|--both` and `/rewind` restore state, and `eira diff` shows what changed. Conversation rewind is append-only, so prompt caches stay valid.

## Navigation and output

- **Code search** ([CODE-SEARCH.md](CODE-SEARCH.md)): regex search runs in a separate, killable interpreter with a timeout. Search adds context lines, per-file counts, `.gitignore` support, safe dotfiles such as `.github/workflows`, paging for large repositories, and explicit counts of skipped files.
- **Head and tail output**: long shell output, pages and tool results keep both ends, so test failures at the bottom survive. The full redacted output is saved under `.eira/outputs`, and `read_output` pages or searches it without rerunning the command.

## Sandbox and autonomy

- **Mount plan** ([SANDBOX.md](SANDBOX.md)): inside the shell container, secret files read as empty, and VCS metadata, agent instructions, CI workflows, hook managers and IDE config are read-only. Credential-bearing `.git/config` files are replaced by a sanitized copy, and newly created protected paths are reported. File-tool writes to those config paths are never preapproved by `--approve-writes`.
- **Sandboxed autorun**: `--shell-approval sandboxed` runs commands in that protected container without a per-command prompt. Destructive commands, such as a recursive delete of the workspace root or `git clean -fdx`, and pending trust-handoff alerts still ask, and headless runs deny them. The container stays offline, without capabilities, and with a read-only root. Every decision is journaled as `approval_decided`.

## Instructions

- **AGENTS.md discovery** ([INSTRUCTIONS.md](INSTRUCTIONS.md)): Eira reads a global file, then one file per directory from the project root down to the workspace. `EIRA.md`, `AGENTS.override.md` and `AGENTS.md` come first, with `CLAUDE.md` and `GEMINI.md` as fallbacks. Instructions are capped at 32 KiB and frozen into the session prefix. Guidance in subdirectories is delivered with the first tool result that touches them. `eira instructions` and `/instructions` show what was loaded.

## Integration

The features were built in parallel on shared integration seams ([EVENTS.md](EVENTS.md); "Adding a tool" in [ARCHITECTURE.md](ARCHITECTURE.md)), then merged and tested together. `tests/test_integration_features.py` drives the real CLI against a loopback model. It covers a multi-file patch followed by a broken patch that the syntax guard refuses as a whole, rewinding that work, and a sandboxed `rm` that rewind undoes. It also checks that a shell-routed patch is still denied without write approval, and that subdirectory guidance arrives with a patch result.

Verification: 396 unit tests, plus the Docker integration suite, pass against a real daemon both as root and as a non-root user. CI runs the unit tests on Python 3.11 to 3.14 and the Docker suite on GitHub's runners.

## Upgrade notes

- Sessions started before 0.5 take one prompt-cache miss when resumed, because the tool list grew. After that the prefix is stable again.
- `.eira/` now also holds `history/` (checkpoints), `outputs/` and `patches/` (the journal used for rollback). It remains private, workspace-local, and excluded from file tools and the shell sandbox.

## Not in 0.5

No live model was run for this release, so there are no measured pass rates yet. Run `eira eval` with your model to get them. Behavioral evals (sandboxed test checks, pass@k, side-by-side runs with `codex exec`) were specified but not built; see [ROADMAP.md](ROADMAP.md). Token streaming, MCP, plan and goal modes, and web search remain future work.
