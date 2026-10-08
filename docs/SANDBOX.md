# Shell sandbox

## Approval modes

`--shell-approval` chooses who decides whether a Docker shell command runs. It is available on `run`, `chat` and `setup`.

| Mode | Behavior |
|---|---|
| `always` (default) | Every shell command shows its review text and needs `y` from an interactive terminal. This is the 0.4 behavior. |
| `sandboxed` | Commands run without a prompt inside the protected container, unless an escalation rule below fires. Requires `--shell docker`; otherwise Eira exits 2 with `--shell-approval sandboxed requires --shell docker`. |

The banner shows `shell: docker (auto in sandbox)` in sandboxed mode, and `/status` prints the active mode. Each command that runs without a prompt prints `· running in the sandbox without approval`.

### Decision table

`eira_harness/approvals.py` decides before every shell command, evaluating these rules in order. The first match wins.

1. `--read-only`: denied with `Denied by read-only policy.`, in either mode.
2. Mode `always`: ask.
3. The mount plan does not report `protected: True`: ask, with the reason `sandbox protections unavailable`. Autorun depends on the protected mount plan described in this document, which hides secrets and makes VCS, agent and tool config read-only. If that plan is missing or incomplete, Eira asks. It never assumes the protection is there.
4. A trust-handoff alert is pending: ask, with the reason `a previous command created protected config paths: .vscode; review before continuing`.
5. The command matches a destructive pattern: ask, with that pattern as the reason, such as `recursive rm of "."`.
6. Otherwise the command runs automatically.

When Eira asks in sandboxed mode, the approval text ends with `Reason for review: ...`.

### Destructive patterns

This check is a speed bump against accidents, not a security boundary. The command is tokenized with `shlex` (POSIX rules, comments off), and newlines count as command separators. Eira splits it on `;`, `&&`, `||`, `|`, `&` and parentheses. It strips leading `NAME=value` assignments and the wrappers `sudo`, `env`, `nice`, `nohup`, `command`, `exec`, `time`, `timeout DURATION`, `xargs` and `stdbuf`, with their flags. Scripts passed to `sh`, `bash`, `dash`, `zsh`, `ksh` or `ash` through `-c` (or a flag cluster such as `-lc`) are checked too, as are `eval` arguments, up to three levels deep.

| Rule | Asks for | Runs automatically |
|---|---|---|
| `rm` with `-r`, `-R` or `--recursive` and a critical operand: `.`, `..`, `/`, `/workspace`, `~`, `*`, `./*`, `/*`, `/workspace/*`, `.*`, `$HOME`, `$PWD`, `/tmp` (with or without a trailing slash), any path made only of `.` and `..`, or an operand that starts with `$` or contains `$(` or a backtick | `rm -rf .`, `rm -r -f *`, `rm -rf -- *`, `rm -rf "$DIR"/*`, `FOO=1 timeout 5 rm -rf ~`, `sh -lc 'cd x && rm -rf ..'` | `rm -rf build`, `rm -rf build/*`, `rm -f a.txt`, `make test && rm -rf dist/*` |
| `git clean` with `-f`/`--force` plus `-x` or `-X`, unless it is a dry run (`-n`) | `git clean -fdx`, `git clean -fX` | `git clean -n`, `git -C . clean -fd` |
| `find` with `-delete`, or `-exec`/`-execdir`/`-ok` running `rm`, whose starting path is missing or critical | `find . -name '*.pyc' -delete`, `find -delete` | `find src -name '*.pyc' -delete` |
| `shred` | any use | |

The check cannot see through variables set earlier, aliases, functions, scripts the command runs (`make clean`, `python cleanup.py`), or deliberate obfuscation. A command with unbalanced quotes skips the check entirely; the container still applies. What actually limits damage is the container, the protected mount plan and, if enabled, checkpoints.

### Trust-handoff alerts

After each command, the protected mount plan reports any newly created protected config path, such as `.vscode/`, `.github/workflows/`, `.husky/` or `AGENTS.md`, that did not exist before. Host tools, editors, CI and later agent sessions may execute or trust those files. In sandboxed mode, the next shell command then asks, and keeps asking until a human approves one shell command. That approval counts as acknowledging the alert. A denial does not clear it.

Alerts are kept in the session journal, not only in memory. `chat` builds a new toolbox for every prompt, and `--session` resumes with a fresh one. So on its first shell command each toolbox reloads the `sandbox_protected_path_created` events journaled since the session's last approved shell command.

### Headless runs

When stdin is not a terminal, every "ask" becomes a denial. With `eira run ... --shell docker --shell-approval sandboxed`, ordinary commands run and the agent receives their output. Destructive commands and commands after a trust-handoff alert are denied, and the model sees the denial as a tool error. In `always` mode every shell command is denied when stdin is piped, as before.

### What autorun never covers

- File tools, `apply_patch` (including patches routed through the shell tool), `fetch_url`, `market_prices` and `remember` keep their own policies. Host writes still need write approval or `--approve-writes`, and protected config paths always show their diff.
- The container keeps every hardening flag: no network, a read-only root, all capabilities dropped, no new privileges, resource limits, your UID, and `.eira` hidden.
- Commands containing recognized credentials are still rejected, and output is still redacted.

### Audit trail

Every decision is journaled and emitted as `approval_decided` with `tool`, `decision` (`auto`, `approved` or `denied`), `reason` and `command_sha256`. In `always` mode the reason is `approval mode always`. `eira trace SESSION` shows these events, and `--json` prints them.

### Residual risks

- A command that runs automatically can still change or delete any unprotected workspace file, and can use CPU, memory and disk up to the container limits. Workspace disk use is not quota-controlled.
- The destructive heuristic can be evaded, as described above.
- Docker remains a trusted dependency, and a container is not a VM.

Use sandboxed mode on a project under version control, ideally with checkpoints enabled, so automatic changes can be reviewed and reverted.
