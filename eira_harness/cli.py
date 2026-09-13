"""Eira terminal entrypoint."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import sys

from . import __version__
from .agent import Agent, Limits
from .demo import DemoProvider, sample_csv
from .finance import backtest, markdown_report
from .provider import Provider
from .security import HarnessError, Redactor, Workspace, atomic_write, clean_terminal
from .store import Store
from .tools import Policy, Toolbox


def print_safe(value: str, file=None):
    print(clean_terminal(Redactor()(value)), file=file or sys.stdout, flush=True)


def approve(name: str, detail: str) -> bool:
    # Piped input can never silently authorize a side effect.
    if not sys.stdin.isatty():
        return False
    print_safe(f"\nApproval required · {name}\n{detail}", file=sys.stderr)
    print("\nApprove this action? [y/N] ", end="", file=sys.stderr, flush=True)
    return sys.stdin.readline().strip().lower() == "y"


def renderer(as_json: bool):
    def emit(event):
        if as_json:
            print(json.dumps(event, ensure_ascii=True, allow_nan=False), flush=True)
            return
        kind = event["event"]
        if kind == "run_started":
            print_safe(f"\nEIRA / {event['model']}\nSession {event['session']}\n", file=sys.stderr)
        elif kind == "tool_started":
            print_safe(f"  → {event['name']}", file=sys.stderr)
        elif kind == "tool_completed" and not event["ok"]:
            print_safe(f"  ! {event['error']}", file=sys.stderr)
        elif kind == "assistant":
            print_safe(event["text"])
        elif kind == "run_stopped":
            print_safe(f"Stopped: {event['reason']}. Session saved.", file=sys.stderr)
        elif kind == "recovered_tool":
            print_safe("Recovered an interrupted tool call; its outcome is unknown. It was not replayed.", file=sys.stderr)
    return emit


def build_parser():
    parser = argparse.ArgumentParser(prog="eira", description="A local agent harness for coding and financial research.")
    parser.add_argument("--version", action="version", version=f"Eira {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    def workspace(command):
        command.add_argument("--workspace", type=Path, default=Path.cwd(), help="Workspace directory (default: current directory)")

    def model_options(command):
        workspace(command)
        command.add_argument("--model", default=os.getenv("EIRA_MODEL", ""))
        command.add_argument("--base-url", default=os.getenv("EIRA_BASE_URL", "https://api.openai.com/v1"))
        command.add_argument("--session", help="Resume a session from this workspace")
        command.add_argument("--read-only", action="store_true", help="Deny file writes, memory writes, and shell calls")
        command.add_argument("--approve-writes", action="store_true", help="Preapprove workspace file and memory writes; never shell/network")
        command.add_argument("--allow-host", action="append", default=[], help="Preapprove HTTPS GET requests to this exact hostname (repeatable)")
        command.add_argument("--shell", choices=["disabled", "docker", "host"], default="disabled")
        command.add_argument("--docker-image", default="python:3.11-slim", help="Pre-pulled Docker image for shell mode")
        command.add_argument("--max-steps", type=int, default=20)
        command.add_argument("--max-tool-calls", type=int, default=50)
        command.add_argument("--max-tokens", type=int, default=100_000, help="Cumulative reported usage; checked between requests, not a hard billing cap")
        command.add_argument("--max-context-chars", type=int, default=120_000)
        command.add_argument("--json", action="store_true", help="Emit JSONL events to stdout")

    run = sub.add_parser("run", help="Run one task")
    run.add_argument("prompt")
    model_options(run)
    chat = sub.add_parser("chat", help="Start an interactive conversation")
    model_options(chat)
    init = sub.add_parser("init", help="Initialize workspace state and EIRA.md")
    workspace(init)
    demo = sub.add_parser("demo", help="Run a scripted offline demo on synthetic prices")
    workspace(demo)
    demo.add_argument("--json", action="store_true")
    for name, help_text in [("sessions", "List saved sessions"), ("doctor", "Check local setup without contacting a model")]:
        command = sub.add_parser(name, help=help_text)
        workspace(command)
    trace = sub.add_parser("trace", help="Export a session's conversation and event journal as JSON")
    workspace(trace)
    trace.add_argument("session")
    memory = sub.add_parser("memory", help="List or remove workspace memory")
    workspace(memory)
    memory.add_argument("--forget", metavar="KEY")
    bt = sub.add_parser("backtest", help="Backtest a daily date,close CSV without a model")
    workspace(bt)
    bt.add_argument("csv", help="Workspace-relative CSV path")
    bt.add_argument("--fast", type=int, default=10)
    bt.add_argument("--slow", type=int, default=30)
    bt.add_argument("--capital", type=float, default=10_000)
    bt.add_argument("--fee-bps", type=float, default=10)
    bt.add_argument("--slippage-bps", type=float, default=5)
    bt.add_argument("--exposure", type=float, default=1)
    bt.add_argument("--max-drawdown", type=float, default=.20)
    bt.add_argument("--periods-per-year", type=int, default=252)
    bt.add_argument("--output", help="New workspace-relative .json or .md report (never overwrites)")
    return parser


def run_agent(args, store, workspace):
    if args.read_only and args.approve_writes:
        raise HarnessError("Choose either --read-only or --approve-writes.")
    api_key = os.getenv("EIRA_API_KEY") or os.getenv("OPENAI_API_KEY", "")
    provider = Provider(args.model, args.base_url, api_key)
    policy = Policy(approve=approve, approve_writes=args.approve_writes, read_only=args.read_only,
                    allowed_hosts={h.lower() for h in args.allow_host}, shell_mode=args.shell,
                    docker_image=args.docker_image)
    session = args.session or store.create(args.prompt if args.command == "run" else "Interactive session")
    toolbox = Toolbox(workspace, store, policy, session)
    agent = Agent(provider, store, toolbox, renderer(args.json), Limits(
        max_steps=args.max_steps, max_tool_calls=args.max_tool_calls,
        max_context_chars=args.max_context_chars, max_total_tokens=args.max_tokens))
    if args.command == "run":
        return 0 if agent.run(args.prompt)["status"] == "completed" else 3
    if not sys.stdin.isatty():
        raise HarnessError("chat requires an interactive terminal; use run for scripts.")
    print_safe("Eira · /exit to leave · session " + session)
    while True:
        try:
            print("\nyou › ", end="", file=sys.stderr, flush=True)
            prompt = input().strip()
        except EOFError:
            break
        if prompt in {"/exit", "/quit"}:
            break
        if not prompt:
            continue
        try:
            agent.run(prompt)
        except HarnessError as exc:
            if args.json:
                print(json.dumps({"event": "error", "error": store.redact(str(exc))}))
            else:
                print_safe(str(exc), file=sys.stderr)
    return 0


def main(argv=None):
    args = build_parser().parse_args(argv)
    store = None
    try:
        if args.command in {"init", "demo"}:
            args.workspace.mkdir(parents=True, exist_ok=True)
        workspace = Workspace(args.workspace)
        if args.command == "doctor":
            report = {"version": __version__, "python": sys.version.split()[0],
                      "workspace": str(workspace.root), "model": os.getenv("EIRA_MODEL") or "not configured",
                      "api_key_present": bool(os.getenv("EIRA_API_KEY") or os.getenv("OPENAI_API_KEY")),
                      "docker_available": bool(shutil.which("docker")),
                      "session_lock_supported": os.name == "posix",
                      "live_provider_tested": False}
            print_safe(json.dumps(report, indent=2))
            return 0
        if args.command == "backtest":
            result = backtest(workspace.read(args.csv, 5_000_000),
                              **{key: getattr(args, key) for key in
                                 ("fast", "slow", "capital", "fee_bps", "slippage_bps", "exposure", "max_drawdown", "periods_per_year")})
            if args.output:
                target = workspace.path(args.output)
                if target.suffix not in {".json", ".md"}:
                    raise HarnessError("Report output must end in .json or .md.")
                if target.exists():
                    raise HarnessError("Output already exists; choose a new report path.")
                content = json.dumps(result, indent=2, allow_nan=False) if target.suffix == ".json" else markdown_report(result)
                atomic_write(target, content)
                print_safe(str(target))
            else:
                print_safe(json.dumps(result, indent=2, allow_nan=False))
            return 0
        store = Store(workspace.root)
        if args.command == "init":
            target = workspace.path("EIRA.md")
            if not target.exists():
                atomic_write(target, "# Project guidance\n\nDescribe your project, preferred workflow, and verification commands here.\nKeep credentials out of this file. Permissions are controlled by CLI policy.\n")
            print_safe(f"Initialized {workspace.root}\nProject guidance: EIRA.md\nLocal state: .eira/")
        elif args.command == "demo":
            target = workspace.path("synthetic.csv")
            if target.exists() and workspace.read("synthetic.csv") != sample_csv():
                raise HarnessError("synthetic.csv already contains different data. Use an empty demo workspace.")
            atomic_write(target, sample_csv())
            session = store.create("Offline synthetic-data demo")
            toolbox = Toolbox(workspace, store, Policy(read_only=True), session)
            result = Agent(DemoProvider(), store, toolbox, renderer(args.json)).run(
                "Inspect synthetic.csv and backtest SMA 5/20 with costs. Report the verified metrics and label synthetic data.")
            return 0 if result["status"] == "completed" else 3
        elif args.command == "sessions":
            print_safe(json.dumps(store.sessions(), indent=2))
        elif args.command == "trace":
            print_safe(json.dumps({"session": args.session, "messages": store.messages(args.session),
                                   "events": store.events(args.session)}, indent=2))
        elif args.command == "memory":
            if args.forget:
                store.forget(args.forget)
            print_safe(json.dumps(store.memories(), indent=2))
        else:
            return run_agent(args, store, workspace)
        return 0
    except KeyboardInterrupt:
        print_safe("Interrupted. Any started session was saved; in-flight tool outcomes may be unknown.", file=sys.stderr)
        return 130
    except (HarnessError, OSError, ValueError) as exc:
        if getattr(args, "json", False):
            print(json.dumps({"event": "error", "error": Redactor()(str(exc))}), flush=True)
        else:
            print_safe(f"Eira: {exc}", file=sys.stderr)
        return 2
    finally:
        if store:
            store.close()
