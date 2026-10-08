"""Small, safe terminal presentation layer for interactive Eira sessions."""
from __future__ import annotations

from dataclasses import dataclass
import os
import shutil
import sys
import time
from typing import TextIO

from . import __version__
from .security import HarnessError, Redactor, clean_terminal


@dataclass(frozen=True)
class _Palette:
    cyan: str = "\x1b[38;5;123m"
    lavender: str = "\x1b[38;5;183m"
    muted: str = "\x1b[38;5;245m"
    green: str = "\x1b[38;5;120m"
    yellow: str = "\x1b[38;5;222m"
    red: str = "\x1b[38;5;210m"
    bold: str = "\x1b[1m"
    reset: str = "\x1b[0m"


class Terminal:
    """Render Eira events without taking ownership of the terminal.

    ANSI styling is enabled only for a TTY and can always be disabled with
    ``NO_COLOR``. Dynamic text goes through Eira's terminal cleaner and
    redactor before it reaches the user's terminal.
    """

    def __init__(self, stream: TextIO | None = None):
        self.stream = stream if stream is not None else sys.stderr
        try:
            tty = bool(self.stream.isatty())
        except (AttributeError, OSError):
            tty = False
        self.color = tty and "NO_COLOR" not in os.environ and os.environ.get("TERM", "") != "dumb"
        self.palette = _Palette()
        self.redactor = Redactor()
        self._thinking_since: float | None = None
        self._workspace = ""
        self._session = ""
        self.history: list[str] = []
        columns = shutil.get_terminal_size((80, 24)).columns
        self.width = max(24, min(columns, 88))
        try:
            import readline
            readline.set_auto_history(False)
            self._readline = readline
        except (ImportError, AttributeError):
            self._readline = None

    def _safe(self, value, limit: int = 100_000) -> str:
        # Refresh secrets on each emission because credentials may be loaded or
        # changed after Terminal construction.
        text = clean_terminal(Redactor()(str(value)))
        if len(text) > limit:
            return text[:limit] + "\n[… output truncated …]"
        return text

    def _write(self, text: str = "", *, end: str = "\n") -> None:
        self.stream.write(text + end)
        self.stream.flush()

    def _paint(self, code: str, text: str) -> str:
        return f"{code}{text}{self.palette.reset}" if self.color else text

    def banner(self, workspace, provider, model, session, read_only: bool = False,
               shell: str = "disabled") -> None:
        """Show a compact context panel before an interactive run."""
        self._workspace = self._safe(workspace, 500)
        self._session = self._safe(session, 200)
        provider_text = self._fit(self._safe(provider, 100), 40)
        model_text = self._fit(self._safe(model, 300), 40)
        permission = "read-only" if read_only else "workspace approvals"
        inner = self.width - 2
        lines = [
            self._row("─ E I R A · local agent", inner, "title"),
            self._row("inspectable sessions · careful tools", inner),
            self._row("", inner, "rule"),
            self._row(f"workspace  {self._field(self._workspace, inner - 12)}", inner),
            self._row(f"provider   {self._field(provider_text, inner - 12)}", inner),
            self._row(f"model      {self._field(model_text, inner - 12)}", inner),
            self._row(f"version    {self._field(__version__, inner - 12)}", inner),
            self._row(f"session    {self._field(self._session, inner - 12)}", inner),
            self._row(f"access     {permission}  shell: {self._field(self._safe(shell, 16), inner - 31)}", inner),
        ]
        top = "╭" + "─" * inner + "╮"
        bottom = "╰" + "─" * inner + "╯"
        if self.color:
            self._write(self._paint(self.palette.cyan, top))
            self._write(self._paint(self.palette.lavender, "│" + lines[0] + "│"))
            self._write("│" + lines[1] + "│")
            for line in lines[2:]:
                if line == "":
                    continue
                self._write("│" + line + "│")
            self._write(self._paint(self.palette.cyan, bottom))
        else:
            self._write(top + "\n" + "\n".join("│" + line + "│" for line in lines) + "\n" + bottom)
        self.help(footer=True)

    def _row(self, text: str, inner: int, kind: str = "") -> str:
        if kind == "rule":
            return "─" * inner
        return ("  " + self._field(text, inner - 4)).ljust(inner)

    def _field(self, text: str, width: int) -> str:
        text = self._safe(text).replace("\n", " ").replace("\r", " ").replace("\t", " ")
        return self._fit(text, max(0, width))

    @staticmethod
    def _fit(text: str, width: int) -> str:
        # Keep the panel geometry stable even when a user supplies a long path.
        if width <= 0:
            return ""
        if width == 1:
            return text if len(text) <= 1 else "…"
        return text if len(text) <= width else "…" + text[-(width - 1):]

    def emit(self, event: dict) -> None:
        """Render an Agent event; unknown events are safely ignored."""
        if not isinstance(event, dict):
            return
        kind = event.get("event")
        if kind == "run_started":
            self._session = self._safe(event.get("session", self._session), 200)
        elif kind == "model_started":
            self._thinking_since = time.monotonic()
            self._write(self._paint(self.palette.muted, "  · thinking…"))
        elif kind == "model_completed":
            elapsed = ""
            if self._thinking_since is not None:
                elapsed = f" ({time.monotonic() - self._thinking_since:.1f}s)"
            self._thinking_since = None
            usage = event.get("usage") if isinstance(event.get("usage"), dict) else {}
            cached = usage.get("cache_read_input_tokens")
            cache = f" · {self._count(cached)} cached" if isinstance(cached, int) and cached > 0 else ""
            self._write(self._paint(self.palette.muted, f"  · model ready{elapsed}{cache}"))
        elif kind == "tool_started":
            name = self._safe(event.get("name", "tool"), 160)
            room = max(0, self.width - len(name) - 8)
            detail = " ".join(self._safe(event.get("detail") or "", 500).split())
            # Keep the head: a path or command reads from its start.
            detail = detail if len(detail) <= room else (detail[:room - 1] + "…" if room > 1 else "")
            line = self._paint(self.palette.cyan, f"  → {name}")
            self._write(line + (self._paint(self.palette.muted, f"  {detail}") if detail else ""))
        elif kind == "compaction_started":
            self._write(self._paint(self.palette.muted, "  · compacting context…"))
        elif kind == "context_compacted":
            self.notice(f"Context compacted: {event.get('replaced_messages', 0)} earlier messages summarized. "
                        "Originals stay in the session trace.")
        elif kind == "workspace_context_updated":
            self._write(self._paint(self.palette.muted, "  · workspace guidance or memory changed; update sent to the model"))
        elif kind == "tool_completed":
            if event.get("ok"):
                self._write(self._paint(self.palette.green, "  ✓ tool completed"))
            else:
                self.error(event.get("error", "Tool failed"))
        elif kind == "assistant":
            self._write(self._safe(event.get("text", "")), end="\n")
        elif kind == "sandbox_protected_path_created":
            paths = event.get("paths") if isinstance(event.get("paths"), list) else []
            self.notice("Warning: the shell command created or replaced protected config paths: "
                        f"{', '.join(str(path) for path in paths)}. Review them before trusting them.")
        elif kind == "recovered_tool":
            self.notice("Recovered an interrupted tool call; its outcome is unknown. It was not replayed.")
        elif kind == "run_stopped":
            self.notice(f"Stopped: {event.get('reason', 'limit')}. Session saved.")
        elif kind == "run_completed":
            seconds = event.get("seconds")
            timing = f" · {seconds:.1f}s" if isinstance(seconds, (int, float)) else ""
            tools = event.get("tools", 0)
            self._write(self._paint(self.palette.green, f"✓ Run completed · {tools} tool{'s' if tools != 1 else ''} · "
                                                        f"{self._count(event.get('tokens', 0))} tokens{timing}"))
        elif kind == "run_failed":
            self.error(event.get("error", "Run failed"))

    @staticmethod
    def _count(value) -> str:
        value = value if isinstance(value, int) else 0
        return f"{value / 1_000_000:.1f}M" if value >= 1_000_000 else f"{value / 1000:.1f}k" if value >= 1000 else str(value)

    def notice(self, text) -> None:
        self._write(self._paint(self.palette.yellow, self._safe(text, 8_000)))

    def error(self, text) -> None:
        self._write(self._paint(self.palette.red, self._safe(text, 8_000)))

    def help(self, footer: bool = False) -> None:
        if footer:
            self._write(self._paint(self.palette.muted, "  /help commands · /status context · /exit quit"))
            return
        self._write("Eira commands:")
        self._write("  /help              Show this help")
        self._write("  /model             Choose the model for a new run")
        self._write("  /provider          Choose the provider for a new run")
        self._write("  /new               Start a new session")
        self._write("  /sessions          List saved sessions")
        self._write("  /resume ID         Resume a saved session")
        self._write("  /status            Show workspace, model, and permission context")
        self._write("  /clear             Clear the visible terminal")
        self._write("  /exit              Leave chat")

    def status(self) -> None:
        self.notice(f"workspace={self._workspace} session={self._session}")

    def clear(self) -> None:
        if self.color:
            self._write("\x1b[2J\x1b[H", end="")
        else:
            self._write("\n" * 3, end="")

    def prompt(self) -> str:
        """Read one line from stdin; history remains process-memory only."""
        self._write(self._paint(self.palette.cyan, "you › "), end="")
        try:
            line = input()
        except EOFError:
            return "/exit"
        cleaned = clean_terminal(line)
        if cleaned != line:
            raise HarnessError("Input contains terminal control characters; please retype it.")
        line = cleaned
        remembered = Redactor()(line)
        if line and (not self.history or self.history[-1] != remembered):
            self.history.append(remembered)
            del self.history[:-100]
            if self._readline is not None:
                self._readline.add_history(remembered)
                while self._readline.get_current_history_length() > 100:
                    self._readline.remove_history_item(0)
        return line

    @staticmethod
    def parse_command(line: str) -> tuple[str | None, str]:
        """Return a normalized slash command and its argument string."""
        if not isinstance(line, str) or not line.startswith("/"):
            return None, line if isinstance(line, str) else ""
        command, _, args = line[1:].partition(" ")
        return command.strip().lower() or None, args.strip()
