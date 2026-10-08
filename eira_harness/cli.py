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
from .provider import DEFAULT_MAX_OUTPUT_TOKENS, DEFAULT_MODEL_TIMEOUT, MAX_MODEL_TIMEOUT, build_provider
from .security import HarnessError, Redactor, Workspace, atomic_write, clean_terminal, approval_text, redact_tree
from .store import Store
from .syntax import parse_lint
from .tools import Policy, Toolbox


def print_safe(value: str, file=None):
    print(clean_terminal(Redactor()(value)), file=file or sys.stdout, flush=True)


def approve(name: str, detail: str) -> bool:
    # Piped input can never silently authorize a side effect.
    if not sys.stdin.isatty():
        return False
    print_safe(f"\nApproval required · {name} (JSON-escaped lines)", file=sys.stderr)
    print(approval_text(Redactor()(detail)), file=sys.stderr, flush=True)
    print("\nApprove this action? [y/N] ", end="", file=sys.stderr, flush=True)
    return sys.stdin.readline().strip().lower() == "y"


def compact_count(value) -> str:
    value = value or 0
    return f"{value / 1_000_000:.1f}M" if value >= 1_000_000 else f"{value / 1000:.1f}k" if value >= 1000 else str(value)


def run_summary(event) -> str:
    parts = [f"{event.get('tools', 0)} tool{'s' if event.get('tools') != 1 else ''}",
             f"{compact_count(event.get('tokens'))} tokens"]
    if event.get("seconds") is not None:
        parts.append(f"{event['seconds']:.1f}s")
    return " · ".join(parts)


def provider_options(args) -> dict:
    return {"timeout": getattr(args, "model_timeout", DEFAULT_MODEL_TIMEOUT),
            "max_output_tokens": getattr(args, "max_output_tokens", DEFAULT_MAX_OUTPUT_TOKENS),
            "prompt_cache": not getattr(args, "no_prompt_cache", False)}


def limits_from(args) -> Limits:
    return Limits(
        max_steps=args.max_steps,
        max_tool_calls=args.max_tool_calls,
        max_context_chars=args.max_context_chars,
        max_total_tokens=args.max_tokens,
        instructions=getattr(args, "instructions", None) or ("workspace" if args.command == "eval" else "all"),
        compact=not args.no_compact,
        checkpoints=not args.no_checkpoints,
    )


def policy_from(args) -> Policy:
    return Policy(
        approve=approve,
        approve_writes=args.approve_writes,
        read_only=args.read_only,
        allowed_hosts={h.lower() for h in args.allow_host},
        shell_mode=args.shell,
        docker_image=args.docker_image,
        allowed_data_sources=set(args.allow_data_source),
        syntax_guard=args.syntax_guard,
        lint_commands=parse_lint(args.lint_cmd),
    )


def renderer(as_json: bool):
    def emit(event):
        if as_json:
            print(json.dumps(event, ensure_ascii=True, allow_nan=False), flush=True)
            return
        kind = event["event"]
        if kind == "run_started":
            print_safe(f"\nEIRA / {event['model']}\nSession {event['session']}\n", file=sys.stderr)
        elif kind == "tool_started":
            detail = f"  {event['detail']}" if event.get("detail") else ""
            print_safe(f"  → {event['name']}{detail}", file=sys.stderr)
        elif kind == "tool_completed" and not event["ok"]:
            print_safe(f"  ! {event['error']}", file=sys.stderr)
        elif kind == "sandbox_protected_path_created":
            print_safe(f"  ! shell command created or replaced protected config paths: {', '.join(event['paths'])}. "
                       "Review them before trusting them.", file=sys.stderr)
        elif kind == "compaction_started":
            print_safe("  · compacting context…", file=sys.stderr)
        elif kind == "context_compacted":
            print_safe(f"  · context compacted: {event['replaced_messages']} messages summarized; originals stay in the trace", file=sys.stderr)
        elif kind in {"checkpoint_failed", "checkpoint_skipped"}:
            print_safe(f"  ! no checkpoint for this step ({event['reason']}); continuing", file=sys.stderr)
        elif kind == "assistant":
            print_safe(event["text"])
        elif kind == "run_completed":
            print_safe(f"\n✓ {run_summary(event)}", file=sys.stderr)
        elif kind == "run_stopped":
            print_safe(f"Stopped: {event['reason']}. Session saved. {run_summary(event)}", file=sys.stderr)
        elif kind == "recovered_tool":
            print_safe("Recovered an interrupted tool call; its outcome is unknown. It was not replayed.", file=sys.stderr)
    return emit


def build_parser():
    parser = argparse.ArgumentParser(prog="eira", description="A local agent harness for coding and financial research.")
    parser.add_argument("--version", action="version", version=f"Eira {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    def workspace(command):
        command.add_argument("--workspace", type=Path, default=Path.cwd(), help="Workspace directory (default: current directory)")

    def provider_flags(command):
        command.add_argument("--model", default=None)
        command.add_argument("--provider", choices=["openai", "anthropic", "openrouter", "gemini", "ollama", "custom"], default=None)
        command.add_argument("--base-url", default=None)
        command.add_argument("--max-output-tokens", type=int, default=DEFAULT_MAX_OUTPUT_TOKENS,
                             help="Per-response output cap sent to the Anthropic profile (default %(default)s)")
        command.add_argument("--model-timeout", type=int, default=DEFAULT_MODEL_TIMEOUT,
                             help=f"Seconds allowed per model request, including retries (max {MAX_MODEL_TIMEOUT})")
        command.add_argument("--no-prompt-cache", action="store_true", help="Do not send prompt-cache markers to Anthropic")

    def limit_flags(command):
        command.add_argument("--max-steps", type=int, default=20)
        command.add_argument("--max-tool-calls", type=int, default=50)
        command.add_argument("--max-tokens", type=int, default=100_000, help="Cumulative reported usage; checked between requests, not a hard billing cap")
        command.add_argument("--max-context-chars", type=int, default=120_000)
        command.add_argument("--instructions", choices=["all", "workspace", "none"], default=None,
                             help="Instruction files to load: global, project and workspace (all; default except eval), "
                                  "workspace only (default for eval), or none")
        command.add_argument("--no-compact", action="store_true", help="Stop at the context limit instead of summarizing older turns")
        command.add_argument("--no-checkpoints", action="store_true", help="Do not snapshot the workspace before file-changing tools")

    def model_options(command):
        workspace(command)
        provider_flags(command)
        command.add_argument("--allow-data-source", action="append", choices=["alphavantage", "coinbase"], default=[], help="Preapprove a native daily-price source")
        command.add_argument("--session", help="Resume a session from this workspace")
        command.add_argument("--read-only", action="store_true", help="Deny file writes, memory writes, and shell calls")
        command.add_argument("--approve-writes", action="store_true", help="Preapprove workspace file and memory writes; never shell/network")
        command.add_argument("--allow-host", action="append", default=[], help="Preapprove HTTPS GET requests to this exact hostname (repeatable)")
        command.add_argument("--shell", choices=["disabled", "docker"], default="disabled")
        command.add_argument("--docker-image", default="python:3.11-slim", help="Pre-pulled Docker image for shell mode")
        command.add_argument("--syntax-guard", choices=["reject", "warn", "off"], default="reject",
                             help="Reject edits that break Python, JSON or TOML syntax (default), only warn, or skip checks")
        command.add_argument("--lint-cmd", action="append", default=[], metavar="GLOB=COMMAND",
                             help="After a write, run COMMAND in the Docker sandbox for matching files; {path} is the file (repeatable)")
        limit_flags(command)
        command.add_argument("--json", action="store_true", help="Emit JSONL events to stdout")

    run = sub.add_parser("run", help="Run one task")
    run.add_argument("prompt")
    model_options(run)
    chat = sub.add_parser("chat", help="Start an interactive conversation")
    model_options(chat)
    setup = sub.add_parser("setup", help="Configure your default model interactively")
    model_options(setup)
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
    checkpoints = sub.add_parser("checkpoints", help="List a session's workspace checkpoints")
    workspace(checkpoints)
    checkpoints.add_argument("--session", help="Session ID (default: the session with the latest checkpoint)")
    checkpoints.add_argument("--json", action="store_true")
    diff = sub.add_parser("diff", help="Show workspace changes since a checkpoint")
    workspace(diff)
    diff.add_argument("checkpoint", nargs="?", help="ck-<id> or turn:N (default: the session's first checkpoint)")
    diff.add_argument("--session", help="Session ID (default: the session with the latest checkpoint)")
    diff.add_argument("--stat", action="store_true", help="List changed files with line counts only")
    rewind = sub.add_parser("rewind", help="Restore code, conversation, or both to a checkpoint")
    workspace(rewind)
    rewind.add_argument("target", help="ck-<id> or turn:N")
    rewind_mode = rewind.add_mutually_exclusive_group(required=True)
    for flag in ("code", "conversation", "both"):
        rewind_mode.add_argument(f"--{flag}", dest="mode", action="store_const", const=flag)
    rewind.add_argument("--yes", action="store_true", help="Skip the confirmation prompt (for scripts)")
    rewind.add_argument("--session", help="Session ID (default: the session with the latest checkpoint)")
    memory = sub.add_parser("memory", help="List or remove workspace memory")
    workspace(memory)
    memory.add_argument("--forget", metavar="KEY")
    listing = sub.add_parser("instructions", help="List the instruction files (EIRA.md, AGENTS.md, ...) Eira would load")
    workspace(listing)
    listing.add_argument("--instructions", choices=["all", "workspace", "none"], default="all")
    listing.add_argument("--json", action="store_true")
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
    evaluate = sub.add_parser("eval", help="Score a model on a task suite in throwaway workspaces")
    workspace(evaluate)
    evaluate.add_argument("suite", nargs="?", default="starter", help="Workspace-relative suite JSON, or 'starter' (default)")
    provider_flags(evaluate)
    limit_flags(evaluate)
    evaluate.add_argument("--repeat", type=int, default=1, help="Run every task this many times (1–20)")
    evaluate.add_argument("--work-dir", type=Path, help="Keep task workspaces under this directory (relative to --workspace)")
    evaluate.add_argument("--output", help="Write the JSON report to a new workspace-relative file")
    evaluate.add_argument("--dump-suite", action="store_true", help="Print the suite as JSON without running it")
    evaluate.add_argument("--json", action="store_true", help="Print only the JSON report")
    sub.add_parser("providers", help="List model provider profiles and credential variables")
    prices = sub.add_parser("prices", help="Download daily prices from a fixed financial-data source")
    workspace(prices)
    prices.add_argument("source", choices=["alphavantage", "coinbase"])
    prices.add_argument("symbol", help="Stock symbol or crypto pair such as BTC-USD")
    prices.add_argument("--output", help="Save a new workspace-relative CSV; never overwrite")
    return parser


def configure_model(args, ui, force=False, choose_provider=True):
    """Explicit terminal setup; save preferences, never an API key or authority."""
    import getpass
    from .provider import PROFILES, _same_base_url, _model_name, _validate_endpoint
    from .settings import save_settings
    def ask(label):
        print(clean_terminal(Redactor()(label)), end="", file=sys.stderr, flush=True)
        return input().strip()

    selected, model, endpoint = args.provider, args.model, args.base_url
    if force or not model:
        ui.notice('Model setup · choose a tool-capable model from your provider.')
        if choose_provider:
            names = list(PROFILES)
            ui.notice('\n'.join(f'  {i + 1}. {PROFILES[name]["label"]}' for i, name in enumerate(names)))
            answer = ask(f'Provider [{selected}]: ').lower()
            if answer:
                candidate = names[int(answer) - 1] if answer.isdigit() and 1 <= int(answer) <= len(names) else answer
                if candidate not in PROFILES:
                    raise HarnessError('Choose a listed provider name or number.')
                if candidate != selected:
                    model, endpoint = '', None
                selected = candidate
        if selected == 'custom':
            endpoint = ask(f'Base URL [{endpoint or "https://your-server/v1"}]: ') or endpoint
            if not endpoint:
                raise HarnessError('A custom endpoint is required.')
        ui.notice('Paste the exact model ID shown by your provider. Local models must support tools.')
        model = ask(f'Model ID{f" [{model}]" if model else ""}: ') or model
    _model_name(model)
    profile = PROFILES[selected]
    url = endpoint or profile['default_base_url']
    _validate_endpoint(url)
    standard = bool(profile['default_base_url'] and _same_base_url(url, profile['default_base_url']))
    key_env = profile['api_key_env'] if standard else 'EIRA_API_KEY'
    ui.notice(f'Endpoint: {url}\nConversation and tool results are sent to this endpoint when you submit a task.')
    if not os.getenv(key_env) and (profile['requires_api_key'] or selected == 'custom'):
        ui.notice(f'{key_env} is not set. Enter it privately for this process; it will not be saved.')
        key = getpass.getpass(f'{key_env} (Enter to skip): ')
        if key:
            os.environ[key_env] = key
    provider = build_provider(selected, model, endpoint, **provider_options(args))
    save_settings(selected, model, endpoint)
    args.provider, args.model, args.base_url = selected, model, endpoint
    ui.notice('Model preferences saved. API keys are read from the environment or entered privately each launch.')
    return provider


def run_agent(args, store, workspace):
    if args.read_only and args.approve_writes:
        raise HarnessError('Choose either --read-only or --approve-writes.')
    interactive = args.command == 'chat'
    if interactive and not sys.stdin.isatty():
        raise HarnessError('Chat requires an interactive terminal; use Eira run "your task" for scripts.')
    ui = None
    if interactive:
        from .terminal import Terminal
        ui = Terminal()
    session = args.session or store.create(args.prompt if not interactive else 'Interactive session')
    store.require(session)
    policy = policy_from(args)
    limits = limits_from(args)
    provider = None

    def banner():
        ui.banner(workspace.root, args.provider, args.model or 'Not configured', session,
                  read_only=args.read_only, shell=args.shell)
        if args.approve_writes:
            ui.notice('Workspace file and memory writes are preapproved for this session.')

    if interactive:
        banner()
        try:
            if not args.model:
                provider = configure_model(args, ui)
            else:
                try:
                    provider = build_provider(args.provider, args.model, args.base_url, **provider_options(args))
                except HarnessError:
                    provider = configure_model(args, ui)
        except HarnessError as exc:
            ui.error(str(exc))
            ui.notice('Use /model to finish setup, /help for commands, or /exit to leave.')
        store.redact = Redactor()
    else:
        provider = build_provider(args.provider, args.model, args.base_url, **provider_options(args))

    def make_agent():
        return Agent(provider, store, Toolbox(workspace, store, policy, session),
                     renderer(True) if args.json else (ui.emit if ui else renderer(False)), limits)

    if not interactive:
        return 0 if make_agent().run(args.prompt)['status'] == 'completed' else 3
    while True:
        try:
            prompt = ui.prompt().strip()
        except (EOFError, KeyboardInterrupt):
            ui.notice('Session saved. See you soon.')
            break
        except HarnessError as exc:
            ui.error(str(exc))
            continue
        if not prompt:
            continue
        parts = prompt.split(maxsplit=1)
        command = parts[0].lower()
        argument = parts[1].strip() if len(parts) > 1 else ''
        try:
            if command in {'/exit', '/quit'}:
                ui.notice('Session saved. See you soon.')
                break
            if command == '/help':
                ui.help()
            elif command == '/status':
                banner()
                from .provider import PROFILES
                ui.notice(f'Endpoint: {args.base_url or PROFILES[args.provider]["default_base_url"]}')
                ui.notice(f'Writes: {"denied" if args.read_only else "preapproved" if args.approve_writes else "ask first"}\n'
                          f'Hosts: {", ".join(args.allow_host) or "ask first"}\n'
                          f'Data sources: {", ".join(args.allow_data_source) or "ask first"}\n'
                          f'Context: {args.max_context_chars:,} characters; '
                          f'{"compaction off" if args.no_compact else "older turns are summarized near the limit"}')
            elif command == '/sessions':
                recent = store.sessions()[:20]
                ui.notice('\n'.join(f'{s["id"]}  {s["title"]}' for s in recent) or 'No saved sessions.')
                ui.notice('Resume with /resume SESSION_ID')
            elif command in {'/checkpoints', '/rewind', '/diff'}:
                chat_checkpoints(command, argument, store, workspace, session, ui)
            elif command == '/resume':
                store.require(argument)
                session = argument
                banner()
                ui.notice('Conversation resumed with your current permissions and model.')
            elif command == '/new':
                session = store.create('Interactive session')
                banner()
            elif command in {'/model', '/provider'}:
                # Switching is explicit; old session history is retained and shown by /status.
                provider = configure_model(args, ui, force=True, choose_provider=command == '/provider')
                store.redact = Redactor()
                banner()
            elif command == '/clear':
                if sys.stderr.isatty() and os.getenv('TERM') != 'dumb':
                    print('\033[2J\033[H', end='', file=sys.stderr, flush=True)
                banner()
                ui.notice('Display cleared; conversation history is retained. /new starts a fresh session.')
            elif command == '/instructions':
                from . import instructions
                ui.notice(instructions.report(workspace, limits.instructions))
            elif command.startswith('/'):
                ui.error('Unknown command. Type /help for available commands.')
            else:
                if provider is None:
                    provider = configure_model(args, ui)
                    store.redact = Redactor()
                make_agent().run(prompt)
        except KeyboardInterrupt:
            ui.notice('Task interrupted. History is saved; unfinished tool outcomes may be unknown.')
        except (HarnessError, OSError, ValueError) as exc:
            ui.error(str(exc))
        finally:
            store.redact = Redactor()
    return 0


def checkpoint_command(args, store, workspace):
    from .checkpoints import Checkpoints
    checkpoints = Checkpoints(store, workspace)
    session = args.session or checkpoints.default_session()
    if session is None:
        if args.command == "checkpoints":
            print_safe("[]" if args.json else "No checkpoints in this workspace yet.")
            return 0
        raise HarnessError("No checkpoints in this workspace yet.")
    store.require(session)
    if args.command == "checkpoints":
        rows = checkpoints.listing(session)
        print_safe(json.dumps(rows, indent=2) if args.json else f"Session {session}\n" + checkpoints.format_listing(rows))
        return 0
    if args.command == "diff":
        print_safe(checkpoints.diff(session, args.checkpoint, stat_only=args.stat))
        return 0

    def confirm(summary):
        print_safe(summary, file=sys.stderr)
        if args.yes:
            return True
        if not sys.stdin.isatty():
            raise HarnessError("Rewind needs confirmation: run it in a terminal, or pass --yes in scripts.")
        print("\nRewind now? [y/N] ", end="", file=sys.stderr, flush=True)
        return sys.stdin.readline().strip().lower() == "y"

    result = checkpoints.rewind(session, args.target, args.mode, confirm)
    if result is None:
        print_safe("Rewind cancelled; nothing changed.", file=sys.stderr)
        return 1
    print_safe(rewind_message(result), file=sys.stderr)
    return 0


def rewind_message(result) -> str:
    parts = [f"Rewound {result['mode']} to {result['checkpoint']}"]
    if result["mode"] != "conversation":
        parts.append(f"{result['restored']} restored, {result['deleted']} deleted"
                     + (f"; undo with: eira rewind {result['backup']} --code" if result.get("backup") else ""))
    if result["mode"] != "code":
        parts.append(f"{result['hidden_messages']} messages hidden from the model")
    text = " · ".join(parts)
    if result.get("prompt") and result["mode"] != "code":
        text += "\nOriginal prompt:\n" + result["prompt"]
    return text


def chat_checkpoints(command, argument, store, workspace, session, ui):
    """/checkpoints, /diff and /rewind for the current chat session."""
    from .checkpoints import Checkpoints
    checkpoints = Checkpoints(store, workspace)
    if command == '/checkpoints':
        ui.notice(checkpoints.format_listing(checkpoints.listing(session)))
        return
    if command == '/diff':
        ui.notice(checkpoints.diff(session, argument or None))
        return
    if not argument:
        ui.notice(checkpoints.format_listing(checkpoints.listing(session)))
        ui.notice('Rewind with /rewind N (a turn number) or /rewind ck-ID.')
        return

    def ask(label):
        print(clean_terminal(label), end='', file=sys.stderr, flush=True)
        return input().strip().lower()

    choice = ask('Restore [c]ode, con[v]ersation, [b]oth, or [n]othing? ')
    mode = {'c': 'code', 'v': 'conversation', 'b': 'both'}.get(choice[:1])
    if mode is None:
        ui.notice('Rewind cancelled; nothing changed.')
        return

    def confirm(summary):
        ui.notice(summary)
        return ask('Rewind now? [y/N] ') == 'y'

    result = checkpoints.rewind(session, argument, mode, confirm, emit=ui.emit)
    if result is None:
        ui.notice('Rewind cancelled; nothing changed.')
        return
    if result.get('backup'):
        ui.notice(f'Undo the file changes with /rewind {result["backup"]} (code).')
    if mode != 'code' and result.get('prompt'):
        # Like an edited resend: the original prompt is shown and Up recalls it.
        recalled = Redactor()(result['prompt'])
        ui.notice('Original prompt (press Up to edit and resend):\n' + recalled)
        ui.history.append(recalled)
        readline = getattr(ui, '_readline', None)
        if readline is not None:
            readline.add_history(recalled)


def run_eval(args, workspace):
    from .evals import load_suite, run_suite
    from .settings import resolve_settings
    suite = load_suite(args.suite, workspace)
    if args.dump_suite:
        print_safe(json.dumps(redact_tree(suite, Redactor()), indent=2, ensure_ascii=False))
        return 0
    target = None
    if args.output:
        target = workspace.path(args.output)
        if target.suffix != ".json" or target.exists():
            raise HarnessError("Choose a new .json report path; existing files are never overwritten.")
        if not target.parent.is_dir() or not os.access(target.parent, os.W_OK):
            raise HarnessError("The report directory must already exist and be writable.")
    resolve_settings(args)
    limits = limits_from(args)
    if any(isinstance(value, (int, float)) and not isinstance(value, bool) and value <= 0
           for value in vars(limits).values()):
        raise HarnessError("All runtime limits must be positive.")
    build_provider(args.provider, args.model, args.base_url, **provider_options(args))
    work_dir = args.work_dir
    if work_dir is not None and not work_dir.is_absolute():
        work_dir = workspace.root / work_dir

    def progress(result, done, total):
        if args.json:
            return
        mark = "✓" if result["passed"] else "✗"
        reason = "" if result["passed"] else "  " + (result.get("error") or ", ".join(
            f"{c['type']}{' ' + c['path'] if 'path' in c else ''}" for c in result["checks"] if not c["passed"]))
        print_safe(f"[{done}/{total}] {mark} {result['task']}  ({result['tool_calls']} tools, "
                   f"{compact_count(result['tokens'])} tokens, {result['seconds']:.1f}s){reason}", file=sys.stderr)

    if not args.json:
        print_safe(f"Eira eval · suite {suite['name']} · {args.provider} / {args.model} · "
                   f"{len(suite['tasks'])} tasks × {args.repeat}", file=sys.stderr)
    report = run_suite(suite, lambda: build_provider(args.provider, args.model, args.base_url, **provider_options(args)),
                       limits, repeat=args.repeat, work_dir=work_dir, progress=progress)
    report["provider"] = args.provider
    encoded = json.dumps(redact_tree(report, Redactor()), indent=2, ensure_ascii=False, allow_nan=False)
    if target:
        try:
            atomic_write(target, encoded + "\n", overwrite=False)
        except OSError as exc:
            # Never lose a paid run: fall back to stdout (once, even with --json).
            print_safe(f"Eira: could not write {target}: {exc}. The report follows on stdout.", file=sys.stderr)
            if not args.json:
                print_safe(encoded)
            target = None
    summary = report["summary"]
    if args.json:
        print_safe(encoded)
    else:
        print_safe(f"\nPassed {summary['passed']}/{summary['runs']} ({summary['pass_rate']:.0%}) · "
                   f"{summary['tool_errors']} tool errors · {compact_count(summary['tokens'])} tokens · "
                   f"{summary['seconds']:.1f}s" + (f"\nReport: {target}" if target else ""), file=sys.stderr)
    return 0 if summary["passed"] == summary["runs"] else 1


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or (argv[0].startswith("-") and argv[0] not in {"-h", "--help", "--version"}):
        argv.insert(0, "chat")
    args = build_parser().parse_args(argv)
    store = None
    try:
        if args.command == "providers":
            from .provider import PROFILES
            print_safe(json.dumps(PROFILES, indent=2))
            return 0
        if args.command in {"init", "demo"}:
            args.workspace.mkdir(parents=True, exist_ok=True)
        workspace = Workspace(args.workspace)
        if args.command == "doctor":
            from . import patch
            from .provider import PROFILES
            from .settings import resolve_settings
            selected = resolve_settings(argparse.Namespace(provider=None, model=None, base_url=None))
            key_env = PROFILES[selected.provider]["api_key_env"] if not selected.base_url else "EIRA_API_KEY"
            report = {"version": __version__, "python": sys.version.split()[0],
                      "workspace": str(workspace.root), "model": selected.model or "not configured",
                      "provider": selected.provider, "api_key_present": bool(os.getenv(key_env)),
                      "docker_available": bool(shutil.which("docker")),
                      "session_lock_supported": os.name == "posix",
                      "live_provider_tested": False,
                      "incomplete_patches": patch.leftovers(workspace.root / ".eira")}
            print_safe(json.dumps(report, indent=2))
            return 0
        if args.command == "prices":
            from .market_data import fetch_prices
            data = fetch_prices(args.source, args.symbol)
            if args.output:
                target = workspace.path(args.output)
                if target.suffix.lower() != ".csv" or target.exists():
                    raise HarnessError("Choose a new .csv output path; existing files are never overwritten.")
                atomic_write(target, data["csv"], overwrite=False)
                metadata = {key: value for key, value in data.items() if key != "csv"}
                print_safe(json.dumps({"output": str(target), **metadata}, indent=2))
            else:
                print_safe(data["csv"])
                print_safe(json.dumps({key: value for key, value in data.items() if key != "csv"}), file=sys.stderr)
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
                atomic_write(target, content, overwrite=False)
                print_safe(str(target))
            else:
                print_safe(json.dumps(result, indent=2, allow_nan=False))
            return 0
        if args.command == "eval":
            return run_eval(args, workspace)
        if args.command in {"run", "chat", "setup"}:
            from .settings import resolve_settings
            resolve_settings(args)
        if args.command == "setup":
            from .terminal import Terminal
            if not sys.stdin.isatty():
                raise HarnessError("setup requires an interactive terminal.")
            configure_model(args, Terminal(), force=True)
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
            store.require(args.session)
            frozen = store.session_context(args.session)
            print_safe(json.dumps({"session": args.session, "system": frozen["system"] if frozen else None,
                                   "messages": store.messages(args.session),
                                   "events": store.events(args.session)}, indent=2))
        elif args.command in {"checkpoints", "diff", "rewind"}:
            return checkpoint_command(args, store, workspace)
        elif args.command == "memory":
            if args.forget:
                store.forget(args.forget)
            print_safe(json.dumps(store.memories(), indent=2))
        elif args.command == "instructions":
            from . import instructions
            print_safe(instructions.report(workspace, args.instructions, as_json=args.json))
        else:
            return run_agent(args, store, workspace)
        return 0
    except EOFError:
        print_safe("Setup closed. Run Eira again when ready.", file=sys.stderr)
        return 0
    except KeyboardInterrupt:
        if args.command == "eval":
            print_safe("Interrupted. Throwaway eval workspaces were removed unless --work-dir kept them.", file=sys.stderr)
        else:
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
