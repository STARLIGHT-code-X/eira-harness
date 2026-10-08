# Code navigation

`list_files`, `search_files` and `read_file` are the model's read-only view of a workspace. This page describes what they show, what they skip, and their limits.

## Visibility

Both `list_files` and `search_files` walk the directory with `os.scandir`, never follow symlinks, sort each level, and stop at 32 levels, 200,000 entries or 5 seconds. A stopped walk sets `truncated` and explains why in `note`.

Every entry passes `Workspace.path`, the same check `read_file` uses. VCS metadata (`.git`, `.hg`, `.svn`, `.bzr`), Eira state (`.eira`), `.env` and `.env.*`, keys (`*.pem`, `*.key`, `*.p12`, `*.pfx`, `id_rsa`, `id_ed25519`), credential files and directories (`.ssh`, `.aws`, `.config`, `.docker`, `credentials.json` and others), symlinks, hard-linked files and protected home configuration are never listed or searched, whatever the arguments. Other dot entries such as `.github/workflows/ci.yml`, `.gitignore`, `.vscode/settings.json` and `.pre-commit-config.yaml` are shown, because file tools could already read them by exact path. Nothing new becomes readable.

Dependency and cache directories (`node_modules`, `__pycache__`, `venv`, `.venv`, `.tox`, `.nox`, `.mypy_cache`, `.pytest_cache`, `.ruff_cache`) and paths matched by `.gitignore` are skipped unless the call passes `ignored: true`.

Results report skips in `skipped` instead of dropping files silently:

| Key | Meaning |
|---|---|
| `ignored` | Files or pruned directories matched by `.gitignore` |
| `heavy_dirs` | Pruned dependency or cache directories |
| `blocked` | Entries refused by the path checks (including symlinks) |
| `gitignore_unread` | `.gitignore` files that could not be read or exceeded the 200-file budget (only when nonzero) |
| `binary` | Search only: a NUL byte in the first 8 KiB |
| `too_large` | Search only: larger than 5 MB |
| `non_utf8` | Search only: not valid UTF-8 |
| `unreadable` | Search only: changed or vanished between the check and the read |

## .gitignore

Eira reads `<dir>/.gitignore` files through the workspace checks (at most 100,000 bytes each and 200 files per walk) and follows this subset of gitignore(5):

- Blank lines and `#` comments are ignored; `\#` and `\!` escape a leading `#` or `!`. Unescaped trailing spaces are stripped.
- A leading `!` re-includes. A trailing `/` matches directories only.
- A `/` at the start or in the middle anchors the pattern to the `.gitignore`'s directory; otherwise the pattern matches a name at any depth.
- `*` and `?` never match `/`. `[...]` classes accept `!` or `^` for negation. A leading `**/`, a trailing `/**` and an inner `/**/` span directories. A backslash escapes the next character, and a pattern ending in a backslash never matches.
- Within one file the last matching rule wins; deeper files override shallower ones. An ignored directory is pruned, so its children cannot be re-included, as in git.
- When `path` names a subdirectory, `.gitignore` files in its ancestors still apply to its contents, but the named directory itself is always walked.

Not supported: `.git/info/exclude`, `core.excludesFile` and the global excludes file are not read, and matching is always case-sensitive.

## list_files

Arguments: `path`, `glob`, `ignored`, `offset` (default 0) and `limit` (default 500, at most 2,000). The result is `{files, truncated, next_offset, skipped}`. Paths are in sorted order, so paging with `offset = next_offset` until it is `null` returns every visible file exactly once.

## search_files

Arguments: `query` (at most 500 characters), `path`, `glob`, `ignore_case`, `regex`, `context` (0-10 lines before and after each match), `max_results` (1-500, default 100), `output` and `ignored`.

- `output: "matches"` (default): `{matches: [{path, line, column, text, before?, after?}], files_searched, truncated, skipped}`. `text` is at most 500 characters around the match; each context line is at most 300 characters.
- `output: "files"`: `{files: [{path, count}], total_matches, files_searched, truncated, skipped}`, sorted by path; `max_results` limits the number of files.
- `output: "count"`: `{total_matches, files_with_matches, files_searched, truncated, skipped}`.

`skipped_files` and `note` summarize unread files when there are any. Files up to 5 MB are searched.

Literal search runs inside Eira with a 5-second deadline. A regular expression (`regex: true`) never does, because Python's `re` module cannot be interrupted:

1. The pattern is compiled in Eira first; an invalid pattern fails immediately with `Invalid regular expression: ...` and starts no process.
2. Eira validates every file with `Workspace.path`, then starts `search_worker.py` from the installed package as `python -I` with cwd `/` and an empty environment. `-I` keeps the workspace and user site-packages off `sys.path`; the worker imports only standard-library modules.
3. The request (pattern, flags, validated absolute paths, limits) goes on stdin; the JSON reply is capped at 8 MiB.
4. After 10 seconds the process group is killed and the call fails with `Regex search timed out after 10 s; simplify the pattern or narrow path/glob.`

The worker matches each line's first 10,000 characters. It opens files without following a final symlink or blocking on a FIFO and rechecks that each is a regular, singly linked file; it shares the check-then-read race of in-process reads.

## Line numbers

`read_file`, `search_files`, `edit_file` and `apply_patch` share one rule: only `\n`, `\r\n` and `\r` end a line. Form feed, vertical tab, `\x85`, U+2028 and U+2029 stay inside a line, as in editors, grep and patch tools, so `a\x0cb\nc\n` has two lines and `c` is on line 2 everywhere.
