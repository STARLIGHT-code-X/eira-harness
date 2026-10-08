"""Run an eval task through another harness, so the same checks compare both.

Only Codex CLI is supported. It runs the user's own installation, with the
user's credentials and Codex's own workspace-write sandbox, on the task's
throwaway workspace. Prompts and fixtures go to OpenAI. Codex error text is
never copied into reports: only event types and exit codes are kept.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import time

from .security import HarnessError, bounded_json_loads
from .tools import _run_capture

CODEX_OUTPUT_BYTES = 8 * 1024 * 1024
TOOL_ITEMS = {"command_execution", "file_change", "mcp_tool_call", "web_search"}


def codex_binary() -> str:
    found = shutil.which("codex")
    if not found:
        raise HarnessError("codex is not on PATH; install Codex CLI or use --harness eira")
    return found


def codex_version() -> str:
    try:
        done = subprocess.run([codex_binary(), "--version"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"
    return (done.stdout.strip().splitlines() or ["unknown"])[0][:100]


def parse_codex_events(data: bytes) -> dict:
    """Summarize `codex exec --json` output without copying error messages."""
    answer, tokens, tools, completed, failed, events = "", 0, 0, False, [], 0
    for line in data.decode("utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            event = bounded_json_loads(line)
        except HarnessError:
            continue
        if not isinstance(event, dict):
            continue
        events += 1
        kind = event.get("type")
        item = event.get("item") if isinstance(event.get("item"), dict) else {}
        if kind == "item.completed":
            if item.get("type") == "agent_message" and isinstance(item.get("text"), str):
                answer = item["text"]
            elif item.get("type") in TOOL_ITEMS:
                tools += 1
        elif kind == "turn.completed":
            completed = True
            usage = event.get("usage") if isinstance(event.get("usage"), dict) else {}
            tokens += sum(value for key, value in usage.items()
                          if key in {"input_tokens", "output_tokens"} and type(value) is int and value > 0)
        elif kind in {"turn.failed", "error"}:
            failed.append(kind)
    status = "completed" if completed and not failed else "error"
    return {"answer": answer, "tokens": tokens, "tool_calls": tools, "status": status,
            "error": f"codex reported {', '.join(sorted(set(failed)))}" if failed else ("" if completed else "codex did not complete a turn"),
            "events": events}


def run_codex(prompt: str, root, model: str | None = None, timeout: int = 900) -> dict:
    argv = [codex_binary(), "exec", "--json", "--sandbox", "workspace-write", "--skip-git-repo-check",
            "--ephemeral", "-C", str(root)] + (["-m", model] if model else []) + [prompt]
    started = time.monotonic()
    env = {key: value for key, value in os.environ.items()}
    raw = _run_capture(argv, root, env, timeout, capture_limit=CODEX_OUTPUT_BYTES, merge_stderr=False)
    result = parse_codex_events(raw["data"])
    result["seconds"] = round(time.monotonic() - started, 3)
    if raw["stopped"]:
        result.update(status="error", error=f"codex stopped: {raw['stopped']}")
    elif raw["exit_code"] != 0 and result["status"] == "completed":
        result.update(status="error", error=f"codex exited {raw['exit_code']}")
    return result
