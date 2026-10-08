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

## Output truncation and spill

| Event | Fields | When |
|---|---|---|
| `output_saved` | `output_id` (str), `tool` (`"shell"` or `"fetch_url"`), `bytes` (int), `lines` (int) | A long tool output was shortened and its full redacted copy saved for `read_output` |
