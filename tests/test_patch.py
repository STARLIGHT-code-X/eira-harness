"""apply_patch: Codex-format parsing, tolerant matching, one approval, all-or-nothing commits."""
from contextlib import redirect_stdout
import errno
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch as mock_patch

from eira_harness import patch
from eira_harness.agent import Agent
from eira_harness.cli import main
from eira_harness.security import HarnessError, Redactor, Workspace
from eira_harness.store import Store
from eira_harness.tools import Policy, Toolbox

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "apply_patch"
VALID = "Valid hunk headers: '*** Add File: {path}', '*** Delete File: {path}', '*** Update File: {path}'"
UNEXPECTED = ("Unexpected line found in update hunk: '{}'. Every line should start with ' ' (context line), "
              "'+' (added line), or '-' (removed line)")

# Scenario name -> substring the error must contain (None: the patch must succeed).
SCENARIO_ERRORS = {
    "add_file": None,
    "multiple_operations": None,
    "multiple_chunks": None,
    "move_to_new_directory": None,
    "empty_patch": "No files were modified.",
    "missing_context": "Failed to find expected lines in notes.txt:\nthis line is not in the file",
    "delete_missing_file": "Failed to delete file absent.txt",
    "empty_update_hunk": "invalid hunk at line 2, Update file hunk for path 'notes.txt' is empty",
    "update_missing_file": "Failed to read file to update absent.txt",
    "delete_directory_fails": "Failed to delete file folder: it is a directory",
    "invalid_hunk_header": f"invalid hunk at line 2, '*** Frobnicate File: notes.txt' is not a valid hunk header. {VALID}",
    "trailing_newline_appended": None,
    "pure_addition_appends": None,
    "whitespace_padded_markers": None,
    "unicode_punctuation_fuzz": None,
    "crlf_preserved": None,
    "mixed_line_endings": None,
    "end_of_file_anchor": None,
    "deletion_only_update": None,
    "add_existing_fails": "Add File target already exists: exists.txt. Use *** Update File, or Delete File and Add File in one patch.",
    "move_onto_existing_fails": "Move destination already exists: target.txt",
    "failure_after_partial_success_rolls_back": "Failed to read file to update missing.txt",
}


def snapshot(root: Path) -> dict:
    if not root.is_dir():
        return {}
    return {path.relative_to(root).as_posix(): path.read_bytes() for path in sorted(root.rglob("*"))
            if path.is_file() and ".eira" not in path.relative_to(root).parts}


def wrap(body: str) -> str:
    return f"*** Begin Patch\n{body}\n*** End Patch"


class FakeProvider:
    model = "fake"

    def complete(self, messages, tools):
        raise AssertionError("not used")


class PatchTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root, Redactor(extra=("fixture-secret-value",)))
        self.session = self.store.create("patch")
        self.asked = []
        self.answer = True
        self.policy = Policy(approve=lambda name, detail: self.asked.append((name, detail)) or self.answer)
        self.tools = Toolbox(Workspace(self.root), self.store, self.policy, self.session)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def write(self, rel, data, mode=None):
        target = self.root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data.encode() if isinstance(data, str) else data)
        if mode is not None:
            os.chmod(target, mode)
        return target

    def apply(self, text):
        return self.tools.call("apply_patch", {"input": text})

    def fails(self, text, contains):
        before = snapshot(self.root)
        with self.assertRaises(HarnessError) as caught:
            self.apply(text)
        self.assertIn(contains, str(caught.exception))
        self.assertEqual(snapshot(self.root), before)
        return str(caught.exception)

    def patches_dir(self):
        return self.root / ".eira" / "patches"


class GoldenScenarioTests(unittest.TestCase):
    def test_every_scenario_is_listed(self):
        self.assertEqual(sorted(p.name for p in FIXTURES.iterdir() if p.is_dir()), sorted(SCENARIO_ERRORS))

    def test_scenarios(self):
        for name, error in sorted(SCENARIO_ERRORS.items()):
            with self.subTest(scenario=name), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                for rel, data in snapshot(FIXTURES / name / "input").items():
                    (root / rel).parent.mkdir(parents=True, exist_ok=True)
                    (root / rel).write_bytes(data)
                store = Store(root)
                try:
                    tools = Toolbox(Workspace(root), store, Policy(approve_writes=True), store.create("golden"))
                    text = (FIXTURES / name / "patch.txt").read_bytes().decode()
                    if error is None:
                        result = tools.call("apply_patch", {"input": text})
                        self.assertTrue(result["summary"].startswith("Success. Updated the following files:"))
                        if name == "unicode_punctuation_fuzz":
                            self.assertEqual(result["fuzz"], [{"path": "quotes.txt", "hunk": 1, "pass": "unicode"}])
                    else:
                        with self.assertRaises(HarnessError) as caught:
                            tools.call("apply_patch", {"input": text})
                        self.assertIn(error, str(caught.exception))
                        self.assertTrue(str(caught.exception).startswith(patch.VERIFY))
                    self.assertEqual(snapshot(root), snapshot(FIXTURES / name / "expected"))
                    self.assertEqual(os.listdir(root / ".eira" / "patches") if (root / ".eira" / "patches").exists() else [], [])
                finally:
                    store.close()


class ParserTests(unittest.TestCase):
    def error(self, text):
        with self.assertRaises(HarnessError) as caught:
            patch.parse(text)
        return str(caught.exception)

    def test_exact_error_strings(self):
        self.assertEqual(self.error("bad"), "invalid patch: The first line of the patch must be '*** Begin Patch'")
        self.assertEqual(self.error("*** Begin Patch\n*** Add File: a\n+x"),
                         "invalid patch: The last line of the patch must be '*** End Patch'")
        self.assertEqual(self.error(wrap("*** Add File: a\n+x\n*** Rename File: b")),
                         f"invalid hunk at line 4, '*** Rename File: b' is not a valid hunk header. {VALID}")
        self.assertEqual(self.error(wrap("*** Update File: a\n@@\n context\nbogus")),
                         "invalid hunk at line 5, Expected update hunk to start with a @@ context marker, got: 'bogus'")
        self.assertEqual(self.error(wrap("*** Update File: a\n@@\nbogus")),
                         "invalid hunk at line 4, " + UNEXPECTED.format("bogus"))
        self.assertEqual(self.error(wrap("*** Update File: a\n@@\n-x\n+y\n*** End of File\n context")),
                         "invalid hunk at line 7, Expected update hunk to start with a @@ context marker, got: ' context'")
        self.assertEqual(self.error(wrap("*** Environment ID: remote\n*** Add File: a\n+x")),
                         "invalid patch: Environment IDs are not supported")
        self.assertEqual(self.error(wrap("*** Update File: a\n@@\n*** End Patch")),
                         "invalid hunk at line 4, Update hunk does not contain any lines")
        self.assertEqual(self.error(wrap("*** Update File: a\n*** Move to: b")),
                         "invalid hunk at line 2, Update file hunk for path 'a' is empty")

    def test_structure(self):
        hunks = patch.parse("<<'EOF'\n*** Begin Patch\n*** Update File: a.py\n*** Move to: b.py\n@@ def f():\n x\n-y\n+z\n"
                            "\n@@\n+tail\n*** End of File\n\n*** Add File: empty.txt\n*** Delete File: gone\n*** End Patch\nEOF\n")
        self.assertEqual([(h.kind, h.path) for h in hunks], [("update", "a.py"), ("add", "empty.txt"), ("delete", "gone")])
        update = hunks[0]
        self.assertEqual(update.move_path, "b.py")
        first, second = update.chunks
        self.assertEqual((first.change_context, first.old_lines, first.new_lines), ("def f():", ["x", "y", ""], ["x", "z", ""]))
        self.assertEqual(first.context_line_indices, [(0, 0), (2, 2)])
        self.assertTrue(second.is_end_of_file)
        self.assertEqual(hunks[1].contents, "")

    def test_limits(self):
        many = "\n".join(f"*** Add File: f{i}.txt\n+x" for i in range(101))
        self.assertIn("at most 100 file operations", self.error(wrap(many)))
        chunks = "\n".join("@@\n-a\n+b" for _ in range(2001))
        self.assertIn("at most 2,000 chunks", self.error(wrap("*** Update File: a\n" + chunks)))


class ResultTests(PatchTestCase):
    def test_three_operation_result(self):
        self.write("modify.txt", "alpha\nbeta\ngamma\n")
        self.write("delete.txt", "bye\n")
        result = self.apply((FIXTURES / "multiple_operations" / "patch.txt").read_text())
        self.assertEqual(result["summary"], "Success. Updated the following files:\nA nested/new.txt\nM modify.txt\nD delete.txt")
        for entry in result["files"]:
            if "sha256" in entry:
                on_disk = (self.root / entry.get("to", entry["path"])).read_bytes()
                self.assertEqual(entry["sha256"], hashlib.sha256(on_disk).hexdigest())
        self.assertEqual([e["op"] for e in result["files"]], ["add", "delete", "update"])
        self.assertEqual(result["files"][2]["first_changed_line"], 2)
        self.assertEqual((result["fuzz"], result["warnings"], result["checks"]), ([], [], []))
        self.assertEqual(os.listdir(self.patches_dir()), [])

    def test_move_result_and_sequenced_hunks(self):
        self.write("old.py", "x = 1\n", mode=0o750)
        result = self.apply(wrap("*** Update File: old.py\n*** Move to: new.py\n@@\n-x = 1\n+x = 2\n"
                                 "*** Add File: made.txt\n+one\n*** Update File: made.txt\n@@\n one\n+two"))
        self.assertFalse((self.root / "old.py").exists())
        self.assertEqual((self.root / "new.py").read_text(), "x = 2\n")
        self.assertEqual(stat.S_IMODE((self.root / "new.py").stat().st_mode), 0o750)
        self.assertEqual((self.root / "made.txt").read_text(), "one\ntwo\n")
        self.assertEqual(result["files"][0], {"path": "old.py", "op": "move", "to": "new.py",
                                              "sha256": hashlib.sha256(b"x = 2\n").hexdigest()})
        self.assertEqual(result["summary"], "Success. Updated the following files:\nA made.txt\nM new.py\nM made.txt")
        detail = self.asked[0][1]
        self.assertTrue(detail.startswith("apply_patch: 2 files (1 added, 0 modified, 0 deleted, 1 moved)\n"))
        self.assertIn("rename from old.py\nrename to new.py\n", detail)

    def test_delete_then_add_same_path_is_allowed(self):
        self.write("swap.txt", "old\n")
        self.apply(wrap("*** Delete File: swap.txt\n*** Add File: swap.txt\n+new"))
        self.assertEqual((self.root / "swap.txt").read_text(), "new\n")

    def test_describe(self):
        self.assertEqual(self.tools.describe("apply_patch", {"input": wrap("*** Add File: a.py\n+x\n*** Delete File: b.py")}),
                         "2 files: a.py, b.py")


class RollbackTests(PatchTestCase):
    def three_files(self):
        self.write("a.txt", "a1\n", 0o640)
        self.write("b.txt", "b1\n", 0o600)
        self.write("c.txt", "c1\n", 0o755)
        return wrap("*** Update File: a.txt\n@@\n-a1\n+a2\n*** Update File: b.txt\n@@\n-b1\n+b2\n"
                    "*** Update File: c.txt\n@@\n-c1\n+c2")

    def modes(self):
        return {name: stat.S_IMODE((self.root / name).stat().st_mode) for name in ("a.txt", "b.txt", "c.txt")}

    def test_failed_write_rolls_back_everything(self):
        text = self.three_files()
        before, modes = snapshot(self.root), self.modes()
        emitted = []
        Agent(FakeProvider(), self.store, self.tools, emit=emitted.append)
        real = os.replace
        target = str(self.root / "b.txt")

        def replace(src, dst, *args, **kwargs):
            if str(dst) == target:
                raise OSError(errno.ENOSPC, "No space left on device")
            return real(src, dst, *args, **kwargs)
        with mock_patch("eira_harness.patch.os.replace", side_effect=replace):
            with self.assertRaises(HarnessError) as caught:
                self.apply(text)
        message = str(caught.exception)
        self.assertEqual(message, "Patch failed while writing b.txt (ENOSPC); all changes were rolled back.")
        self.assertEqual(snapshot(self.root), before)
        self.assertEqual(self.modes(), modes)
        self.assertEqual(list(self.root.rglob(".eira-patch-*")), [])
        self.assertEqual(os.listdir(self.patches_dir()), [])
        events = [e for e in self.store.events(self.session) if e["kind"] == "patch_rolled_back"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["payload"]["path"], "b.txt")
        self.assertEqual(events[0]["payload"]["error"], "ENOSPC")
        self.assertEqual(len(events[0]["payload"]["patch_id"]), 12)
        self.assertTrue(any(e["event"] == "patch_rolled_back" for e in emitted))

    def test_rollback_restores_deletes_moves_and_removes_adds(self):
        self.write("gone.txt", "bye\n", 0o644)
        self.write("src/mover.txt", "move me\n", 0o600)
        self.write("z.txt", "z\n")
        before = snapshot(self.root)
        real = os.replace
        target = str(self.root / "z.txt")

        def replace(src, dst, *args, **kwargs):
            if str(dst) == target:
                raise OSError(errno.EIO, "I/O error")
            return real(src, dst, *args, **kwargs)
        with mock_patch("eira_harness.patch.os.replace", side_effect=replace):
            with self.assertRaises(HarnessError) as caught:
                self.apply(wrap("*** Add File: deep/new/added.txt\n+fresh\n*** Delete File: gone.txt\n"
                                "*** Update File: src/mover.txt\n*** Move to: dst/moved.txt\n@@\n-move me\n+moved\n"
                                "*** Update File: z.txt\n@@\n-z\n+zz"))
        self.assertIn("(EIO); all changes were rolled back.", str(caught.exception))
        self.assertEqual(snapshot(self.root), before)
        self.assertFalse((self.root / "deep").exists())
        self.assertFalse((self.root / "dst").exists())
        self.assertEqual(stat.S_IMODE((self.root / "src/mover.txt").stat().st_mode), 0o600)

    def test_cross_device_delete_is_restored_from_the_pre_image(self):
        self.write("gone.txt", "keep me\n", 0o640)
        self.write("z.txt", "z\n")
        real = os.replace

        def replace(src, dst, *args, **kwargs):
            if "trash" in Path(dst).parts:
                raise OSError(errno.EXDEV, "Invalid cross-device link")
            if str(dst) == str(self.root / "z.txt"):
                raise OSError(errno.ENOSPC, "No space left on device")
            return real(src, dst, *args, **kwargs)
        with mock_patch("eira_harness.patch.os.replace", side_effect=replace):
            with self.assertRaises(HarnessError):
                self.apply(wrap("*** Delete File: gone.txt\n*** Update File: z.txt\n@@\n-z\n+zz"))
        self.assertEqual((self.root / "gone.txt").read_text(), "keep me\n")
        self.assertEqual(stat.S_IMODE((self.root / "gone.txt").stat().st_mode), 0o640)
        self.assertEqual(os.listdir(self.patches_dir()), [])

    def test_failed_rollback_keeps_the_journal(self):
        text = self.three_files()
        emitted = []
        Agent(FakeProvider(), self.store, self.tools, emit=emitted.append)
        real = os.replace

        def replace(src, dst, *args, **kwargs):
            if str(dst) == str(self.root / "b.txt"):
                raise OSError(errno.ENOSPC, "No space left on device")
            return real(src, dst, *args, **kwargs)
        with mock_patch("eira_harness.patch.os.replace", side_effect=replace), \
                mock_patch("eira_harness.patch.atomic_write", side_effect=OSError(errno.EACCES, "denied")):
            with self.assertRaises(HarnessError) as caught:
                self.apply(text)
        (leftover,) = patch.leftovers(self.root / ".eira")
        self.assertEqual(leftover["state"], "rollback_failed")
        self.assertIn("rollback was incomplete", str(caught.exception))
        self.assertIn(leftover["directory"], str(caught.exception))
        self.assertEqual((self.root / "a.txt").read_text(), "a2\n")
        self.assertEqual((self.patches_dir() / leftover["id"] / "pre" / "0").read_text(), "a1\n")
        self.assertEqual(list(self.root.rglob(".eira-patch-*")), [])
        self.assertEqual([e["event"] for e in emitted if e["event"].startswith("patch_")], ["patch_rollback_failed"])

    def test_interrupted_commit_leaves_a_manifest_for_doctor(self):
        text = self.three_files()
        real, calls = os.replace, []

        def replace(src, dst, *args, **kwargs):
            if not str(dst).startswith(str(self.patches_dir())):
                calls.append(dst)
                if len(calls) == 2:
                    raise KeyboardInterrupt
            return real(src, dst, *args, **kwargs)
        with mock_patch("eira_harness.patch.os.replace", side_effect=replace):
            with self.assertRaises(KeyboardInterrupt):
                self.apply(text)
        (leftover,) = patch.leftovers(self.root / ".eira")
        self.assertEqual(leftover["state"], "committing")
        manifest = json.loads((self.patches_dir() / leftover["id"] / "manifest.json").read_text())
        self.assertEqual(manifest["state"], "committing")
        self.assertEqual([op["path"] for op in manifest["ops"]], ["a.txt", "b.txt", "c.txt"])
        self.assertEqual((self.patches_dir() / leftover["id"] / manifest["ops"][0]["pre_image"]).read_text(), "a1\n")
        self.assertEqual(stat.S_IMODE((self.patches_dir() / leftover["id"]).stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((self.patches_dir() / leftover["id"] / "pre" / "0").stat().st_mode), 0o600)
        out = io.StringIO()
        env = {"HOME": str(self.root / "home"), "XDG_CONFIG_HOME": str(self.root / "home" / "config"),
               "PATH": os.environ.get("PATH", "")}
        with mock_patch.dict(os.environ, env, clear=True), redirect_stdout(out):
            self.assertEqual(main(["doctor", "--workspace", str(self.root)]), 0)
        report = json.loads(out.getvalue())
        self.assertEqual([item["id"] for item in report["incomplete_patches"]], [leftover["id"]])

    def test_leftovers_is_empty_without_state(self):
        self.assertEqual(patch.leftovers(self.root / "missing"), [])


class ApprovalTests(PatchTestCase):
    def setUp(self):
        super().setUp()
        for name in ("one", "two", "three"):
            self.write(f"{name}.txt", f"{name}\n")
        self.text = wrap("*** Update File: one.txt\n@@\n-one\n+ONE\n*** Update File: two.txt\n@@\n-two\n+TWO\n"
                         "*** Update File: three.txt\n@@\n-three\n+THREE")

    def test_one_approval_with_every_diff_in_order(self):
        self.apply(self.text)
        self.assertEqual(len(self.asked), 1)
        name, detail = self.asked[0]
        self.assertEqual(name, "apply_patch")
        self.assertTrue(detail.startswith("apply_patch: 3 files (0 added, 3 modified, 0 deleted, 0 moved)\n"))
        positions = [detail.index(f"--- {n}.txt (before)") for n in ("one", "two", "three")]
        self.assertEqual(positions, sorted(positions))
        self.assertIn("-two\n+TWO\n", detail)

    def test_denial_changes_nothing(self):
        self.answer = False
        message = self.fails(self.text, "Action denied")
        self.assertNotIn(patch.VERIFY, message)

    def test_file_changed_during_approval_cancels_everything(self):
        def approve(name, detail):
            (self.root / "three.txt").write_text("changed by someone\n")
            return True
        self.policy.approve = approve
        with self.assertRaises(HarnessError) as caught:
            self.apply(self.text)
        self.assertEqual(str(caught.exception), "File changed during approval; patch cancelled. No files were modified.")
        self.assertEqual((self.root / "one.txt").read_text(), "one\n")
        self.assertEqual((self.root / "three.txt").read_text(), "changed by someone\n")

    def test_add_target_created_during_approval_cancels(self):
        def approve(name, detail):
            (self.root / "late.txt").write_text("raced\n")
            return True
        self.policy.approve = approve
        with self.assertRaises(HarnessError) as caught:
            self.apply(wrap("*** Add File: late.txt\n+mine"))
        self.assertIn("File changed during approval", str(caught.exception))
        self.assertEqual((self.root / "late.txt").read_text(), "raced\n")


class PolicyTests(PatchTestCase):
    def test_read_only_denies(self):
        self.write("a.txt", "a\n")
        self.policy.read_only = True
        self.fails(wrap("*** Update File: a.txt\n@@\n-a\n+b"), "read-only")
        self.assertEqual(self.asked, [])

    def test_approve_writes_and_review_paths(self):
        self.write("app.py", "x = 1\n")
        self.write("ci.yml", "on: push\n")
        self.policy.approve_writes = True
        self.apply(wrap("*** Update File: app.py\n@@\n-x = 1\n+x = 2"))
        self.assertEqual(self.asked, [])
        self.tools.review_paths = lambda path: path.endswith(".yml")
        self.apply(wrap("*** Update File: app.py\n@@\n-x = 2\n+x = 3\n*** Update File: ci.yml\n@@\n-on: push\n+on: pull_request"))
        self.assertEqual(len(self.asked), 1)
        self.assertEqual((self.root / "ci.yml").read_text(), "on: pull_request\n")

    def test_blocked_paths_fail_before_approval(self):
        self.write("x.txt", "x\n")
        for path in ["../x", ".git/config", ".env", "key.pem", "/etc/passwd"]:
            with self.subTest(path=path):
                self.fails(wrap(f"*** Add File: {path}\n+data"), patch.VERIFY)
        self.assertIn("Path is outside the workspace", self.fails(wrap("*** Add File: /etc/passwd\n+x"), "outside"))
        self.assertEqual(self.asked, [])

    def test_docker_and_absolute_workspace_paths_map_to_relative(self):
        self.write("src/a.py", "a = 1\n")
        result = self.apply(wrap("*** Update File: /workspace/src/a.py\n@@\n-a = 1\n+a = 2\n"
                                 f"*** Add File: {self.tools.workspace.root}/src/b.py\n+b = 1"))
        self.assertEqual([entry["path"] for entry in result["files"]], ["src/a.py", "src/b.py"])
        self.assertEqual((self.root / "src/a.py").read_text(), "a = 2\n")

    def test_symlink_targets_are_blocked(self):
        self.write("real.txt", "real\n")
        os.symlink(self.root / "real.txt", self.root / "link.txt")
        self.fails(wrap("*** Update File: link.txt\n@@\n-real\n+fake"), "Symlink")


class ProtectedContentTests(PatchTestCase):
    def test_file_with_secret_is_refused(self):
        self.write("settings.py", "TOKEN = 'fixture-secret-value'\nDEBUG = True\n")
        self.fails(wrap("*** Update File: settings.py\n@@\n-DEBUG = True\n+DEBUG = False"), "protected or redacted")
        self.fails(wrap("*** Delete File: settings.py"), "protected or redacted")

    def test_added_secret_or_placeholder_is_refused(self):
        self.write("a.txt", "a\n")
        for line in ["key = '[REDACTED]'", "key = 'fixture-secret-value'"]:
            with self.subTest(line=line):
                self.fails(wrap(f"*** Update File: a.txt\n@@\n a\n+{line}"), "protected or redacted")
                self.fails(wrap(f"*** Add File: b.txt\n+{line}"), "protected or redacted")
        self.assertEqual(self.asked, [])

    def test_binary_and_oversized_files_are_refused(self):
        self.write("bin.dat", b"a\x00b\n")
        self.fails(wrap("*** Update File: bin.dat\n@@\n-a\n+b"), "Binary")
        self.write("latin.txt", b"caf\xe9\n")
        self.fails(wrap("*** Delete File: latin.txt"), "not UTF-8")


class GuardTests(PatchTestCase):
    def test_guard_rejects_whole_patch_before_approval(self):
        self.write("a.py", "a = 1\n")
        self.write("b.py", "b = 1\n")
        seen = []

        def guard(path, old, new):
            seen.append(path)
            if path == "b.py":
                raise HarnessError("syntax error in b.py")
            return {"type": "probe", "path": path}
        self.tools.write_guards[:] = [guard]  # isolate from the built-in syntax guard
        self.fails(wrap("*** Update File: a.py\n@@\n-a = 1\n+a = 2\n*** Update File: b.py\n@@\n-b = 1\n+b = (\n"), "syntax error in b.py")
        self.assertEqual(seen, ["a.py", "b.py"])
        self.assertEqual(self.asked, [])
        self.tools.write_guards[:] = [lambda path, old, new: {"type": "probe", "path": path, "old": old}]
        result = self.apply(wrap("*** Update File: a.py\n@@\n-a = 1\n+a = 2\n*** Add File: c.py\n+c = 1"))
        self.assertEqual(result["checks"], [{"type": "probe", "path": "a.py", "old": "a = 1\n"},
                                            {"type": "probe", "path": "c.py", "old": None}])


class MatchingTests(PatchTestCase):
    def test_trailing_whitespace_drift_reports_fuzz(self):
        self.write("a.py", "def f():   \n    return 1  \n")
        result = self.apply(wrap("*** Update File: a.py\n@@\n def f():\n-    return 1\n+    return 2"))
        self.assertEqual(result["fuzz"], [{"path": "a.py", "hunk": 1, "pass": "rstrip"}])
        self.assertEqual((self.root / "a.py").read_text(), "def f():   \n    return 2\n")

    def test_ambiguous_fuzzy_match_fails(self):
        self.write("a.py", "x = 1  \ny = 2\n\nx = 1 \ny = 2\n")
        message = self.fails(wrap("*** Update File: a.py\n@@\n-x = 1\n+x = 9"), "Ambiguous match")
        self.assertIn("hunk 1 matches 2 places in a.py (lines 1, 4). Add an @@ line or more context.", message)

    def test_exact_repeat_applies_first_with_warning(self):
        self.write("a.py", "x = 1\ny = 2\nx = 1\n")
        result = self.apply(wrap("*** Update File: a.py\n@@\n-x = 1\n+x = 9"))
        self.assertEqual((self.root / "a.py").read_text(), "x = 9\ny = 2\nx = 1\n")
        self.assertEqual(len(result["warnings"]), 1)
        self.assertIn("matches 2 places (lines 1, 3)", result["warnings"][0])

    def test_context_marker_disambiguates(self):
        self.write("a.py", "def a():\n    v = 1\n\ndef b():\n    v = 1\n")
        result = self.apply(wrap("*** Update File: a.py\n@@ def b():\n-    v = 1\n+    v = 2"))
        self.assertEqual((self.root / "a.py").read_text(), "def a():\n    v = 1\n\ndef b():\n    v = 2\n")
        self.assertEqual(result["warnings"], [])
        self.fails(wrap("*** Update File: a.py\n@@ def c():\n-    v = 1\n+    v = 3"), "Failed to find context 'def c():' in a.py")

    def test_context_lines_keep_their_bytes(self):
        self.write("w.txt", b"keep\r\nold\nkeep2\r")
        self.apply(wrap("*** Update File: w.txt\n@@\n keep\n-old\n+new\n keep2"))
        self.assertEqual((self.root / "w.txt").read_bytes(), b"keep\r\nnew\r\nkeep2\r")

    def test_closest_match_report(self):
        lines = [f"value_{n} = {n}" for n in range(1, 40)]
        lines += ["def compute(total):", "\tresult\t= total  ", "\treturn result", "# end"]
        lines += [f"other_{n} = {n}" for n in range(10)]
        self.write("calc.py", "\n".join(lines) + "\n")
        message = self.fails(wrap("*** Update File: calc.py\n@@\n def compute(total):\n-    result = total\n"
                                  "+    result = total * 2\n     return result\n # end"), "Closest match at lines 40-43")
        self.assertIn("Failed to find expected lines in calc.py:", message)
        self.assertIn("\n-→result→= total··\n", message)
        self.assertIn("@@ -41,2 +2,2 @@", message)
        self.assertIn("(similarity ", message)
        self.assertTrue(message.endswith("No files were modified."))
        self.assertLessEqual(message.count("Closest match"), 3)

    def test_closest_match_probe_cap(self):
        common = ["}", "return x;", "  break;", "else {"]
        body = "".join(f"{common[n % 4]}\n" for n in range(1_250_000))[:4_900_000]
        self.write("big.c", body)
        pattern = "\n".join(f"-{common[n % 4]}" for n in range(199)) + "\n-missing line here"
        counts = []
        with mock_patch.object(patch, "probe_hook", counts.append):
            self.fails(wrap(f"*** Update File: big.c\n@@ no such context\n{pattern}"), "Failed to find context")
            self.fails(wrap(f"*** Update File: big.c\n@@\n{pattern}\n+new"), "Failed to find expected lines")
        self.assertEqual(len(counts), 1)
        self.assertLessEqual(counts[0], patch.MAX_PROBES)
        self.assertGreater(counts[0], patch.MAX_PROBES // 2)


class ShellRoutingTests(PatchTestCase):
    def test_heredoc_routes_with_shell_disabled(self):
        self.assertEqual(self.policy.shell_mode, "disabled")
        result = self.tools.call("shell", {"command": "apply_patch <<'EOF'\n*** Begin Patch\n*** Add File: a.txt\n+hi\n*** End Patch\nEOF"})
        self.assertEqual(result["routed_from"], "shell")
        self.assertEqual((self.root / "a.txt").read_text(), "hi\n")
        self.assertEqual([name for name, _ in self.asked], ["apply_patch"])

    def test_cd_prefix_writes_under_directory(self):
        (self.root / "sub").mkdir()
        self.tools.call("shell", {"command": "cd sub && apply_patch <<EOF\n*** Begin Patch\n*** Add File: b.txt\n+x\n*** End Patch\nEOF\n"})
        self.assertEqual((self.root / "sub/b.txt").read_text(), "x\n")

    def test_docker_mode_routes_without_running_docker(self):
        try:
            from fakes import fake_docker
        except ImportError:
            from tests.fakes import fake_docker
        self.policy.shell_mode = "docker"
        log = self.root / "docker.log"
        with fake_docker(log):
            result = self.tools.shell("apply_patch <<'EOF'\n*** Begin Patch\n*** Add File: d.txt\n+x\n*** End Patch\nEOF")
            self.tools.shell("printf hi")
        self.assertEqual(result["routed_from"], "shell")
        self.assertEqual([name for name, _ in self.asked], ["apply_patch", "shell"])
        runs = [json.loads(line) for line in log.read_text().splitlines()]
        self.assertEqual(sum("printf hi" in argv for argv in runs), 1)
        self.assertFalse(any("apply_patch" in " ".join(argv) for argv in runs))

    def test_routed_patch_still_obeys_write_policy(self):
        self.answer = False
        with self.assertRaises(HarnessError) as caught:
            self.tools.shell("apply_patch \"*** Begin Patch\n*** Add File: q.txt\n+x\n*** End Patch\"")
        self.assertIn("Action denied", str(caught.exception))
        self.assertFalse((self.root / "q.txt").exists())

    def test_trailing_commands_are_not_routed(self):
        commands = ["apply_patch <<'EOF'\n*** Begin Patch\n*** Add File: a.txt\n+hi\n*** End Patch\nEOF\n; rm -rf .",
                    "apply_patch <<'EOF'\n*** Begin Patch\n*** Add File: a.txt\n+hi\n*** End Patch\nEOF\nrm -rf .\nEOF",
                    "echo hi && apply_patch <<EOF\n*** Begin Patch\n*** End Patch\nEOF",
                    "cd $HOME && apply_patch <<EOF\n*** Begin Patch\n*** End Patch\nEOF",
                    "apply_patch '*** Begin Patch\n*** Add File: a\n+x' ; rm -rf . ; echo '\n*** End Patch'",
                    "apply_patch \"*** Begin Patch\n*** Add File: a\n+$(id)\n*** End Patch\""]
        for command in commands:
            with self.subTest(command=command):
                self.assertIsNone(patch.from_shell_command(command))
        with self.assertRaises(HarnessError) as caught:
            self.tools.call("shell", {"command": commands[0]})
        self.assertIn("Shell requires --shell docker", str(caught.exception))
        self.assertFalse((self.root / "a.txt").exists())

    def test_forms_that_route(self):
        body = "*** Begin Patch\n*** Add File: a\n+x\n*** End Patch"
        self.assertEqual(patch.from_shell_command(f"applypatch <<-\"END\"\n\t{body}\n\tEND\n"),
                         {"input": body, "directory": None})
        self.assertEqual(patch.from_shell_command(f"apply_patch '{body}'"), {"input": body, "directory": None})
        self.assertEqual(patch.from_shell_command(f"cd src/pkg && apply_patch <<'PATCH'\n{body}\nPATCH"),
                         {"input": body, "directory": "src/pkg"})


if __name__ == "__main__":
    unittest.main()
