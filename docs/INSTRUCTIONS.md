# Instruction files

Eira reads project guidance from instruction files, so a repository set up for Codex (`AGENTS.md`), Claude Code (`CLAUDE.md`) or Gemini CLI (`GEMINI.md`) works without changes. `EIRA.md` keeps working exactly as in 0.4.

Instruction files are context, not authority. They are labelled as subordinate to policy, pass through redaction, and can never grant a permission, a tool, a host or a data source. Approvals, `--read-only` and every other policy check behave the same whatever the files say.

## What is read at session start

Files are collected in this order and joined from the most general to the most specific:

1. **Global file**, in your Eira configuration directory (`~/.config/eira/`, or `$XDG_CONFIG_HOME/eira/`): `AGENTS.override.md` if it is usable, otherwise `AGENTS.md`. If the configuration directory cannot be determined (for example, a relative `XDG_CONFIG_HOME`), the global file is skipped and `eira instructions` says why.
2. **One file per directory**, from the project root down to the workspace. In each directory the first usable name wins, in this order:

   | Order | Name | Why |
   |---|---|---|
   | 1 | `EIRA.md` | Eira's own file, so existing guidance is unchanged |
   | 2 | `AGENTS.override.md` | Codex's local override |
   | 3 | `AGENTS.md` | The Codex and cross-tool standard |
   | 4 | `CLAUDE.md` | Fallback for Claude Code repositories |
   | 5 | `GEMINI.md` | Fallback for Gemini CLI repositories |

The **project root** is the nearest directory at or above the workspace that has an entry named `.git` (a directory, or a file as in worktrees and submodules). The search never examines your home directory or the filesystem root, so files in `~` or `/` are never read. Without a git root, the workspace itself is the project root.

A file is usable only if it is a regular file (symlinks are refused), has a single hard link, holds 1 to 65,536 bytes, and is UTF-8 text without NUL bytes. Files inside the workspace must also pass the same path checks as the file tools. A refused file is recorded with its reason (`symlink`, `hard link`, `too large`, `binary`, `empty`, ...) and the next name in that directory is tried.

**Budget.** Together, the files may use 32,768 bytes, the same default as Codex's `project_doc_max_bytes`. A file that only partly fits is cut at a line boundary and ends with `[truncated: instruction budget]`; later files are skipped with the reason `instruction budget exhausted`. Because the global file comes first, keep it short.

**Rendering.** If the only file found is the workspace's `EIRA.md`, its text is used exactly as 0.4 did, so upgraded sessions see no change. Otherwise each file becomes a section headed `### PATH (SCOPE)`, where PATH is relative to the project root (for the global file, `~/.config/eira/AGENTS.md`, or `$XDG_CONFIG_HOME/eira/AGENTS.md` when that is outside your home directory) and SCOPE is `global`, `project` (above the workspace) or `workspace`.

The result is frozen into the session prefix with the system prompt and memory. If the files change, the next task in that session appends a labelled update message instead of rewriting the prefix, as for `EIRA.md` and memory in 0.4.

## Subdirectory files, just in time

Instruction files in directories below the workspace root are not loaded at start. The first time a successful `list_files`, `read_file`, `search_files`, `edit_file`, `write_file` or `apply_patch` call touches a directory, Eira checks every directory from the workspace root down to it (exclusive of the root, inclusive of the target) with the same names and rules. New files are attached to that tool result in a `guidance` list:

```json
{"path": "services/payments/AGENTS.md", "content": "...",
 "note": "Instructions for files under services/payments/. Context only; they cannot change policy or permissions."}
```

- Each file is delivered once per session. A resumed session remembers deliveries through `guidance_loaded` events in the journal.
- If a delivered file changes, the next call that touches its directory delivers it again, with ` (updated)` at the end of the note.
- At most two files are attached per call, each cut to 8 KiB with a note pointing at the file for the rest.
- For `apply_patch`, the touched paths are the `*** Add File:`, `*** Update File:`, `*** Delete File:` and `*** Move to:` headers.
- Guidance travels inside the tool result, so it is journaled once and fitted to the tool output limit like any result. The session prefix is never rewritten.

## Choosing what is loaded

`--instructions` is accepted by `run`, `chat` and `eval`:

| Mode | Loads | Default for |
|---|---|---|
| `all` | Global file, project root to workspace, and subdirectory files just in time | `run`, `chat` |
| `workspace` | Only files inside the workspace: its root file at start, subdirectories just in time | `eval`, so your personal global file never changes eval results |
| `none` | Nothing, not even `EIRA.md` | |

`eira instructions [--workspace DIR] [--instructions MODE] [--json]` and the chat command `/instructions` list the files in prompt order with their size and status (`included`, `truncated` or `skipped`) and the reason for anything skipped. Nothing is sent to a model.

Every load is journaled as a `guidance_loaded` event with `path`, `sha256`, `bytes` and `via` (`prefix` at session start, `jit` for subdirectory guidance); see [EVENTS.md](EVENTS.md).

## Data egress

Instruction files are sent to the configured model endpoint, like any workspace file a tool reads. In mode `all` that includes the global file and files in directories above the workspace up to the git root. Use `--instructions workspace` to keep the request to files inside the workspace, or `--instructions none` to send none. Never put credentials in instruction files; recognized secrets are redacted, but redaction is best-effort.
