"""Workspace checkpoints and rewinds: offline and deterministic."""
from contextlib import redirect_stderr, redirect_stdout
import copy
import io
import json
import os
from pathlib import Path
import stat
import tempfile
import time
import unittest
from unittest import mock

from eira_harness import checkpoints as checkpoints_module
from eira_harness.agent import Agent, Limits
from eira_harness.checkpoints import Checkpoints, SnapshotSkipped
from eira_harness.cli import build_parser, limits_from, main
from eira_harness.security import HarnessError, Redactor, Workspace
from eira_harness.store import Store
from eira_harness.tools import Policy, Toolbox

try:
    from fakes import fake_docker
except ImportError:  # run as tests.test_checkpoints from the repository root
    from tests.fakes import fake_docker


def call(name, args, call_id):
    return {"role": "assistant", "content": None, "tool_calls": [{"id": call_id, "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)}}]}


def edit(path, old, new, call_id):
    return call("edit_file", {"path": path, "old_string": old, "new_string": new}, call_id)


def done(text="Done."):
    return {"role": "assistant", "content": text}


class Recorder:
    """Scripted provider that keeps a copy of every request."""
    model = "fixture"

    def __init__(self, responses):
        self.responses, self.requests = iter(responses), []

    def complete(self, messages, tools):
        self.requests.append(copy.deepcopy(messages))
        return next(self.responses), {"total_tokens": 1}


class CheckpointCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "work"
        self.root.mkdir()
        self.outside = Path(self.temp.name) / "outside"
        self.outside.mkdir()
        self.store = Store(self.root, Redactor())
        self.session = self.store.create("checkpoints")
        self.workspace = Workspace(self.root)
        self.ck = Checkpoints(self.store, self.workspace)
        self.events = []

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def write(self, relative, data, mode=None, age=None):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data if isinstance(data, bytes) else data.encode())
        if mode is not None:
            path.chmod(mode)
        if age is not None:
            past = time.time() - age
            os.utime(path, (past, past))
        return path

    def agent(self, responses, **policy):
        provider = Recorder(responses)
        toolbox = Toolbox(self.workspace, self.store, Policy(approve=lambda name, detail: True, **policy), self.session)
        return Agent(provider, self.store, toolbox, self.events.append), provider

    def kinds(self, kind):
        return [event for event in self.events if event["event"] == kind]

    def objects(self):
        base = self.root / ".eira" / "history" / "objects"
        return {p.parent.name + p.name for p in base.glob("*/*") if not p.name.startswith(".")}

    def cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err), mock.patch("sys.stdin", io.StringIO("")):
            code = main([*argv, "--workspace", str(self.root)])
        return code, out.getvalue(), err.getvalue()


class SnapshotTests(CheckpointCase):
    def test_round_trip_restores_bytes_and_modes_and_leaves_protected_paths(self):
        self.write("a.txt", "alpha\n")
        self.write("src/b.py", "print('b')\n")
        binary = bytes(range(256)) * 4
        self.write("blob.bin", binary)
        self.write("run.sh", "#!/bin/sh\necho hi\n", mode=0o755)
        self.write(".env", "TOKEN=one\n")
        self.write(".git/config", "[core]\n")
        self.write("node_modules/x.js", "one\n")
        (self.root / "link").symlink_to("a.txt")
        row = self.ck.snapshot(self.session)
        manifest = self.ck.manifest(row["manifest"])
        self.assertEqual(sorted(manifest["entries"]), ["a.txt", "blob.bin", "run.sh", "src/b.py"])
        self.assertEqual(manifest["skipped"]["symlinks"], 1)
        self.assertEqual(manifest["skipped"]["heavy_dirs"], 1)
        self.assertGreaterEqual(manifest["skipped"]["protected"], 2)
        history = self.root / ".eira" / "history"
        self.assertEqual(stat.S_IMODE(history.stat().st_mode), 0o700)
        self.assertTrue(all(stat.S_IMODE(p.stat().st_mode) == 0o600 for p in history.glob("objects/*/*")))

        # Direct writes stand in for shell side effects.
        self.write("a.txt", "changed\n")
        (self.root / "src" / "b.py").unlink()
        self.write("blob.bin", b"\x00broken")
        (self.root / "run.sh").chmod(0o644)
        self.write("new.txt", "created\n")
        self.write("src/new/deep.txt", "created\n")
        self.write(".env", "TOKEN=two\n")
        self.write(".git/config", "[changed]\n")
        self.write("node_modules/x.js", "two\n")

        plan = self.ck.plan(self.session, row["id"], "code")
        self.assertEqual((plan["restore"], plan["recreate"], plan["delete"]),
                         (["a.txt", "blob.bin", "run.sh"], ["src/b.py"], ["new.txt", "src/new/deep.txt"]))
        summary = Checkpoints.summary(plan)
        self.assertIn("-changed", summary)
        self.assertIn("+alpha", summary)
        self.assertIn("Binary or large file blob.bin differs", summary)
        seen = []
        result = self.ck.rewind(self.session, row["id"], "code", lambda text: seen.append(text) or True)
        self.assertEqual(seen, [summary])
        self.assertEqual((result["restored"], result["deleted"]), (4, 2))
        self.assertEqual((self.root / "a.txt").read_text(), "alpha\n")
        self.assertEqual((self.root / "src" / "b.py").read_text(), "print('b')\n")
        self.assertEqual((self.root / "blob.bin").read_bytes(), binary)
        self.assertEqual(stat.S_IMODE((self.root / "run.sh").stat().st_mode), 0o755)
        self.assertFalse((self.root / "new.txt").exists())
        self.assertFalse((self.root / "src" / "new" / "deep.txt").exists())
        self.assertTrue((self.root / "src" / "new").is_dir())  # directories are never removed
        self.assertEqual((self.root / ".env").read_text(), "TOKEN=two\n")
        self.assertEqual((self.root / ".git" / "config").read_text(), "[changed]\n")
        self.assertEqual((self.root / "node_modules" / "x.js").read_text(), "two\n")
        self.assertTrue((self.root / "link").is_symlink())
        self.assertEqual(os.readlink(self.root / "link"), "a.txt")
        events = self.store.events(self.session)
        self.assertEqual(events[-1]["kind"], "rewind_completed")
        self.assertEqual(events[-1]["payload"], {"checkpoint": row["id"], "mode": "code", "restored": 4,
                                                 "deleted": 2, "hidden_messages": 0})
        self.assertEqual(self.ck.plan(self.session, row["id"], "code")["restore"], [])

    def test_dedup_and_incremental_reuse(self):
        for index in range(6):
            self.write(f"f{index}.txt", f"file {index}\n", age=100)
        self.write("same-a.txt", "duplicate\n", age=100)
        self.write("same-b.txt", "duplicate\n", age=100)
        first = self.ck.snapshot(self.session)
        before = self.objects()
        self.assertEqual(len(before), 6 + 1 + 1)  # identical content is stored once, plus the manifest
        self.write("f0.txt", "file 0 changed\n")
        opened = []
        real_open = open

        def counting(path, *args, **kwargs):
            opened.append(str(path))
            return real_open(path, *args, **kwargs)
        with mock.patch.object(checkpoints_module, "open", counting, create=True):
            second = self.ck.snapshot(self.session)
        self.assertEqual(opened, [str(self.root / "f0.txt")])
        added = self.objects() - before
        self.assertEqual(added - {second["manifest"]}, {self.ck.manifest(second["manifest"])["entries"]["f0.txt"]["sha256"]})
        self.assertEqual(len(added), 2)
        self.assertNotEqual(first["manifest"], second["manifest"])
        # An unchanged workspace yields the same manifest object.
        with mock.patch.object(checkpoints_module, "open", counting, create=True):
            opened.clear()
            third = self.ck._snapshot(self.session, "step", "again")
        self.assertEqual(third["manifest"], second["manifest"])

    def test_file_cap_skips_without_a_manifest_row(self):
        for index in range(10):
            self.write(f"f{index}.txt", "x")
        toolbox = Toolbox(self.workspace, self.store, Policy(), self.session)

        def event(kind, **payload):  # journals like Agent.event
            self.store.event(self.session, kind, payload)
            self.events.append((kind, payload))
        seq = self.store.append(self.session, {"role": "user", "content": "task"})
        turn = self.ck.begin_turn(self.session, "task", seq, event)
        with mock.patch.object(checkpoints_module, "MAX_FILES", 5):
            result = self.ck.before_batch(toolbox, edit("f0.txt", "x", "y", "c1")["tool_calls"], seq + 1, event)
        self.assertIsNone(result)
        self.assertEqual(self.events, [("checkpoint_skipped", {"reason": "file_limit", "checkpoint": turn})])
        self.assertTrue(all(row["manifest"] is None for row in self.store.checkpoints(self.session)))
        with self.assertRaisesRegex(HarnessError, "not taken"):
            self.ck.plan(self.session, "turn:1", "code")
        with mock.patch.object(checkpoints_module, "MAX_FILES", 5), self.assertRaises(SnapshotSkipped):
            self.ck.snapshot(self.session)

    def test_retention_keeps_newest_and_sweeps_unreachable_objects(self):
        rows = []
        with mock.patch.object(checkpoints_module, "KEEP_PER_SESSION", 3):
            for index in range(5):
                self.write("only.txt", f"version {index}\n")
                rows.append(self.ck.snapshot(self.session))
        kept = [row["id"] for row in self.store.checkpoints(self.session)]
        self.assertEqual(kept, [row["id"] for row in rows[-3:]])
        live = set()
        for row in rows[-3:]:
            live |= {row["manifest"], self.ck.manifest(row["manifest"])["entries"]["only.txt"]["sha256"]}
        self.assertEqual(self.objects(), live)

    def test_size_cap_drops_oldest_but_keeps_newest(self):
        rows = []
        for index in range(4):
            self.write("big.txt", f"{index}" * 1000)
            rows.append(self.ck.snapshot(self.session))
        with mock.patch.object(checkpoints_module, "STORE_CAP_BYTES", 1500):
            self.write("big.txt", "z" * 1000)
            newest = self.ck.snapshot(self.session)
        self.assertEqual([row["id"] for row in self.store.checkpoints(self.session)], [newest["id"]])
        self.assertEqual(len(self.objects()), 2)


class SafetyTests(CheckpointCase):
    def test_symlinked_history_is_refused(self):
        (self.root / ".eira" / "history").symlink_to(self.outside, target_is_directory=True)
        self.write("a.txt", "a")
        with self.assertRaisesRegex(HarnessError, "symlink"):
            self.ck.snapshot(self.session)
        self.assertEqual(list(self.outside.iterdir()), [])
        agent, _ = self.agent([edit("a.txt", "a", "b", "c1"), done()])
        agent.run("Edit a")
        self.assertEqual((self.root / "a.txt").read_text(), "b")
        self.assertIn("symlink", self.kinds("checkpoint_failed")[0]["reason"])
        self.assertEqual(list(self.outside.iterdir()), [])

    def test_restore_never_writes_through_a_new_symlink(self):
        self.write("dir/sub.txt", "inside\n")
        self.write("top.txt", "top\n")
        row = self.ck.snapshot(self.session)
        (self.root / "dir" / "sub.txt").unlink()
        (self.root / "dir").rmdir()
        (self.root / "dir").symlink_to(self.outside, target_is_directory=True)
        self.write("top.txt", "changed\n")
        with self.assertRaisesRegex(HarnessError, "Symlink access is blocked"):
            self.ck.rewind(self.session, row["id"], "code", lambda text: True)
        self.assertEqual((self.root / "top.txt").read_text(), "changed\n")
        self.assertEqual(list(self.outside.iterdir()), [])
        # Even if planning had passed, the restore itself revalidates before any write.
        plan = {"source": row["id"], "checkpoint": row["id"], "manifest": row["manifest"],
                "restore": ["top.txt"], "recreate": ["dir/sub.txt"], "delete": []}
        with self.assertRaisesRegex(HarnessError, "Symlink access is blocked"):
            self.ck._restore(self.session, plan)
        self.assertEqual((self.root / "top.txt").read_text(), "changed\n")
        self.assertEqual(list(self.outside.iterdir()), [])

    def test_changes_during_confirmation_abort_the_restore(self):
        self.write("a.txt", "one\n")
        row = self.ck.snapshot(self.session)
        self.write("a.txt", "two\n")

        def confirm(text):
            self.write("late.txt", "appeared while waiting\n")
            return True
        with self.assertRaisesRegex(HarnessError, "changed while the rewind waited"):
            self.ck.rewind(self.session, row["id"], "code", confirm)
        self.assertEqual((self.root / "a.txt").read_text(), "two\n")
        self.assertTrue((self.root / "late.txt").exists())

    def test_declined_rewind_changes_nothing(self):
        self.write("a.txt", "one\n")
        row = self.ck.snapshot(self.session)
        self.write("a.txt", "two\n")
        self.assertIsNone(self.ck.rewind(self.session, row["id"], "code", lambda text: False))
        self.assertEqual((self.root / "a.txt").read_text(), "two\n")
        self.assertEqual([r["kind"] for r in self.store.checkpoints(self.session)], ["step"])


class AgentIntegrationTests(CheckpointCase):
    def test_snapshots_only_before_mutating_batches(self):
        self.write("app.py", "x = 1\n")
        log = Path(self.temp.name) / "docker.jsonl"
        agent, _ = self.agent([call("read_file", {"path": "app.py"}, "r1"),
                               edit("app.py", "x = 1", "x = 2", "e1"),
                               call("shell", {"command": "printf made > made.txt"}, "s1"),
                               done()], shell_mode="docker")
        with fake_docker(log):
            self.assertEqual(agent.run("Change x")["status"], "completed")
        order = [(e["event"], e.get("name") or e.get("type")) for e in self.events
                 if e["event"] in {"tool_started", "checkpoint_created"}]
        self.assertEqual(order, [("tool_started", "read_file"), ("checkpoint_created", "turn"),
                                 ("tool_started", "edit_file"), ("checkpoint_created", "step"),
                                 ("tool_started", "shell")])
        turn, step = self.store.checkpoints(self.session)
        self.assertEqual((turn["kind"], turn["label"], step["kind"]), ("turn", "Change x", "step"))
        rows = self.store.message_rows(self.session)
        self.assertEqual(rows[turn["user_seq"] - rows[0][0]][1]["content"], "Change x")
        shell_seq = next(seq for seq, m in rows if m.get("tool_calls") and m["tool_calls"][0]["id"] == "s1")
        self.assertEqual(step["message_seq"], shell_seq)
        turn_manifest = self.ck.manifest(turn["manifest"])["entries"]
        step_manifest = self.ck.manifest(step["manifest"])["entries"]
        self.assertEqual(self.ck._get(turn_manifest["app.py"]["sha256"]), b"x = 1\n")
        self.assertEqual(self.ck._get(step_manifest["app.py"]["sha256"]), b"x = 2\n")
        self.assertNotIn("made.txt", step_manifest)
        self.assertEqual((self.root / "made.txt").read_text(), "made")
        # Shell side effects are recoverable.
        result = self.ck.rewind(self.session, step["id"], "code", lambda text: True)
        self.assertEqual((result["restored"], result["deleted"]), (0, 1))
        self.assertFalse((self.root / "made.txt").exists())

    def test_read_only_batches_and_disabled_checkpoints_take_no_snapshot(self):
        self.write("app.py", "x = 1\n")
        agent, _ = self.agent([call("read_file", {"path": "app.py"}, "r1"), done()])
        agent.run("Look")
        self.assertEqual([r["manifest"] for r in self.store.checkpoints(self.session)], [None])
        provider = Recorder([edit("app.py", "x = 1", "x = 3", "e1"), done()])
        toolbox = Toolbox(self.workspace, self.store, Policy(approve=lambda n, d: True), self.session)
        agent = Agent(provider, self.store, toolbox, self.events.append, Limits(checkpoints=False))
        self.assertIsNone(agent.checkpoints)
        agent.run("Edit without checkpoints")
        self.assertEqual(len(self.store.checkpoints(self.session)), 1)
        self.assertFalse(limits_from(build_parser().parse_args(["run", "x", "--no-checkpoints"])).checkpoints)
        self.assertTrue(limits_from(build_parser().parse_args(["run", "x"])).checkpoints)

    def test_snapshot_failure_is_reported_and_the_edit_still_applies(self):
        self.write("app.py", "x = 1\n")
        agent, _ = self.agent([edit("app.py", "x = 1", "x = 2", "e1"), done()])
        with mock.patch.object(Checkpoints, "scan", side_effect=OSError("disk full")):
            self.assertEqual(agent.run("Edit")["status"], "completed")
        self.assertEqual((self.root / "app.py").read_text(), "x = 2\n")
        failed = self.kinds("checkpoint_failed")
        self.assertEqual(len(failed), 1)
        self.assertIn("disk full", failed[0]["reason"])
        self.assertEqual([e["kind"] for e in self.store.events(self.session)].count("checkpoint_failed"), 1)


class ConversationRewindTests(CheckpointCase):
    def two_tasks(self):
        self.write("app.py", "v = 0\n")
        agent, first = self.agent([edit("app.py", "v = 0", "v = 1", "e1"), done("one")])
        agent.run("Task one")
        agent, second = self.agent([edit("app.py", "v = 1", "v = 2", "e2"), done("two")])
        agent.run("Task two")
        return first, second

    def test_conversation_rewind_keeps_the_request_prefix(self):
        _, second = self.two_tasks()
        original = self.store.messages(self.session)
        code, _, err = self.cli("rewind", "turn:2", "--conversation", "--yes", "--session", self.session)
        self.assertEqual(code, 0, err)
        self.assertIn("Original prompt:\nTask two", err)
        self.assertEqual((self.root / "app.py").read_text(), "v = 2\n")  # code untouched
        agent, third = self.agent([done("three")])
        agent.run("Task three")
        before, after = second.requests[0], third.requests[0]
        self.assertEqual(before[-1], {"role": "user", "content": "Task two"})
        self.assertEqual(after[:-1], before[:-1])
        self.assertEqual(after[-1], {"role": "user", "content": "Task three"})
        journal = self.store.messages(self.session)
        self.assertEqual(journal[:len(original)], original)
        markers = [m for m in journal if m["role"] == "marker"]
        self.assertEqual(len(markers), 1)
        self.assertEqual(markers[0]["eira_rewind"]["to_seq"], self.store.checkpoints(self.session)[1]["user_seq"])
        event = self.store.events(self.session)
        rewind = next(e for e in event if e["kind"] == "rewind_completed")
        self.assertEqual(rewind["payload"]["hidden_messages"], 4)  # prompt, edit call, its result, reply

    def test_both_restores_files_and_backup_undoes_it(self):
        self.two_tasks()
        code, _, err = self.cli("rewind", "turn:2", "--both", "--yes", "--session", self.session)
        self.assertEqual(code, 0, err)
        self.assertEqual((self.root / "app.py").read_text(), "v = 1\n")
        view = self.store.model_messages(self.session)
        self.assertNotIn("Task two", [m.get("content") for m in view])
        self.assertIn("Task one", [m.get("content") for m in view])
        backups = [r for r in self.store.checkpoints(self.session) if r["kind"] == "rewind-backup"]
        self.assertEqual(len(backups), 1)
        self.assertIn(f"eira rewind {backups[0]['id']} --code", err)
        code, _, err = self.cli("rewind", backups[0]["id"], "--code", "--yes", "--session", self.session)
        self.assertEqual(code, 0, err)
        self.assertEqual((self.root / "app.py").read_text(), "v = 2\n")
        code, _, err = self.cli("rewind", backups[0]["id"], "--both", "--yes", "--session", self.session)
        self.assertEqual(code, 2)
        self.assertIn("choose a turn checkpoint", err)

    def test_turn_without_edits_uses_the_next_checkpoint(self):
        self.write("app.py", "v = 0\n")
        agent, _ = self.agent([done("nothing to do")])
        agent.run("Just talk")
        self.write("app.py", "v = 1\n")
        agent, _ = self.agent([edit("app.py", "v = 1", "v = 2", "e1"), done()])
        agent.run("Edit")
        first, second = self.store.checkpoints(self.session)
        self.assertIsNone(first["manifest"])
        plan = self.ck.plan(self.session, "turn:1", "code")
        self.assertEqual(plan["source"], second["id"])
        self.assertIn("changed no files", Checkpoints.summary(plan))
        self.assertIn(second["id"], Checkpoints.summary(plan))
        self.ck.rewind(self.session, "turn:1", "code", lambda text: True)
        self.assertEqual((self.root / "app.py").read_text(), "v = 1\n")

    def test_step_checkpoints_accept_only_code(self):
        self.write("a.txt", "a")
        row = self.ck.snapshot(self.session)
        for mode in ("conversation", "both"):
            with self.subTest(mode=mode), self.assertRaisesRegex(HarnessError, "choose a turn checkpoint"):
                self.ck.rewind(self.session, row["id"], mode, lambda text: True)
        self.assertNotIn("marker", [m["role"] for m in self.store.messages(self.session)])

    def test_replay_handles_compaction_and_nested_rewinds(self):
        def add(role, content, **extra):
            return self.store.append(self.session, {"role": role, "content": content, **extra})
        u1 = add("user", "p1")
        add("assistant", "a1")
        u2 = add("user", "p2")
        add("assistant", "a2")
        add("user", "summary", eira_compaction={"replaced_messages": 4})
        u3 = add("user", "p3")
        add("assistant", "a3")
        contents = lambda: [m["content"] for m in self.store.model_messages(self.session)]
        self.assertEqual(contents(), ["summary", "p3", "a3"])
        self.store.append_rewind(self.session, u3, "ck-0000000003")
        self.assertEqual(contents(), ["summary"])
        self.store.append_rewind(self.session, u2, "ck-0000000002")  # crosses the compaction
        self.assertEqual(contents(), ["p1", "a1"])
        add("user", "p4")
        self.assertEqual(contents(), ["p1", "a1", "p4"])
        self.store.append_rewind(self.session, u3, "ck-0000000003")  # the view before u3, as it stood then
        self.assertEqual(contents(), ["summary"])
        self.store.append_rewind(self.session, u1, "ck-0000000001")
        self.assertEqual(contents(), [])
        with self.assertRaises(HarnessError):
            self.store.append_rewind(self.session, 10_000, "ck-0000000009")
        self.assertEqual(len([m for m in self.store.messages(self.session) if m["role"] == "marker"]), 4)

    def test_rewind_to_before_a_compaction_restores_the_pre_compaction_view(self):
        self.write("app.py", "v = 0\n")
        agent, _ = self.agent([edit("app.py", "v = 0", "v = 1", "e1"), done("one")])
        agent.run("Task one")
        pre = self.store.model_messages(self.session)
        agent, _ = self.agent([done("two")])
        agent.run("Task two")
        self.store.append(self.session, {"role": "user", "content": "[summary]", "eira_compaction": {"replaced_messages": 6}})
        self.assertEqual(len(self.store.model_messages(self.session)), 1)
        self.ck.rewind(self.session, "turn:2", "conversation", lambda text: True)
        self.assertEqual(self.store.model_messages(self.session), pre)

    def test_recovery_ignores_markers_and_calls_hidden_by_a_rewind(self):
        self.write("app.py", "v = 0\n")
        agent, _ = self.agent([done("one")])
        agent.run("Task one")
        agent, _ = self.agent([done("two")])
        agent.run("Task two")
        # Simulate a crash mid-tool in task two, then rewind it away.
        self.store.append(self.session, call("read_file", {"path": "app.py"}, "lost"))
        self.ck.rewind(self.session, "turn:2", "conversation", lambda text: True)
        agent, provider = self.agent([done("three")])
        agent.run("Task three")
        self.assertEqual([m["role"] for m in provider.requests[0]], ["system", "user", "assistant", "user"])
        self.assertEqual(self.kinds("recovered_tool"), [])


class CliTests(CheckpointCase):
    def test_checkpoints_listing_diff_and_confirmation(self):
        self.write("app.py", "v = 0\n")
        agent, _ = self.agent([edit("app.py", "v = 0", "v = 1", "e1"), done()])
        agent.run("Task one")
        agent, _ = self.agent([done("chat only")])
        agent.run("Task two")
        code, out, _ = self.cli("checkpoints", "--json")
        self.assertEqual(code, 0)
        rows = json.loads(out)
        self.assertEqual([(r["kind"], r["label"], r["turn"], r["files"]) for r in rows],
                         [("turn", "Task one", 1, 1), ("turn", "Task two", 2, 0)])
        self.assertEqual(rows[0]["bytes"], len("v = 0\n"))
        self.write("added.txt", "new\n")
        code, out, _ = self.cli("diff", "--stat")
        self.assertEqual(code, 0)
        self.assertIn(f"Changes since {rows[0]['id']}", out)
        self.assertIn("M  app.py  +1 -1", out)
        self.assertIn("A  added.txt  +1 -0", out)
        self.assertIn("2 files changed", out)
        code, out, _ = self.cli("diff", rows[0]["id"])
        self.assertIn("-v = 0", out)
        self.assertIn("+v = 1", out)
        code, _, err = self.cli("rewind", "turn:1", "--code", "--session", self.session)
        self.assertEqual(code, 2)
        self.assertIn("needs confirmation", err)
        self.assertEqual((self.root / "app.py").read_text(), "v = 1\n")
        code, _, err = self.cli("rewind", "turn:9", "--code", "--yes")
        self.assertEqual(code, 2)
        self.assertIn("Turn 9 has no checkpoint", err)

    def test_chat_rewind_both_recalls_the_prompt(self):
        self.write("app.py", "v = 0\n")
        ui = mock.MagicMock()
        ui.history, ui._readline = [], mock.MagicMock()
        ui.prompt.side_effect = ["Bump v", "/checkpoints", "/diff", "/rewind 1", "/exit"]
        provider = Recorder([edit("app.py", "v = 0", "v = 1", "e1"), done()])
        home = Path(self.temp.name) / "home"
        with mock.patch.dict(os.environ, {"HOME": str(home), "XDG_CONFIG_HOME": str(home / "config")}), \
                mock.patch("sys.stdin.isatty", return_value=True), \
                mock.patch("eira_harness.terminal.Terminal", return_value=ui), \
                mock.patch("eira_harness.cli.build_provider", return_value=provider), \
                mock.patch("builtins.input", side_effect=["b", "y"]), redirect_stderr(io.StringIO()):
            self.assertEqual(main(["chat", "--workspace", str(self.root), "--provider", "ollama",
                                   "--model", "fixture", "--approve-writes", "--session", self.session]), 0)
        self.assertEqual((self.root / "app.py").read_text(), "v = 0\n")
        notices = "\n".join(str(c.args[0]) for c in ui.notice.call_args_list)
        self.assertIn("turn 1", notices)
        self.assertIn("+v = 1", notices)
        self.assertIn("Rewind code and conversation", notices)
        self.assertIn("Original prompt (press Up to edit and resend):\nBump v", notices)
        self.assertEqual(ui.history, ["Bump v"])
        ui._readline.add_history.assert_called_once_with("Bump v")
        self.assertIn("rewind_completed", [c.args[0]["event"] for c in ui.emit.call_args_list])
        self.assertEqual(self.store.model_messages(self.session), [])
        ui.error.assert_not_called()

    def test_empty_workspace_lists_nothing(self):
        self.assertEqual(self.cli("checkpoints", "--json")[:2], (0, "[]\n"))
        self.assertEqual(self.cli("diff")[0], 2)


if __name__ == "__main__":
    unittest.main()
