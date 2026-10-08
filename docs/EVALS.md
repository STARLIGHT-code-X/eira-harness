# Evaluating a model with Eira

`eira eval` answers the question "how well does this model do real work in this harness?" with numbers you can reproduce. Each task runs in a fresh, throwaway workspace. A task passes only if the run completes and every check passes.

```bash
eira eval                                   # built-in starter suite, configured model
eira eval --provider anthropic --model "$MODEL" --repeat 3 --output report.json
eira eval my-suite.json --work-dir eval-runs     # keep workspaces (relative to --workspace)
eira eval --dump-suite > my-suite.json      # start from the starter suite
```

The exit code is `0` when every run passes, `1` when any run fails, and `2` for a configuration error. Progress goes to stderr. `--json` prints only the report.

## What a task may do

Inside the throwaway workspace, file and memory writes are preapproved. Shell, URL fetching, and market data stay denied, because no one is present to approve them. Model calls, budgets, compaction, and prompt caching behave exactly as in `eira run`, and the same limit flags apply. Each run's transcript stays in that workspace's `.eira/state.db`. Use `--work-dir` to keep it.

## Suite format

```json
{
  "name": "my-suite",
  "description": "optional",
  "tasks": [
    {
      "id": "fix-mean",
      "prompt": "stats.mean([2, 4, 6]) should return 4.0. Fix the bug in stats.py.",
      "files": {"stats.py": "def mean(values):\n    return sum(values) / (len(values) - 1)\n"},
      "max_steps": 12,
      "checks": [
        {"type": "file_matches", "path": "stats.py", "pattern": "return\\s+sum\\(values\\)\\s*/\\s*len\\(values\\)\\s*$"},
        {"type": "answer_contains", "text": "fixed", "ignore_case": true}
      ]
    }
  ]
}
```

| Check | Passes when |
|---|---|
| `file_exists` / `file_absent` | the path is a file / does not exist |
| `file_contains` / `file_not_contains` | the file includes / excludes `text` |
| `file_equals` | the file is exactly `text` |
| `file_matches` / `file_not_matches` | `pattern` (Python regex, multiline) is / is not found in the file |
| `file_unchanged` | the file still equals its fixture content |
| `answer_contains` / `answer_not_contains` | the final answer includes / excludes `text` |
| `answer_matches` / `answer_not_matches` | `pattern` is / is not found in the final answer |

`contains`, `equals`, and `matches` checks accept `"ignore_case": true`. Fixture and check paths follow the same rules as file tools (no traversal, symlinks, VCS metadata, `.env`, or credential files), `file_unchanged` must name a fixture file, and `contains` text must be non-empty. All of this is validated before any model request. A task's `max_steps` replaces `--max-steps` for that task.

Suites are trusted local files, like test code: their regular expressions run in-process, so a pathological pattern can make a check slow.

Checks are declarative on purpose: Eira does not run model-written code on your host. For behavioral checks, run your project's tests yourself in the kept workspaces, or in Docker.

## The starter suite

| Task | What it measures |
|---|---|
| `fix-mean` | Locate and fix a one-line bug without collateral edits |
| `add-function` | Add code while preserving existing code |
| `rename-everywhere` | A multi-file rename including imports and tests |
| `answer-from-code` | Answer from the code without modifying anything |
| `create-changelog` | Create a new file with required structure |
| `find-in-large-file` | Find one line in a 3,000-line file using search or paging |
| `protected-file-honesty` | Report that a file with a credential cannot be edited, and never claim success |

A scripted oracle in `tests/test_evals.py` solves every starter task with Eira's real tools, and a do-nothing provider fails every task. Both run in CI, so an impossible check, or a task whose checks all pass without any work, is caught. A single check that always passes alongside others that fail is not. The starter suite is small and is a smoke test, not a benchmark. Write suites from your own repositories for decisions that matter, and use `--repeat` to see run-to-run variance.

## Report fields

`summary` has `runs`, `passed`, `pass_rate`, `steps`, `tool_calls`, `tool_errors`, `tokens`, and `seconds`. `tool_calls` and `tool_errors` count only calls that were executed; tokens include runs that ended in an error. Each item in `results` has the task id, run number, status (`completed`, `stopped`, or `error`), and per-check results with failure details. It also has the metrics above, the first 2,000 characters of the redacted final answer, and, with `--work-dir`, the workspace path. Tokens are provider-reported usage, including cached prompt tokens. They are not a bill.
