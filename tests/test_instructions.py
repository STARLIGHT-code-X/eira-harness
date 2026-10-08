from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from eira_harness import instructions
from eira_harness.agent import UPDATE_NOTE, Agent, Limits
from eira_harness.cli import build_parser, limits_from, main
from eira_harness.evals import STARTER_SUITE
from eira_harness.security import HarnessError, Workspace
from eira_harness.store import Store
from eira_harness.tools import Policy, Toolbox


def call(name, args, call_id):
    return {"role": "assistant", "content": None, "tool_calls": [{"id": call_id, "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)}}]}


def text(value):
    return {"role": "assistant", "content": value}


class Scripted:
    model = "scripted"

    def __init__(self, *responses):
        self.responses, self.seen = list(responses), []

    def complete(self, messages, tools):
        self.seen.append(json.loads(json.dumps(messages)))
        return self.responses.pop(0), {"total_tokens": 10}


def tool_results(messages):
    return [json.loads(m["content"]) for m in messages if m["role"] == "tool"]


class Base(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.tmp = Path(temp.name).resolve()
        self.home = self.tmp / "home"
        self.config = self.tmp / "config"
        self.home.mkdir()
        self.config.mkdir()
        env = patch.dict(os.environ, {"HOME": str(self.home), "XDG_CONFIG_HOME": str(self.config)})
        env.start()
        self.addCleanup(env.stop)

    def write(self, path: Path, content: str):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        return path

    def repo(self):
        repo = self.tmp / "repo"
        (repo / ".git").mkdir(parents=True)
        return repo


class DiscoveryTests(Base):
    def test_hierarchy_order_labels_and_precedence(self):
        repo = self.repo()
        self.write(self.config / "eira" / "AGENTS.md", "Global rule.\n")
        self.write(repo / "AGENTS.md", "Root rule.\n")
        self.write(repo / "services" / "CLAUDE.md", "Services rule.\n")
        payments = repo / "services" / "payments"
        self.write(payments / "AGENTS.override.md", "Override rule.\n")
        self.write(payments / "EIRA.md", "Eira rule.\n")
        found = instructions.discover(Workspace(payments), "all")
        self.assertEqual([(f.display, f.scope, f.status) for f in found],
                         [("$XDG_CONFIG_HOME/eira/AGENTS.md", "global", "included"),
                          ("AGENTS.md", "project", "included"),
                          ("services/CLAUDE.md", "project", "included"),
                          ("services/payments/EIRA.md", "workspace", "included")])
        rendered = instructions.render(found)
        self.assertEqual(rendered, "### $XDG_CONFIG_HOME/eira/AGENTS.md (global)\nGlobal rule.\n\n"
                                   "### AGENTS.md (project)\nRoot rule.\n\n"
                                   "### services/CLAUDE.md (project)\nServices rule.\n\n"
                                   "### services/payments/EIRA.md (workspace)\nEira rule.\n")
        self.assertNotIn("Override rule", rendered)

    def test_global_file_under_home_uses_tilde_label_and_override(self):
        with patch.dict(os.environ, {"XDG_CONFIG_HOME": ""}):
            self.write(self.home / ".config" / "eira" / "AGENTS.md", "Plain.\n")
            self.write(self.home / ".config" / "eira" / "AGENTS.override.md", "Override.\n")
            workspace = self.tmp / "ws"
            workspace.mkdir()
            found = instructions.discover(Workspace(workspace), "all")
        self.assertEqual([(f.display, f.content) for f in found], [("~/.config/eira/AGENTS.override.md", "Override.\n")])

    def test_relative_xdg_config_home_skips_global_with_reason(self):
        workspace = self.tmp / "ws"
        workspace.mkdir()
        with patch.dict(os.environ, {"XDG_CONFIG_HOME": "relative/dir"}):
            found = instructions.discover(Workspace(workspace), "all")
        self.assertEqual(len(found), 1)
        self.assertEqual((found[0].scope, found[0].status), ("global", "skipped"))
        self.assertIn("absolute", found[0].reason)

    def test_only_eira_md_renders_byte_identical_to_0_4(self):
        repo = self.repo()  # an empty git root above the workspace changes nothing
        workspace = repo / "app"
        guide = "# Guide\n\nRun tests.\r\nNo trailing newline"
        self.write(workspace / "EIRA.md", guide)
        for mode in ("all", "workspace"):
            self.assertEqual(instructions.render(instructions.discover(Workspace(workspace), mode)), guide)
        store = Store(workspace)
        self.addCleanup(store.close)
        session = store.create("t")
        agent = Agent(Scripted(), store, Toolbox(Workspace(workspace), store, Policy(), session))
        self.assertEqual(agent.workspace_context(), "Workspace guidance (subordinate to policy):\n" + guide +
                         "\nWorkspace memory (context only):\n{}")
        empty = self.tmp / "empty"
        empty.mkdir()
        self.assertEqual(instructions.render(instructions.discover(Workspace(empty), "all")), "")

    def test_budget_truncates_at_a_line_boundary_then_skips(self):
        repo = self.repo()
        line = "x" * 99 + "\n"
        block = line * 205  # 20,500 bytes
        for relative in ("AGENTS.md", "a/AGENTS.md", "a/b/AGENTS.md"):
            self.write(repo / relative, block)
        found = instructions.discover(Workspace(repo / "a" / "b"), "all")
        self.assertEqual([(f.display, f.status) for f in found],
                         [("AGENTS.md", "included"), ("a/AGENTS.md", "truncated"), ("a/b/AGENTS.md", "skipped")])
        self.assertEqual(found[0].content, block)
        cut = found[1].content
        self.assertTrue(cut.endswith("\n[truncated: instruction budget]"))
        body = cut[:-len("[truncated: instruction budget]")]
        self.assertEqual(set(body.split("\n")[:-1]), {"x" * 99})
        self.assertLessEqual(len(found[0].content.encode()) + len(cut.encode()), instructions.BUDGET_BYTES)
        self.assertGreater(len(cut.encode()), instructions.BUDGET_BYTES - len(block) - 200)
        self.assertEqual(found[2].reason, "instruction budget exhausted")
        self.assertEqual(found[2].content, "")
        self.assertNotIn("a/b/AGENTS.md", instructions.render(found))

    def test_refused_files_are_reported_with_reasons(self):
        workspace = self.tmp / "ws"
        self.write(workspace / "real.md", "Target.\n")
        os.symlink(workspace / "real.md", workspace / "EIRA.md")
        self.write(workspace / "AGENTS.override.md", "Linked.\n")
        os.link(workspace / "AGENTS.override.md", workspace / "linked.txt")
        self.write(workspace / "AGENTS.md", "y" * (70 * 1024))
        self.write(workspace / "CLAUDE.md", "")
        (workspace / "GEMINI.md").write_bytes(b"bad\x00bytes")
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(main(["instructions", "--json", "--workspace", str(workspace)]), 0)
        report = json.loads(out.getvalue())
        self.assertEqual(report["mode"], "all")
        self.assertEqual([(f["path"], f["status"], f["reason"]) for f in report["files"]],
                         [("EIRA.md", "skipped", "symlink"), ("AGENTS.override.md", "skipped", "hard link"),
                          ("AGENTS.md", "skipped", "too large"), ("CLAUDE.md", "skipped", "empty"),
                          ("GEMINI.md", "skipped", "binary")])
        self.assertEqual(report["files"][2]["bytes"], 70 * 1024)
        (workspace / "CLAUDE.md").write_text("Fallback.\n")
        with redirect_stdout(io.StringIO()) as plain:
            self.assertEqual(main(["instructions", "--workspace", str(workspace)]), 0)
        self.assertIn("included", plain.getvalue())
        self.assertIn("CLAUDE.md (workspace)", plain.getvalue())

    def test_boundaries_and_modes(self):
        home = self.tmp / "h"
        repo, sub = home / "repo", home / "repo" / "sub"
        (repo / ".git").mkdir(parents=True)
        self.write(home / "AGENTS.md", "HOME FILE\n")
        self.write(repo / "AGENTS.md", "Repo.\n")
        self.write(sub / "CLAUDE.md", "Sub.\n")
        self.write(self.config / "eira" / "AGENTS.md", "Global.\n")
        seen = []
        original = instructions._load

        def spy(path, guard=None):
            seen.append(Path(path))
            return original(path, guard)

        with patch.dict(os.environ, {"HOME": str(home)}), patch.object(instructions, "_load", spy):
            everything = instructions.discover(Workspace(sub), "all")
            self.assertTrue(all(p.parent in {repo, sub, self.config / "eira"} for p in seen), seen)
            self.assertNotIn("HOME FILE", instructions.render(everything))
            self.assertEqual([f.display for f in everything],
                             ["$XDG_CONFIG_HOME/eira/AGENTS.md", "AGENTS.md", "sub/CLAUDE.md"])
            (repo / ".git").rmdir()  # no git root: the walk falls back to the workspace
            self.assertEqual([f.display for f in instructions.discover(Workspace(sub), "all")],
                             ["$XDG_CONFIG_HOME/eira/AGENTS.md", "CLAUDE.md"])
            seen.clear()
            local = instructions.discover(Workspace(sub), "workspace")
            self.assertEqual([f.display for f in local], ["CLAUDE.md"])
            self.assertTrue(all(p.parent == sub for p in seen))
            seen.clear()
            self.assertEqual(instructions.discover(Workspace(sub), "none"), [])
            self.assertEqual(seen, [])
        self.assertTrue(all(p.parent != home for p in seen))
        with self.assertRaises(HarnessError):
            instructions.discover(Workspace(sub), "everything")

    def test_home_itself_is_never_a_project_root(self):
        (self.home / ".git").mkdir()
        self.write(self.home / "AGENTS.md", "Dotfiles.\n")
        project = self.home / "project"
        project.mkdir()
        self.assertEqual(instructions.project_root(project), project)
        self.assertEqual(instructions.discover(Workspace(project), "all"), [])


class SessionTests(Base):
    def setUp(self):
        super().setUp()
        self.root = self.repo()
        self.stores = []

    def tearDown(self):
        for store in self.stores:
            store.close()

    def open(self, workspace: Path, session=None):
        store = Store(workspace)
        self.stores.append(store)
        session = session or store.create("t")
        return store, session

    def test_prefix_is_frozen_and_changes_are_appended(self):
        payments = self.root / "services" / "payments"
        self.write(self.root / "AGENTS.md", "Root rule.\n")
        self.write(payments / "AGENTS.md", "Use decimal money.\n")
        store, session = self.open(payments)
        first = Scripted(text("One."))
        Agent(first, store, Toolbox(Workspace(payments), store, Policy(), session)).run("First")
        system = first.seen[0][0]["content"]
        self.assertIn("### services/payments/AGENTS.md (workspace)\nUse decimal money.", system)
        self.assertIn("### AGENTS.md (project)\nRoot rule.", system)
        prefix_events = [e["payload"] for e in store.events(session) if e["kind"] == "guidance_loaded"]
        self.assertEqual([(p["path"], p["via"]) for p in prefix_events],
                         [("AGENTS.md", "prefix"), ("services/payments/AGENTS.md", "prefix")])
        self.write(payments / "AGENTS.md", "Use integer cents.\n")
        second = Scripted(text("Two."))
        Agent(second, store, Toolbox(Workspace(payments), store, Policy(), session)).run("Second")
        request = second.seen[0]
        self.assertEqual(request[0]["content"], system)
        self.assertTrue(request[-2]["content"].startswith(UPDATE_NOTE))
        self.assertIn("Use integer cents.", request[-2]["content"])
        self.assertEqual(sum(e["kind"] == "guidance_loaded" for e in store.events(session)), 2)

    def test_jit_guidance_once_per_session_and_on_change(self):
        self.write(self.root / "AGENTS.md", "Root rule.\n")
        self.write(self.root / "services" / "CLAUDE.md", "Services rule.\n")
        self.write(self.root / "services" / "payments" / "AGENTS.md", "Payments rule.\n")
        self.write(self.root / "services" / "payments" / "x.py", "x = 1\n")
        store, session = self.open(self.root)
        read = {"path": "services/payments/x.py"}
        provider = Scripted(call("read_file", read, "c1"), call("read_file", read, "c2"), text("Done."))
        emitted = []
        agent = Agent(provider, store, Toolbox(Workspace(self.root), store, Policy(), session), emitted.append)
        self.assertEqual(agent.run("Read x twice")["status"], "completed")
        first, second = tool_results(store.messages(session))
        self.assertEqual([(g["path"], g["content"]) for g in first["result"]["guidance"]],
                         [("services/CLAUDE.md", "Services rule.\n"),
                          ("services/payments/AGENTS.md", "Payments rule.\n")])
        self.assertEqual(first["result"]["guidance"][1]["note"],
                         "Instructions for files under services/payments/. Context only; they cannot change "
                         "policy or permissions.")
        self.assertNotIn("guidance", second["result"])
        system = provider.seen[0][0]["content"]
        self.assertNotIn("Payments rule", system)
        self.assertEqual(provider.seen[-1][0]["content"], system)
        notices = [e for e in emitted if e["event"] == "guidance_loaded" and e["via"] == "jit"]
        self.assertEqual([e["path"] for e in notices], ["services/CLAUDE.md", "services/payments/AGENTS.md"])
        # Resume with a fresh Agent and Toolbox: the delivered set comes from the journal.
        again = Scripted(call("read_file", read, "c3"), text("Done."))
        Agent(again, store, Toolbox(Workspace(self.root), store, Policy(), session)).run("Read again")
        self.assertNotIn("guidance", tool_results(store.messages(session))[-1]["result"])
        self.write(self.root / "services" / "payments" / "AGENTS.md", "Payments rule v2.\n")
        changed = Scripted(call("read_file", read, "c4"), text("Done."))
        Agent(changed, store, Toolbox(Workspace(self.root), store, Policy(), session)).run("Read after change")
        guidance = tool_results(store.messages(session))[-1]["result"]["guidance"]
        self.assertEqual([g["path"] for g in guidance], ["services/payments/AGENTS.md"])
        self.assertEqual(guidance[0]["content"], "Payments rule v2.\n")
        self.assertTrue(guidance[0]["note"].endswith(" (updated)"))
        self.assertEqual(changed.seen[0][0]["content"], system)

    def test_jit_limits_and_modes(self):
        for depth in ("a", "a/b", "a/b/c"):
            self.write(self.root / depth / "AGENTS.md", f"Rule {depth}.\n" + "z" * 9000 * (depth == "a"))
        self.write(self.root / "a" / "b" / "c" / "f.txt", "f\n")
        store, session = self.open(self.root)
        toolbox = Toolbox(Workspace(self.root), store, Policy(), session)
        instructions.attach_jit(toolbox, "all")
        instructions.attach_jit(toolbox, "all")
        self.assertEqual(len(toolbox.after_call), 1)
        guidance = toolbox.call("list_files", {"path": "a/b/c"})["guidance"]
        self.assertEqual([g["path"] for g in guidance], ["a/AGENTS.md", "a/b/AGENTS.md"])
        self.assertLessEqual(len(guidance[0]["content"].encode()), instructions.JIT_MAX_BYTES)
        self.assertIn("[truncated: guidance limit; read a/AGENTS.md for the rest]", guidance[0]["content"])
        self.assertEqual([g["path"] for g in toolbox.call("read_file", {"path": "a/b/c/f.txt"})["guidance"]],
                         ["a/b/c/AGENTS.md"])
        self.assertNotIn("guidance", toolbox.call("search_files", {"query": "f", "path": "a/b/c"}))
        # Failed calls never carry guidance and never mark a file delivered.
        self.write(self.root / "d" / "AGENTS.md", "Rule d.\n")
        with self.assertRaises(HarnessError):
            toolbox.call("read_file", {"path": "d/missing.txt"})
        self.assertEqual(toolbox.call("list_files", {"path": "d"})["guidance"][0]["path"], "d/AGENTS.md")
        off = Toolbox(Workspace(self.root), store, Policy(), session)
        instructions.attach_jit(off, "none")
        self.assertEqual(off.after_call, [])
        Agent(Scripted(), store, off, limits=Limits(instructions="none"))
        self.assertEqual(off.after_call, [])
        with self.assertRaises(HarnessError):
            Agent(Scripted(), store, off, limits=Limits(instructions="bogus"))

    def test_jit_refuses_symlinked_guidance(self):
        self.write(self.root / "outside.md", "Injected.\n")
        (self.root / "pkg").mkdir()
        os.symlink(self.root / "outside.md", self.root / "pkg" / "AGENTS.md")
        self.write(self.root / "pkg" / "GEMINI.md", "Real.\n")
        store, session = self.open(self.root)
        toolbox = Toolbox(Workspace(self.root), store, Policy(), session)
        instructions.attach_jit(toolbox, "workspace")
        guidance = toolbox.call("list_files", {"path": "pkg"})["guidance"]
        self.assertEqual([(g["path"], g["content"]) for g in guidance], [("pkg/GEMINI.md", "Real.\n")])

    def test_apply_patch_paths_trigger_guidance(self):
        self.write(self.root / "services" / "payments" / "AGENTS.md", "Payments rule.\n")
        patch_text = ("*** Begin Patch\r\n*** Update File: services/payments/x.py\r\n@@\r\n-a\r\n+b\r\n"
                      "*** Add File: docs/new.md\n+hi\n*** Update File: old/name.py\n*** Move to: services/moved.py\n"
                      "*** Delete File: ../escape.py\n*** End Patch\n")
        self.assertEqual(instructions._touched("apply_patch", {"input": patch_text}),
                         [("services", "payments"), ("docs",), ("old",), ("services",)])
        store, session = self.open(self.root)
        toolbox = Toolbox(Workspace(self.root), store, Policy(), session)
        instructions.attach_jit(toolbox, "all")
        hook = toolbox.after_call[0]
        result = hook("apply_patch", {"input": patch_text}, {"changed": ["services/payments/x.py"]})
        self.assertEqual([g["path"] for g in result["guidance"]], ["services/payments/AGENTS.md"])
        self.assertNotIn("guidance", hook("apply_patch", {"input": patch_text}, {}))
        if "apply_patch" in toolbox.registry:  # once the apply-patch feature has merged
            self.assertTrue(toolbox.mutating("apply_patch"))


class ConfigTests(Base):
    def test_limits_resolve_the_instruction_mode(self):
        parser = build_parser()
        self.assertEqual(limits_from(parser.parse_args(["eval"])).instructions, "workspace")
        self.assertEqual(limits_from(parser.parse_args(["run", "task"])).instructions, "all")
        self.assertEqual(limits_from(parser.parse_args(["eval", "--instructions", "all"])).instructions, "all")
        self.assertEqual(limits_from(parser.parse_args(["chat", "--instructions", "none"])).instructions, "none")

    def test_eval_requests_never_include_the_global_file(self):
        self.write(self.config / "eira" / "AGENTS.md", "GLOBAL-MARKER-7f3a\n")
        workspace = self.tmp / "ws"
        workspace.mkdir()
        self.assertIn("GLOBAL-MARKER-7f3a", instructions.render(instructions.discover(Workspace(workspace), "all")))
        (workspace / "suite.json").write_text(json.dumps({"name": "mini", "tasks": [STARTER_SUITE["tasks"][0]]}))
        seen = []

        class Recording:
            model = "recording"

            def complete(self, messages, tools):
                seen.append(json.dumps(messages))
                return {"role": "assistant", "content": "Done."}, {"total_tokens": 1}

        with patch("eira_harness.cli.build_provider", return_value=Recording()), \
                redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
            main(["eval", "suite.json", "--workspace", str(workspace), "--provider", "ollama", "--model", "m", "--json"])
        self.assertTrue(seen)
        self.assertFalse(any("GLOBAL-MARKER-7f3a" in request for request in seen))

    def test_terminal_shows_jit_loads_and_lists_the_command(self):
        from eira_harness.terminal import Terminal
        out = io.StringIO()
        ui = Terminal(out)
        ui.emit({"event": "guidance_loaded", "via": "prefix", "path": "AGENTS.md"})
        self.assertEqual(out.getvalue(), "")
        ui.emit({"event": "guidance_loaded", "via": "jit", "path": "pkg/AGENTS.md"})
        self.assertIn("instructions loaded from pkg/AGENTS.md", out.getvalue())
        ui.help()
        self.assertIn("/instructions", out.getvalue())


if __name__ == "__main__":
    unittest.main()
