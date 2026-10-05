import hashlib
from pathlib import Path
import tempfile
import unittest

from eira_harness.security import HarnessError, Redactor, Workspace
from eira_harness.store import Store
from eira_harness.tools import Policy, Toolbox


class EditingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root, Redactor(extra=("fixture-protected-value",)))
        self.approvals = []
        policy = Policy(approve=lambda name, detail: self.approvals.append((name, detail)) or True)
        self.tools = Toolbox(Workspace(self.root), self.store, policy, self.store.create("test"))

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def write(self, name, text):
        (self.root / name).write_text(text, newline="")
        return text

    def test_edit_replaces_unique_text_after_reviewed_diff(self):
        self.write("app.py", "def mean(xs):\n    return sum(xs) / (len(xs) - 1)\n")
        result = self.tools.call("edit_file", {"path": "app.py", "old_string": "(len(xs) - 1)", "new_string": "len(xs)"})
        self.assertEqual((self.root / "app.py").read_text(), "def mean(xs):\n    return sum(xs) / len(xs)\n")
        self.assertEqual(result["replacements"], 1)
        self.assertEqual(result["first_changed_line"], 2)
        self.assertEqual(result["sha256"], hashlib.sha256((self.root / "app.py").read_bytes()).hexdigest())
        name, diff = self.approvals[0]
        self.assertEqual(name, "edit_file")
        self.assertIn("-    return sum(xs) / (len(xs) - 1)", diff)
        self.assertIn("+    return sum(xs) / len(xs)", diff)

    def test_ambiguous_missing_and_noop_edits_are_refused(self):
        self.write("a.txt", "x = 1\nx = 1\n")
        cases = [({"old_string": "x = 1", "new_string": "x = 2"}, "2 places"),
                 ({"old_string": "y = 1", "new_string": "y = 2"}, "not found"),
                 ({"old_string": "x = 1", "new_string": "x = 1"}, "identical"),
                 ({"old_string": "", "new_string": "x"}, "empty")]
        for arguments, message in cases:
            with self.subTest(arguments=arguments), self.assertRaisesRegex(HarnessError, message):
                self.tools.call("edit_file", {"path": "a.txt", **arguments})
        self.assertEqual(self.approvals, [])
        result = self.tools.call("edit_file", {"path": "a.txt", "old_string": "x = 1", "new_string": "x = 2", "replace_all": True})
        self.assertEqual(result["replacements"], 2)
        self.assertEqual((self.root / "a.txt").read_text(), "x = 2\nx = 2\n")

    def test_edit_preserves_crlf_line_endings(self):
        self.write("win.txt", "one\r\ntwo\r\nthree\r\n")
        self.tools.edit_file("win.txt", "one\ntwo", "one\n2")
        self.assertEqual((self.root / "win.txt").read_bytes(), b"one\r\n2\r\nthree\r\n")

    def test_edit_respects_policy_staleness_and_protected_content(self):
        self.write("a.txt", "alpha\n")
        with self.assertRaisesRegex(HarnessError, "expected_sha256"):
            self.tools.edit_file("a.txt", "alpha", "beta", expected_sha256="0" * 64)
        self.tools.policy = Policy(approve=lambda *args: True, approve_writes=True, read_only=True)
        with self.assertRaisesRegex(HarnessError, "read-only"):
            self.tools.edit_file("a.txt", "alpha", "beta")
        self.tools.policy = Policy()
        with self.assertRaisesRegex(HarnessError, "denied"):
            self.tools.edit_file("a.txt", "alpha", "beta")
        self.assertEqual((self.root / "a.txt").read_text(), "alpha\n")
        self.tools.policy = Policy(approve_writes=True)
        self.write("secret.txt", "token=fixture-protected-value\nmode=dev\n")
        for path, old, new in [("secret.txt", "dev", "prod"), ("a.txt", "alpha", "fixture-protected-value"),
                               ("a.txt", "alpha", "[REDACTED]")]:
            with self.subTest(path=path, new=new), self.assertRaisesRegex(HarnessError, "protected"):
                self.tools.edit_file(path, old, new)
        with self.assertRaisesRegex(HarnessError, "write_file"):
            self.tools.edit_file("missing.txt", "a", "b")
        for path in ["../outside.txt", ".env", ".git/config"]:
            with self.subTest(path=path), self.assertRaises(HarnessError):
                self.tools.edit_file(path, "a", "b")

    def test_concurrent_change_during_approval_cancels_edit(self):
        self.write("a.txt", "alpha\n")
        def change(name, detail):
            (self.root / "a.txt").write_text("someone else\nalpha\n")
            return True
        self.tools.policy = Policy(approve=change)
        with self.assertRaisesRegex(HarnessError, "during approval"):
            self.tools.edit_file("a.txt", "alpha", "beta")
        self.assertEqual((self.root / "a.txt").read_text(), "someone else\nalpha\n")

    def test_large_files_are_read_in_pages_with_full_file_hash(self):
        text = self.write("big.txt", "".join(f"line {i:05d} " + "x" * 40 + "\n" for i in range(1, 3001)))
        first = self.tools.read_file("big.txt")
        self.assertTrue(first["truncated"])
        self.assertEqual(first["start_line"], 1)
        self.assertLessEqual(len(first["content"]), 24_000)
        self.assertEqual(first["total_lines"], 3000)
        self.assertEqual(first["sha256"], hashlib.sha256(text.encode()).hexdigest())
        second = self.tools.read_file("big.txt", first["next_start_line"])
        self.assertTrue(second["content"].startswith(f"line {first['next_start_line']:05d}"))
        window = self.tools.call("read_file", {"path": "big.txt", "start_line": 10, "end_line": 12})
        self.assertEqual(window["content"].splitlines()[0][:10], "line 00010")
        self.assertEqual(window["end_line"], 12)
        self.assertNotIn("truncated", window)
        for start, end in [(3001, None), (10, 9)]:
            with self.subTest(start=start, end=end), self.assertRaises(HarnessError):
                self.tools.read_file("big.txt", start, end)
        self.assertEqual(self.tools.read_file("big.txt", 1, 3)["sha256"], first["sha256"])

    def test_search_and_list_support_glob_and_case(self):
        (self.root / "src").mkdir()
        self.write("src/app.py", "Needle here\n")
        self.write("notes.md", "needle in notes\n")
        self.assertEqual(self.tools.list_files(glob="*.py")["files"], ["src/app.py"])
        self.assertEqual([m["path"] for m in self.tools.search_files("needle")["matches"]], ["notes.md"])
        insensitive = self.tools.call("search_files", {"query": "NEEDLE", "ignore_case": True})
        self.assertEqual(sorted(m["path"] for m in insensitive["matches"]), ["notes.md", "src/app.py"])
        self.assertEqual([m["path"] for m in self.tools.search_files("needle", glob="*.py", ignore_case=True)["matches"]],
                         ["src/app.py"])

    def test_describe_is_short_and_single_line(self):
        self.assertEqual(self.tools.describe("read_file", {"path": "a.py", "start_line": 5, "end_line": 9}), "a.py:5-9")
        self.assertEqual(self.tools.describe("search_files", {"query": "x y", "path": "src", "glob": "*.py"}), '"x y" in src (*.py)')
        self.assertEqual(len(self.tools.describe("shell", {"command": "echo " + "z" * 500})), 100)
        self.assertNotIn("\n", self.tools.describe("set_plan", {"plan": "1. a\n2. b"}))
        self.assertEqual(self.tools.describe("read_file", None), "")


if __name__ == "__main__":
    unittest.main()
