# Edit checks

Eira checks every `edit_file` and `write_file` call, and every file in an `apply_patch` call, before it asks for approval. The check refuses an edit that turns a file that parsed into one that does not, so a broken edit is caught before the model moves on. You can also have project linters run in the Docker sandbox after each write, with their output returned to the model.

SWE-agent measured 18.0% vs 15.0% resolved on SWE-bench Lite with edit linting ([Table 3](https://arxiv.org/html/2405.15793v3)). Aider lints after every edit and shows errors inside their enclosing functions ([Aider, 2024](https://aider.chat/2024/05/22/linting.html)). Eira's error block follows Aider's layout.

## Languages

| Suffix | Language | Parser |
|---|---|---|
| `.py`, `.pyi` | Python | `ast.parse` from the running interpreter |
| `.json` | JSON | `json.loads` |
| `.toml` | TOML | `tomllib.loads` |

Files with any other suffix are not checked. XML is left out on purpose: whether stdlib expat resists entity-expansion attacks depends on the expat version it is linked against.

The guard returns "not checked" instead of an answer in these cases:

- the content is larger than 5,000,000 bytes, the file-tool limit;
- the parser raises `RecursionError` or `MemoryError`, for example on an expression nested 100,000 levels deep.

These cases never cause a rejection.

## The regression-only rule

An edit is **rejected** only when the old file parsed and the new content does not. If the file is new, or already failed to parse before the edit, the edit goes ahead with a warning in the result. These warnings carry a `note` of `"new file"` or `"file already failed to parse before this edit"`.

This rule exists because the host parser is not always the project's parser. A file can fail to parse on the host and still be valid for the project:

- **Newer syntax.** A project on Python 3.13 can use syntax that the host's 3.11 parser rejects.
- **Dialects.** Some JSON files, such as `tsconfig.json`, allow comments (JSONC).

If such a file already fails to parse, editing it only produces a warning. The rule cannot help when a valid file gains newer syntax: on a 3.11 host, adding a Python 3.12 `type X = int` statement to a file that parsed is rejected as a regression. The error names the parser version, for example `(Python 3.11 parser)`. Use `--syntax-guard warn` for such projects, or run Eira on the project's Python version.

## Modes

`--syntax-guard` on `eira run` and `eira chat`:

| Mode | Regression | New or already-broken file | Valid result |
|---|---|---|---|
| `reject` (default) | Refused before approval; the file is unchanged | Written, with a warning | Written, `result: "ok"` |
| `warn` | Written, with a warning (`rejected: false`) | Written, with a warning | Written, `result: "ok"` |
| `off` | Not checked | Not checked | Not checked |

The guard runs inside `Toolbox.check_write` before `Policy.require`. A rejected edit therefore never prompts and never writes, even under `--approve-writes`. A rejection journals and emits a `syntax_check_failed` event (see [EVENTS.md](EVENTS.md)). The guard can only refuse writes. It never allows a write that the policy denies.

## Results

A successful write reports checks in its result:

```json
"checks": [{"type": "syntax", "path": "a.py", "language": "python", "result": "ok"}]
```

A warning looks like this:

```json
{"type": "syntax", "path": "tsconfig.json", "language": "json", "result": "error",
 "message": "Expecting property name enclosed in double quotes (line 2, column 3)", "line": 2,
 "rejected": false, "note": "file already failed to parse before this edit"}
```

A file that could not be checked gives `"result": "not checked"` with a `reason`.

## Rejection format

A rejected edit fails with an error like this:

```text
Syntax check failed for inv.py (Python 3.11 parser): '[' was never closed (line 9, column 18). The edit was not applied; the file is unchanged.
    ⋮
    4│class Invoice:
    ⋮
    6│        self.items = items
    7│
    8│    def total(self):
    9█        prices = [
   10│            item.price
   11│            for item in self.items
    ⋮
```

For JSON and TOML the header names the format, for example `(JSON, Python 3.11 parser)`.

The block shows lines L-3 to L+2 around the error line L, which is marked with `█`. For Python it also shows the headers of the enclosing `class`, `def` and `async def` scopes. To find them, Eira scans upward from line L and takes each header that is indented less than the last one found, stopping at column 0. Gaps between the lines shown are marked with `⋮`. Each line is cut to 200 characters, and the block is capped at 40 lines and 3,000 characters.

Python reports an unclosed bracket at the line of the opening bracket, not at the end of the file. TOML positions come from the `(at line L, column C)` part of the `TOMLDecodeError` message. An error at the end of the document points at the last line.

## Lint commands

`--lint-cmd GLOB=COMMAND` (repeatable) runs a project linter after each successful `edit_file`, `write_file` or `apply_patch`:

```bash
eira run 'Fix the failing test.' --shell docker --docker-image my-python-tools:1 \
  --lint-cmd '*.py=ruff check --quiet {path}' --lint-cmd '*.toml=taplo check {path}'
```

- **Matching.** `GLOB` is matched against each changed workspace-relative path with `fnmatch.fnmatchcase`, where `*` also matches `/`. Deleted files are skipped, and a moved file is linted at its new path.
- **The command.** `{path}` is replaced with the shell-quoted path. Without `{path}`, the quoted path is appended. A path starting with `-` is passed as `./-name`.
- **Limit.** At most 5 lint runs happen per tool call. Each has a 60-second timeout.
- **Results.** Each run adds `{"type": "lint", "path", "command", "exit_code", "output"}` to `checks`. `output` keeps the head and tail of the command output, at most 4,000 characters.
- **Skipped runs.** If shell mode is not `docker`, the entry is `{"type": "lint", "path", "status": "skipped", "reason": "shell is disabled"}`. A denied approval, a credential in the command, Docker errors or cleanup failures give `status: "skipped"` with the reason.
- **Lint never reverts a write.** The edit was already applied when the linter runs. Lint output is untrusted command output, like any shell result.

### Sandbox requirement

Lint commands run only through the shell tool. They get the same Docker sandbox as any shell command: no network, dropped capabilities, a read-only root, the hidden `.eira` directory, credential rejection and output limits. They also need the same approval. Each lint run asks like any shell command, unless the shell approval policy in force allows it to run without a prompt. Lint commands never run on the host. Choose an image that contains the linters with `--docker-image`.

## Security

The built-in checks only parse text on the host with stdlib parsers. They never execute file content: `ast.parse` does not import or evaluate code, and warnings such as invalid escape sequences are suppressed. Parsing is size-bounded. Recursion and memory errors count as "not checked".
