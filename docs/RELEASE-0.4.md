# Eira 0.4

This release targets the gaps that most affect real agent work: precise edits, large files, long sessions, current Claude models, and measuring results instead of asserting them.

## Coding tools

- **`edit_file`** replaces exact text in an existing file. The match must be unique unless `replace_all` is set. The diff is reviewed before writing, and the edit is cancelled if the file changes during approval. It also refuses protected content and keeps CRLF files CRLF. Models no longer have to resend a whole file to change one line.
- **Paged `read_file`.** Files up to 5 MB are read in pages of at most 2,000 lines or 24,000 characters, with `start_line`, `end_line`, `total_lines`, and `next_start_line`. The SHA-256 always covers the whole file. Previously, files over about 31 KB reached the model as a cut-off JSON fragment.
- **`search_files`** and **`list_files`** accept a `glob` filter, and search accepts `ignore_case`.
- Oversized tool results are now shortened field by field and stay valid JSON, so paths, hashes, and flags survive truncation.
- A note is added when the model repeats an identical call three or more times in one task.

## Long sessions

- **Frozen session prefix.** The system prompt, including `EIRA.md` and workspace memory, is captured once per session. Later guidance or memory changes are appended as a clearly labelled update message instead of rewriting the prompt. Every request in a session is therefore the previous request plus new turns. This is what provider prompt caches and newer models' reasoning checks require.
- **Context compaction.** When a session nears `--max-context-chars`, Eira asks the model for a summary and continues from it, quoting the latest user request verbatim. Original messages are never deleted. They stay in `.eira/state.db` and in `eira trace`. Older turns and their reasoning are not replayed after the summary. `--no-compact` restores the old stop-at-limit behavior.

## Anthropic and current Claude models

- **Thinking blocks.** Current Claude models think by default and return `thinking` blocks. The 0.3 adapter rejected any such block as a malformed response, so turns that included thinking failed. Eira 0.4 accepts `thinking` and `redacted_thinking` blocks, stores them with the turn, and replays them verbatim and in order on the next request.
- **Prompt caching.** Eira marks the frozen system prompt, which also covers the tool definitions, and the newest turn with `cache_control`. Cached tokens count toward `--max-tokens`, so the budget means the same thing with or without caching. Use `--no-prompt-cache` to disable it.
- **Output budget.** The per-response limit rises from a fixed 4,096 to `--max-output-tokens` (default 16,000), because thinking counts toward it. Use a lower value for older models with smaller limits.
- `--model-timeout` (default 300 s, max 900 s) replaces the fixed 90-second cap, which long replies could exceed.
- HTTP errors report the status code and, when the provider supplies a lowercase error-type token such as `not_found_error`, that token. Error bodies and messages are still never echoed.
- OpenAI-compatible requests carry only standard Chat Completions message fields. Eira's local metadata is never sent to them.

## Measuring

- **`eira eval`** runs a task suite against your configured model, each task in a throwaway workspace, and scores it with declarative checks on files and the final answer. It reports pass rate, steps, tool calls, tool errors, tokens, and time. A seven-task starter suite is built in, and a test proves every starter task is solvable with Eira's tools. See [EVALS.md](EVALS.md).

## Verified Docker execution

- `tests/test_docker_integration.py` runs Eira's real `shell` tool against a Docker daemon. It confirms that commands run in `/workspace` and can write project files, that networking is blocked, that the effective capability set is empty, that the container root is read-only, and that `.eira` is hidden. It also confirms that timeouts and output limits stop the command, that no container is left behind, and that denied commands never start a container. CI runs it on every push. Locally it runs only when `EIRA_DOCKER_IMAGE` names a pre-pulled image.

## Limits

Live provider accounts were not used. Anthropic behavior is verified with fixtures built from the documented wire format and with an end-to-end loopback server test. OpenAI-compatible behavior is verified as before. Eval scores depend on the model, provider, and suite. Eira makes no comparative benchmark claim; run `eira eval` with your own model to get one.

The installer is still pinned to the 0.3 release commit. The release process updates `RELEASE_COMMIT` after this source commit is merged.
