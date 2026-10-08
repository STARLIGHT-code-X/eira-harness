# Checkpoints and rewind

Before each tool batch that can change the workspace, Eira snapshots the workspace into a private store under `.eira/history`. `eira rewind` (or `/rewind` in chat) can then restore the files, the conversation, or both, and `eira diff` shows everything that changed since any checkpoint. A batch counts as file-changing when any of its tools declares the `write` or `exec` effect (`edit_file`, `write_file`, `shell`, and plugin tools that declare those effects), so a snapshot taken before a shell batch also covers files that the command creates, changes, or deletes.

```bash
eira checkpoints --workspace .                  # list this workspace's latest session's checkpoints
eira checkpoints --session SESSION_ID --json
eira diff --workspace .                         # everything changed since the session's first checkpoint
eira diff turn:3 --stat
eira rewind turn:3 --code                       # files only
eira rewind turn:3 --conversation               # what the model sees only
eira rewind turn:3 --both
eira rewind ck-0123456789 --code --yes          # scripts: skip the prompt
```

In chat, `/checkpoints` lists the current session's checkpoints, `/diff [N|ck-ID]` shows changes, and `/rewind N` (a turn number) or `/rewind ck-ID` asks whether to restore code (`c`), conversation (`v`), both (`b`), or nothing (`n`), shows the summary, and asks for confirmation. After a conversation rewind, the original prompt is printed and added to input history, so Up recalls it for editing and resending.

`--no-checkpoints` turns snapshots off for a `run`, `chat`, or `eval`.

## What is captured

A snapshot holds every regular file that Eira's file tools could open: it is reached without following symlinks and passes the same path checks. That excludes:

- `.git`, `.eira` and other VCS or state directories, `.env` files, keys and other credential files, protected home configuration, symlinks, and hard-linked files;
- heavy directories: `node_modules`, `.venv`, `venv`, `__pycache__`, `.tox`, `.nox`, `.mypy_cache`, `.pytest_cache`, `.ruff_cache`, `.gradle`, `.next`, `.turbo`;
- files over 10 MiB, which the manifest lists as skipped.

Checkpoints do not read `.gitignore` yet. Ignored files that are otherwise in scope, such as small build outputs, are captured, restored and deleted like any other file. Honoring `.gitignore` through the code-search walker (`eira_harness/navigate.py`) is a planned follow-up once that module is available.

A snapshot covering more than 100,000 files, or taking more than 10 seconds, is abandoned. Eira reports `checkpoint_skipped` and stores no partial checkpoint. Any other failure reports `checkpoint_failed`. In both cases the tool batch still runs: a checkpoint problem never blocks work.

The store is content-addressed. File bytes are saved once under `objects/<2 hex>/<62 hex>` and named by their SHA-256, and a checkpoint's manifest (paths, hashes, sizes, modes, and timestamps) is stored the same way. A file whose size, modification time, inode and mode match the previous snapshot is not read again. Files changed within two seconds of a snapshot are always re-read, because some file systems record coarse timestamps.

## When snapshots happen

- When a task starts, Eira records a **turn** checkpoint. It has no files yet.
- Before the first file-changing batch of the task, that turn checkpoint gets its snapshot: the files as they were just before Eira's first change in that task.
- Before each later file-changing batch, Eira adds a **step** checkpoint, unless nothing changed since the session's previous snapshot.
- Before every code rewind, Eira adds a **rewind-backup** checkpoint, so the rewind itself can be undone with `eira rewind <backup id> --code`.

Read-only batches and `--read-only` runs take no snapshot.

Turn numbers count the session's prompts in order and never change, even when retention removes old checkpoints. `eira checkpoints` shows them.

## Rewind semantics

**Code.** Eira scans the workspace and compares it with the checkpoint:

- files that differ, or whose permission bits differ, are **restored**;
- files that are missing are **recreated**;
- files that did not exist at the checkpoint are **deleted**.

Files outside the snapshot scope are never touched. That covers `.git`, `.env`, symlinks, heavy directories, and files over 10 MiB, whether they were skipped at the checkpoint or are now too large. Directories are never removed. The summary lists counts and file names, and shows unified diffs for text files (up to 200 lines per file and 200 KB in total); binary files are listed by name.

After you confirm, Eira holds the session lock and takes the rewind-backup checkpoint. If the workspace changed while it waited for confirmation, it stops and restores nothing. Otherwise it checks every path and every stored object before the first change, then deletes files and writes each restored file atomically (temporary file, fsync, rename, original mode).

**Turn without edits.** A turn whose task never changed files has no snapshot of its own. Rewinding its code uses the next checkpoint in the session, which records the state just before Eira's next change; the summary says so. If there is no later checkpoint, Eira has not changed files since, and files are left as they are. If the turn's snapshot was skipped or failed, its code cannot be restored, and Eira says so instead of using a later state.

**Conversation.** A conversation rewind appends a marker to the journal. It deletes nothing. From then on, the model's view is the conversation as it stood just before that turn's prompt. The next request is therefore a prefix of an earlier request: the same system prompt, tools, and earlier messages. The provider's prompt cache and the session's frozen prefix stay valid. Conversation rewinds need a turn checkpoint; step and backup checkpoints accept only `--code`. If the hidden messages had delivered a workspace guidance or memory update, the next task sends the current values again.

**Both.** The code rewind runs first, then the conversation rewind.

Every rewind is recorded as a `rewind_completed` event. `eira trace` shows the markers and every original message.

## Retention

Each session keeps at most 100 checkpoints, and checkpoints older than 30 days are removed. If the whole store exceeds 2 GiB, the oldest checkpoints in the workspace are removed until it fits; the newest checkpoint is always kept. Objects that no remaining checkpoint references are then deleted. Retention runs after each snapshot, within a 2-second budget.

## Compared with git

Checkpoints are not commits, and Eira never runs git. The store is separate from your repository. Eira never reads or writes `.git`, its index, refs, or stash, so a checkpoint or rewind cannot disturb your staging area or branches. Commit as you normally would; checkpoints are an undo history for agent work, not version control. Unlike git, snapshots capture untracked files (and do not yet honor `.gitignore`), and they skip credential files that git might track.

## Privacy

Snapshots are exact local copies of workspace files, including any secrets those files contain. They live under `.eira/history` with `0700` directories and `0600` files; the store refuses symlinks. File tools cannot read `.eira`, and the Docker shell hides it behind a tmpfs. Do not share `.eira/`. Delete `.eira/history` to discard all snapshots. The `checkpoints` table then still lists rows whose files can no longer be restored.
