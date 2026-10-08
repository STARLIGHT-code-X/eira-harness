# Shell sandbox mount plan

An approved Docker command used to see the whole workspace read-write, including files that the file tools refuse to touch. Before each command, Eira now scans the workspace and builds a mount plan:

- **Secret paths are masked.** Files that the file tools block (`.env*`, keys and credential stores) read as empty inside the container, and blocked directories appear empty.
- **Config paths are read-only.** This covers VCS metadata, agent instructions and config, IDE and devcontainer config, hook managers, CI definitions and package-manager hook files.
- **Credential-bearing git config is replaced by a sanitized copy.**

The protection is always on and has no flags. The shell approval shows a summary line, for example:

```text
Sandbox: no network, read-only system, 3 secret paths hidden, 7 config paths read-only, 1 git config sanitized
```

The code lives in `eira_harness/sandbox.py`.

## Classification

Names are matched per path component, case-insensitively, at any depth.

### Secret: masked in the container, blocked for file tools

These are the names that `security.protected_kind` classifies as `secret`, the same predicate `Workspace.path` uses for file tools:

- every name in `Workspace.BLOCKED` except the state and VCS names `.eira`, `.git`, `.hg`, `.svn`, `.bzr` and `.codex`. That leaves `.ssh`, `.aws`, `.gnupg`, `.kube`, `.netrc`, `_netrc`, `.npmrc`, `.pypirc`, `.docker`, `.git-credentials`, `.azure`, `.gcloud`, `.password-store`, `.config`, `.gitconfig`, `.bashrc`, `.bash_profile`, `.profile`, `.zshrc`, `application_default_credentials.json` and `service_account.json`
- `.env` and `.env.*`
- names ending in `.pem`, `.key`, `.p12` or `.pfx`
- `id_rsa`, `id_ed25519`, `credentials` and `credentials.json`

Two more cases are masked because the file tools block them too:

- a nested `.eira` directory, which holds another workspace's state (the workspace's own `.eira` already has a tmpfs)
- credential locations configured through `XDG_CONFIG_HOME`, `CLOUDSDK_CONFIG`, `GH_CONFIG_DIR`, `AWS_SHARED_CREDENTIALS_FILE`, `GOOGLE_APPLICATION_CREDENTIALS` or `KUBECONFIG` that fall inside the workspace

### Config: read-only in the container, always reviewed for file tools

| Group | Names |
|---|---|
| VCS | `.git` (directory or gitdir file), `.hg`, `.svn`, `.bzr`, `.gitmodules` |
| Agent config and instructions | `.codex`, `.agents`, `.claude`, `.gemini`, `.cursor`, `.mcp.json`, `.claude.json`, `EIRA.md`, `AGENTS.md`, `AGENTS.override.md`, `CLAUDE.md`, `GEMINI.md` |
| IDE and devcontainer | `.vscode`, `.idea`, `.zed`, `.devcontainer`, `.devcontainer.json` |
| Hooks and automation | `.husky`, `.githooks`, `.pre-commit-config.yaml`, `lefthook.yml`, `lefthook.yaml`, `.lefthook.yml`, `.lefthook.yaml`, `.envrc` |
| CI | `.github/workflows`, `.github/actions`, `.gitlab-ci.yml`, `.circleci`, `.buildkite`, `azure-pipelines.yml`, `bitbucket-pipelines.yml`, `.travis.yml`, `Jenkinsfile` |
| Package-manager and tool hooks | `.yarnrc`, `.yarnrc.yml`, `.yarn/plugins`, `.yarn/releases`, `.pnpmfile.cjs`, `.pnp.cjs`, `.pnp.loader.mjs`, `bunfig.toml`, `.bunfig.toml`, `.cargo`, `.mvn`, `gradle-wrapper.properties`, `maven-wrapper.properties`, `.bazelrc`, `.bazelversion`, `.bazeliskrc`, `.ripgreprc`, `pyrightconfig.json` |
| Shell rc files not already secret | `.bash_login`, `.bash_aliases`, `.bash_logout`, `.zprofile`, `.zshenv`, `.zlogin`, `.zlogout` |

Entries written with a slash match a path whose last two components equal them. All other entries match one component.

Sources:

- Claude Code's protected paths (<https://code.claude.com/docs/en/permission-modes>).
- The trust-handoff research: Pillar Security, "GitPwned", 2026-07-20, and the CSA research note of 2026-07-22.
- Codex. It protects four names inside writable roots (`.git`, `.agents`, `.codex` and `.aws`; `protocol/src/permissions.rs` at 8f21b7f), and it leaves secrets elsewhere readable to its sandbox.

Build files that you deliberately run stay writable, because normal work edits them. These include `Makefile`, `package.json`, `pyproject.toml` and `setup.py`. Review build-file changes yourself before running them on the host.

### File tools

`edit_file` and `write_file` always show the diff for approval when a path component is a config name. This applies even with `--approve-writes`, so headless runs deny those writes. This matches Claude Code's rule that writes to protected paths are never auto-approved. `--approve-writes` still covers ordinary files. The rule is `sandbox.requires_review`, installed as `Toolbox.review_paths`.

## Scan

The scan uses `os.scandir` without following symlinks and visits entries in sorted order. It does not descend into:

- masked directories, which are handled whole
- VCS directories
- the workspace's own `.eira`
- `node_modules`, `.venv`, `venv`, `__pycache__`, `.tox`, `.nox`, `.mypy_cache`, `.pytest_cache` and `.ruff_cache`

Other read-only config directories are descended into only to mask secrets inside them, such as `.devcontainer/.env`.

Limits: 200,000 entries, 5 seconds, depth 64. Every failure below refuses the command before approval, so nothing runs:

| Condition | Error |
|---|---|
| Too many entries, too slow, or too deep | `Eira could not verify protected paths in this workspace (N entries scanned). Use a smaller workspace for shell commands.` |
| A config path is a symlink (or `.github` / `.yarn` is) | `Protected path "P" is a symlink; the sandbox cannot protect it.` Replacing such a link could redirect host git or CI. |
| A protected path contains `,`, `:`, `"`, a newline or a control character | `Protected path "P" cannot be expressed safely in a Docker mount; rename it before running shell commands.` |
| More than 512 protective mounts | `This workspace has N protected paths; the sandbox supports at most 512. ...` |
| An unreadable directory that the container user could still traverse | `Eira could not read "P" while checking protected paths (...)` |
| More than 64 git config files, or one over 1 MB | the git config check refuses the command |

A secret name that is a symlink is skipped without an error. Its target is classified on its own, and inside the container an absolute link resolves to container paths, not host files.

The workspace is scanned again after approval. If the protective mounts differ from the approved plan, the command is cancelled with `Protected paths in the workspace changed during approval`.

## Mounts

Protective arguments follow the workspace bind and the `.eira` tmpfs, sorted by destination:

```text
secret file:  --mount type=bind,src=/dev/null,dst=/workspace/<rel>,readonly
secret dir:   --tmpfs /workspace/<rel>:ro,size=4k,mode=0500
config path:  --mount type=bind,src=<root>/<rel>,dst=/workspace/<rel>,readonly
git config:   --mount type=bind,src=<root>/.eira/sandbox/<container>/git-config-<n>,dst=/workspace/<rel>,readonly
```

Docker documents `ro`, `size` and `mode` for `--tmpfs` (<https://docs.docker.com/engine/storage/tmpfs/>). It documents `readonly` for bind mounts and does not create a missing bind source (<https://docs.docker.com/engine/storage/bind-mounts/>).

Inside the container, `cat .env` prints nothing. Writing to `.git/hooks`, `.github/workflows`, `.vscode` or `AGENTS.md` fails with `Read-only file system`.

Every existing hardening flag stays: `--pull=never`, `--network=none`, `--read-only`, `--cap-drop=ALL`, `no-new-privileges`, the pids, memory and CPU limits, `--user uid:gid` and `--log-driver=none`. Containers carry `--label eira.managed=1` so that orphans can be found with `docker ps -a --filter label=eira.managed=1`.

### Sanitized git config

Eira checks the config files of each mounted `.git` directory: its top-level `config` and `config.worktree`, and `modules/**/config`, at most 64 files. A copy is written when a file contains any of:

- URL userinfo with a password
- any userinfo in an http(s) or ftp(s) URL
- an `extraheader`, `helper`, `password` or `token` key
- a `[credential]` section
- a value the redactor recognizes

In the copy:

- userinfo is stripped from those URLs
- those keys and their continuation lines are dropped
- every `[credential...]` section is dropped
- the redactor runs last

All other lines stay byte-identical. Each copy has mode 0600 under `.eira/sandbox/<container>/`, which the container cannot see. It is mounted read-only over its original and deleted after the run. This removes credentials that the redactor's patterns would miss, such as non-`sk-` tokens in remote URLs.

### Container environment

```text
HOME=/tmp LANG=C.UTF-8 TERM=dumb NO_COLOR=1 PAGER=cat GIT_PAGER=cat GIT_OPTIONAL_LOCKS=0 GIT_CONFIG_NOSYSTEM=1
```

With these, git and other tools work non-interactively against a read-only `.git`. Git documents `--no-optional-locks` as equivalent to `GIT_OPTIONAL_LOCKS=0` (<https://git-scm.com/docs/git>). Codex forces a similar environment (`codex-rs/core/src/unified_exec/process_manager.rs` at 8f21b7f).

## Post-run detection

After the container exits, Eira scans the config names again. A config path counts as created if it is present now but was absent before, or if it is now a different file or directory (a different device and inode). The second case catches `mv .github .old && mkdir -p .github/workflows`.

When anything was created:

- the shell result gains `protected_paths_created` and a `warning`
- the paths are added to `Toolbox.shell_alerts`
- a `sandbox_protected_path_created` event is journaled and emitted
- the terminal and the `eira run` renderer print a warning

If that second scan hits a limit, the result carries a warning instead.

Each command also emits `sandbox_prepared` with the mount counts and scan cost. See [EVENTS.md](EVENTS.md).

## Residual risks

- Protected names that a command creates are detected and reported, not prevented. A command can rename a directory that contains a read-only mount, then recreate the protected name beside it. Detection reports this, but the host still sees the new files.
- Heavy directories such as `node_modules` and `.venv` are not scanned, so protected names inside them are neither masked nor read-only.
- VCS directories are mounted read-only as a whole, and only their git config files are sanitized. Other files inside them, such as hook scripts that embed a token, stay readable.
- Hard-linked files are not masked, because pnpm stores rely on hard links. The container cannot create links to host files outside the workspace.
- The workspace is still writable, so build files and source code can be changed. Review them before you run them on the host.
- Paths can change between the post-approval scan and the container start. That window is short, and nothing that the agent controls runs in it.
- Docker remains a trusted dependency and is not a VM.
