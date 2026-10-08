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

## Behavioral evals (suite v2)

Declarative checks show that a file changed; behavioral checks show that the change works. A v2 suite can run the task's own tests after the agent finishes:

```json
{"name": "mine", "sandbox": {"image": "python:3.11-slim"}, "tasks": [{
  "id": "fix-cart", "agent_shell": true, "prompt": "Fix the cart total so the tests pass.",
  "files": {"cart.py": "...", "tests/test_cart.py": "..."},
  "checks": [{"type": "command_succeeds", "command": "python -m unittest -q", "timeout": 120,
              "restore": ["tests/test_cart.py"]}]}]}
```

- `command_succeeds` runs `command` in the same hardened Docker sandbox as agent commands (no network, no capabilities, read-only root, secrets masked). It passes when the exit code equals `expect_exit` (default 0) and, if given, `output_matches` is found in the output. `restore` first rewrites listed fixture files, so an agent cannot pass by editing the tests. Check commands come from the suite author and never run on the host.
- `agent_shell: true` gives the agent the Docker shell under `--shell-approval sandboxed` for that task. Nothing in an eval is approved by a person, so destructive commands are denied.
- A suite with command checks or agent shells needs an image (`sandbox.image` or `--docker-image`) and a Docker daemon. This is checked before any model call.

The built-in `coding` suite has eight such tasks: a bug spanning two modules, implementing a function to a docstring, a package-wide rename, a fix in a CRLF file that must stay CRLF, a failure whose cause appears only at the end of more than 20,000 characters of output, three far-apart edits in one file, adding an argparse flag, and a project whose tests already pass (it must be left unchanged). A scripted oracle passes all eight and a do-nothing provider passes only the last, offline with a fake Docker and against a real daemon in CI.

```bash
eira eval coding --docker-image python:3.11-slim --repeat 5 --jobs 4 --output eira.json
```

### Statistics

With `--repeat n`, each task has n independent runs. Reports (`report_version: 2`) add:

- `pass_rate_ci95`: the Wilson score 95% interval for the suite pass rate ([reference](https://en.wikipedia.org/wiki/Binomial_proportion_confidence_interval)).
- `pass_at_k` for k = 1 and k = n: the unbiased estimator 1 − C(n−c, k) / C(n, k) of Chen et al. 2021, Eq. 1 ([arXiv:2107.03374](https://arxiv.org/abs/2107.03374)), averaged over tasks; per-task values are under `tasks`.
- `errors`: failed tool calls by category (`edit_miss`, `edit_ambiguous`, `syntax_rejected`, `approval_denied`, `schema_error`, `stale_edit`, `other`).
- `tokens_mean`, `tokens_stdev`, `seconds_mean`.

`--jobs N` (1–8) runs tasks concurrently, each with its own workspace, journal and provider instance; results are ordered by run and task, so reports do not depend on completion order.

### Comparing with Codex

`--harness codex` runs the same tasks through your own Codex CLI (`codex exec --json --sandbox workspace-write --skip-git-repo-check --ephemeral -C <workspace>`, plus `-m` from `--codex-model`) and scores them with the same checks. This uses your Codex installation and credentials and sends prompts and fixture files to OpenAI. Eira records the answer, token usage, tool-call count and Codex version, never Codex's error text.

```bash
eira eval coding --harness codex --codex-model "$MODEL" --repeat 5 --output codex.json
eira eval --compare eira.json codex.json
```

`--compare` prints per-task pass-rate deltas and both suite intervals. When the intervals overlap it says "difference not established at 95%". Results compare harness plus model plus suite: use the same model on both sides, enough repeats, and suites from your own work before concluding that one harness is better.
