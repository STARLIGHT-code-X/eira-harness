# Events

Eira reports progress as structured events. `Agent.event(kind, **fields)` redacts the fields, journals them in the session's `events` table (`eira trace SESSION` shows them as `{seq, time, kind, payload}`), and passes `{"event": kind, "session": SESSION_ID, **fields}` to the emit callback. `--json` prints each emitted event as one JSON line on stdout. Tools raise their own events through `Toolbox.notify(kind, **fields)`, which takes exactly the same path once an `Agent` owns the toolbox; without an agent it does nothing.

**Stability rule:** event names are stable and fields are only ever added. Consumers must ignore unknown events and unknown fields. A field is never renamed, removed or given a different type; a new meaning gets a new field or a new event.

## Core

| Event | Fields | When |
|---|---|---|
| `run_started` | `model` (str), `shell` (`"disabled"` or `"docker"`), `read_only` (bool) | A task starts, after recovery and any workspace update |
| `model_started` | `step` (int, from 1) | Before each model request |
| `model_completed` | `step` (int), `usage` (object, provider-reported) | After each model response, including an empty one that then fails the run |
| `assistant` | `text` (str) | The model returned visible text |
| `tool_started` | `call_id` (str), `name` (str), `detail` (str: one line, at most 100 characters, redacted; `""` when unknown) | Before a tool call runs |
| `tool_completed` | `call_id` (str), `name` (str), `ok` (bool), `error` (str or null) | After a tool call, successful or not, once its result is journaled |
| `run_completed` | `tools` (int), `tokens` (int), `seconds` (float) | The model finished without requesting tools |
| `run_stopped` | `reason` (`"budget"`, `"token_budget"` or `"step_budget"`), `tools` (int), `tokens` (int), `seconds` (float or null) | A run budget ended the task; the session is saved |
| `run_failed` | `error` (str) | An exception ended the task; it is then raised to the caller |
| `run_interrupted` | `reason` (`"user_interrupt"`) | Ctrl-C ended the task |
| `recovered_tool` | `call_id` (str), `status` (`"outcome_unknown"`) | A tool call left open by a crashed process was closed without replay |
| `workspace_context_updated` | none | EIRA.md or memory changed since the session's frozen prefix; the update was appended |
| `compaction_started` | `messages` (int), `chars` (int) | Before the summarization request |
| `context_compacted` | `replaced_messages` (int), `chars_before` (int), `chars_after` (int), `usage` (object) | The summary replaced the model's view; the originals stay in the journal |
| `reasoning_withheld` | `reason` (`"redaction"`) | Redaction would have changed signed provider blocks, so the turn was journaled without them |
| `plan` | `plan` (str) | Journal only, never emitted: the `set_plan` tool recorded the plan |
| `error` | `error` (str, redacted) | CLI only, never journaled and without `session`: with `--json`, the command ended with an error and exits 2 (a failed run prints `run_failed` first) |

## Shell sandbox (shell-protected-paths)

| Event | Fields | When |
|---|---|---|
| `sandbox_prepared` | `container` (str), `masked` (int), `read_only` (int), `sanitized_git_config` (int), `entries_scanned` (int), `seconds` (float) | An approved shell command's mount plan was rechecked and its sanitized git config copies written, just before the container starts |
| `sandbox_protected_path_created` | `paths` (list of str, workspace-relative) | After a shell command, when it created or replaced protected config paths; the result also carries `protected_paths_created` (see [SANDBOX.md](SANDBOX.md)) |

## apply_patch

| Event | Fields | When |
|---|---|---|
| `patch_rolled_back` | `patch_id` (str, 12 hex), `path` (str, workspace-relative), `error` (str, errno name such as `"ENOSPC"`) | A write failed during an `apply_patch` commit and every completed step was undone; no file changed |
| `patch_rollback_failed` | `patch_id` (str), `path` (str), `error` (str, errno name), `directory` (str, absolute path of the kept `.eira/patches/<id>`) | A write failed and the rollback could not undo every step; the journal directory is kept with `state: "rollback_failed"` (see [PATCHES.md](PATCHES.md)) |

## Output truncation and spill

| Event | Fields | When |
|---|---|---|
| `output_saved` | `output_id` (str), `tool` (`"shell"` or `"fetch_url"`), `bytes` (int), `lines` (int) | A long tool output was shortened and its full redacted copy saved for `read_output` |

## Edit checks

See [CHECKS.md](CHECKS.md).

| Event | Fields | When |
|---|---|---|
| `syntax_check_failed` | `path` (str), `language` (`"python"`, `"json"` or `"toml"`), `line` (int, 1-based), `rejected` (bool, `true`) | The syntax guard refused an edit that would have made a parseable file unparseable; nothing was written or asked |

## Instruction files (agents-md)

| Event | Fields | When |
|---|---|---|
| `guidance_loaded` | `path` (str), `sha256` (str, of the file bytes), `bytes` (int, file size), `via` (`"prefix"` or `"jit"`) | `prefix`: once per included file when a session's prefix is first frozen; `path` is relative to the project root, or `~/.config/eira/...` for the global file. `jit`: a subdirectory instruction file was attached to a tool result; `path` is workspace-relative. See [INSTRUCTIONS.md](INSTRUCTIONS.md) |

## Checkpoints and rewind

See [CHECKPOINTS.md](CHECKPOINTS.md).

| Event | Fields | When |
|---|---|---|
| `checkpoint_created` | `checkpoint` (str, `ck-` and 10 hex digits), `type` (`"turn"` or `"step"`), `files` (int), `bytes` (int) | A workspace snapshot was stored before a file-changing tool batch |
| `checkpoint_skipped` | `reason` (`"file_limit"` or `"time_limit"`), `checkpoint` (str, only when the snapshot was for a turn checkpoint) | The snapshot exceeded 100,000 files or 10 seconds; no partial checkpoint was stored and the batch ran |
| `checkpoint_failed` | `reason` (str, redacted), `checkpoint` (str, only when the snapshot was for a turn checkpoint) | Recording a turn or taking a snapshot failed; the batch ran anyway |
| `rewind_completed` | `checkpoint` (str), `mode` (`"code"`, `"conversation"` or `"both"`), `restored` (int), `deleted` (int), `hidden_messages` (int) | A confirmed `eira rewind` or `/rewind` finished; journaled in the rewound session |

## Sandboxed autorun

| Event | Fields | When |
|---|---|---|
| `approval_decided` | `tool` (`"shell"`), `decision` (`"auto"`, `"approved"` or `"denied"`), `reason` (str: `"sandboxed"` for auto, otherwise why review was needed, such as `"approval mode always"` or `recursive rm of "."`), `command_sha256` (str, hex SHA-256 of the command) | Inside a shell call, after the approval decision and before the container starts; not emitted when the command is rejected earlier (shell disabled, credentials, read-only) |
