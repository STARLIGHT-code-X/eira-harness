# Patches (`apply_patch`)

`apply_patch` edits files with the patch format that OpenAI's coding models are trained on (the format Codex uses). One call can add, update, rename and delete several files. Eira validates every file first, asks for one approval that shows the combined diff, and then writes all files or none.

Use `edit_file` for one exact replacement and `write_file` to create or fully replace a file. Both are unchanged.

## Format

The tool takes one string argument, `input`:

```
*** Begin Patch
*** Add File: docs/notes.md
+every line of a new file starts with +
*** Delete File: old/unused.py
*** Update File: src/app.py
*** Move to: src/main.py
@@ def handler(request):
     user = request.user
-    return render(user)
+    return render(user, theme="dark")
@@
-DEBUG = True
+DEBUG = False
*** End of File
*** End Patch
```

- `*** Add File: PATH` is followed by the file's lines, each starting with `+`. An Add with no lines creates an empty file.
- `*** Delete File: PATH` deletes one regular file.
- `*** Update File: PATH` is followed by chunks. `*** Move to: NEW` may come directly after it and renames the file.
- A chunk starts with `@@` or `@@ LINE`, where LINE is a line just above the change, such as a function header. Only the first chunk may omit `@@`. Chunk lines start with a space (context), `-` (removed) or `+` (added); a completely empty line counts as an empty context line.
- `*** End of File` after a chunk anchors it to the end of the file.
- Whitespace around the markers and headers is ignored. A patch wrapped in `<<'EOF'` ... `EOF` lines is accepted.
- Paths are workspace-relative. An absolute path under the workspace root, or under `/workspace/` (the Docker view of the workspace), is rewritten to a relative path; any other absolute path fails with `Path is outside the workspace`.
- A patch holds at most 100 file operations and 2,000 chunks.

The parser is a port of Codex's line state machine (`codex-rs/apply-patch`, commit 8f21b7f) and reports Codex's error strings, for example `invalid hunk at line 2, Update file hunk for path 'a.py' is empty`. Errors found before approval start with `apply_patch verification failed: `, as in Codex.

## Matching

An `@@ LINE` is searched for as a single line from the current position, and the search moves past it. The chunk's old lines (context plus `-` lines) are then searched at or after that point with four passes, in order:

1. exact;
2. ignoring trailing whitespace;
3. ignoring leading and trailing whitespace;
4. as 3, and also folding Unicode punctuation: dashes (U+2010 to U+2015, U+2212) to `-`, curly single quotes to `'`, curly double quotes to `"`, and non-breaking and other odd spaces to a space.

A chunk with `*** End of File` is searched at the end of the file. If nothing matches and the chunk's old lines end with an empty line, the search is retried without it. A chunk without old lines appends at the end of the file. Chunks must appear in file order.

Text is rebuilt line by line. Each line keeps its own terminator (`\n`, `\r\n` or a lone `\r`). Context lines are left byte for byte. Inserted lines use the file's first terminator, or `\n` in a file without one. Every resulting line ends with a terminator, as in Codex.

The result's `fuzz` list names every chunk that needed pass 2, 3 or 4, for example `{"path": "a.py", "hunk": 2, "pass": "rstrip"}`. `hunk` counts the chunks of that file's Update from 1.

When a chunk is not found, the error lists up to three closest windows of the file. Each is shown as `Closest match at lines A-B (similarity R):` and a zero-context diff of the file's lines (`-`) against the patch's lines (`+`). Tabs are shown as `→` and trailing spaces as `·`. The search is bounded at 50,000 index probes per chunk, so a large file cannot stall it.

## Deliberate differences from Codex

| Situation | Codex | Eira |
|---|---|---|
| `Add File` on an existing path | overwrites it | fails with `Add File target already exists`, unless the same patch deleted the path first |
| `Move to` an existing path | overwrites it | fails with `Move destination already exists` |
| A chunk without `@@` matches several places on a fuzzy pass (2 to 4) | uses the first | fails with `Ambiguous match: hunk N matches K places in PATH (lines ...)`; add an `@@` line or more context |
| A chunk without `@@` matches several places exactly | uses the first | uses the first and adds a warning |
| A later hunk fails after earlier files were written | earlier files stay changed | nothing is written: every hunk is checked before any write, and a failed write rolls back |

Hunks run in order over an in-memory copy of the touched files, so Add then Update of one file, or two Updates of one file, behave as if applied one after another. Nothing touches disk until the commit.

## Safety checks and approval

Before asking, Eira checks every touched path and every changed file:

- Every path goes through the workspace path checks: no traversal, symlinks, hard links, VCS or Eira state, credential files or protected home configuration.
- Targets must be regular UTF-8 text files without NUL bytes, at most 5,000,000 bytes.
- Files that contain recognized secrets cannot be edited or deleted, and added lines cannot contain `[REDACTED]` or a recognized credential.
- Pre-write guards (`Toolbox.write_guards`, such as a syntax guard) run on each changed file; one refusal rejects the whole patch. Their non-empty results are returned in `checks`.

Then one approval covers the whole patch. Its text starts with `apply_patch: N files (A added, M modified, D deleted, R moved)` and is followed by one unified diff per file in patch order. A move adds `rename from` and `rename to` lines; a delete is a diff against an empty file. `--approve-writes` preapproves it, except when a touched path is review-required (`Toolbox.review_paths`). `--read-only` denies it.

After approval, every touched path is read again. If a file's SHA-256 changed, or an Add target now exists, the patch is cancelled with `File changed during approval; patch cancelled. No files were modified.`

## Commit and recovery

The commit is all or nothing:

1. Eira creates `.eira/patches/<12-hex id>/` (mode 0700). It writes `manifest.json` with `state: "committing"` and a list of operations, and a pre-image copy of every existing touched file under `pre/` (mode 0600).
2. Each new content is written to a temporary file (`.eira-patch-*`) in the target's directory, synced, and given the original file mode. A new file gets the mode your umask allows; a moved file keeps the source's mode. Missing parent directories are created.
3. Files are changed in order: an update is one atomic rename over the target; an add or a move destination is a hard link that fails if the path has appeared meanwhile; a delete or a move source is renamed into the patch directory's `trash/` (or unlinked, once its pre-image is saved, when the rename would cross filesystems).
4. If any step fails, the completed steps are undone in reverse order: pre-images are restored, created files are removed if they still hold what Eira wrote, trashed files are moved back, created directories are removed if empty, and temporary files are deleted. The error reads `Patch failed while writing PATH (ENOSPC); all changes were rolled back.` and the `patch_rolled_back` event is journaled.
5. After success, or after a complete rollback, the patch directory is deleted.

If the rollback itself fails, the directory stays with `state: "rollback_failed"`, the `patch_rollback_failed` event is journaled, and the error names the directory. If the process is killed during a commit, the directory stays with `state: "committing"`. Eira never restores these automatically, in keeping with the rule that side effects are never replayed. `eira doctor` lists them under `incomplete_patches`.

To recover by hand, read `manifest.json` in the listed directory. Each operation has `op`, `path`, `to` for a move, `pre_image` (a file under the directory holding the original bytes), `mode`, `sha256_before` and `sha256_after`. `temporary_files` and `created_directories` list what step 2 created. For each operation, compare the file on disk with `sha256_after` and `sha256_before`, copy back the pre-image where you want the original, delete leftover `.eira-patch-*` files, and then delete the patch directory. The directory sits under `.eira`, which the file tools refuse and the Docker shell hides behind a tmpfs.

## Result

The result, inside the usual `{ok, result}` envelope:

```json
{"summary": "Success. Updated the following files:\nA new.txt\nM mod.txt\nD gone.txt",
 "files": [{"path": "new.txt", "op": "add", "sha256": "..."},
           {"path": "mod.txt", "op": "update", "first_changed_line": 12, "sha256": "..."},
           {"path": "old.py", "op": "move", "to": "new.py", "sha256": "..."},
           {"path": "gone.txt", "op": "delete"}],
 "fuzz": [{"path": "mod.txt", "hunk": 2, "pass": "rstrip"}],
 "warnings": [],
 "checks": []}
```

`summary` matches Codex's output: `A`, `M` and `D` lines in that order, with a move listed as `M <destination>`. `files` has one entry per hunk; `sha256` is the hash of the bytes now on disk at that path (or at `to` for a move).

## Shell routing

Models trained on Codex often send a patch through the shell. Eira routes a shell call that consists only of one of these forms to `apply_patch`:

- `apply_patch <<'EOF'` (or `applypatch`, `<<EOF`, `<<"EOF"`, `<<-EOF`, any tag), a newline, the patch, a newline and the tag, with nothing after it. It may start with `cd DIR &&`, where DIR has no whitespace, quotes, `$`, backticks or shell operators; DIR must be a workspace directory and is prefixed to every relative patch path.
- `apply_patch '<patch>'` or `apply_patch "<patch>"`, whose body starts with `*** Begin Patch` and contains no quote of the same kind (and, in double quotes, no `$`, backtick or backslash).

The patterns are anchored at both ends, so `apply_patch <<EOF ... EOF; rm -rf .`, a second command after the terminator, or `echo x && apply_patch ...` are not routed and go through the normal shell checks. A routed call is a host file write under the file-write policy: it runs even when the shell is disabled, it never runs a command, it asks for write approval like the tool, and its result carries `"routed_from": "shell"`.

## Upgrade note

The tool list sent to models grew by one tool, and the system prompt's editing sentence now names `apply_patch`. Existing sessions keep their frozen system prompt, but a resumed session sends the longer tool list, which costs one prompt-cache miss after the upgrade.
