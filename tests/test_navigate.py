"""Code navigation: dotfiles, .gitignore, paging, regex isolation, skip counts and line numbers."""
import os
from pathlib import Path
import random
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

from eira_harness import navigate, search_worker, tools as tools_module
from eira_harness.navigate import is_ignored, parse_gitignore
from eira_harness.security import HarnessError, Redactor, Workspace
from eira_harness.store import Store
from eira_harness.text import split_lines
from eira_harness.tools import Policy, Toolbox


class NavigateTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root, Redactor())
        self.tools = Toolbox(Workspace(self.root), self.store, Policy(), self.store.create("navigate"))

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def write(self, name, content="x\n"):
        target = self.root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            target.write_bytes(content)
        else:
            target.write_text(content, encoding="utf-8", newline="")
        return target

    def listed(self, **arguments):
        return self.tools.call("list_files", arguments)["files"]


class DotfileTests(NavigateTestCase):
    BLOCKED = [".env", ".env.local", ".git/config", ".eira/extra.db", "key.pem", ".ssh/id_rsa",
               "deploy/.env.production", "certs/server.key", ".aws/credentials"]

    def test_safe_dotfiles_are_listed_and_blocked_paths_never_are(self):
        for name in [".github/workflows/ci.yml", ".gitignore", ".pre-commit-config.yaml", ".vscode/settings.json", "src/app.py"]:
            self.write(name)
        for name in self.BLOCKED:
            self.write(name, "private-test-credential\n")
        for ignored in (False, True):
            with self.subTest(ignored=ignored):
                result = self.tools.call("list_files", {"ignored": ignored})
                for name in [".github/workflows/ci.yml", ".gitignore", ".pre-commit-config.yaml", ".vscode/settings.json"]:
                    self.assertIn(name, result["files"])
                for name in self.BLOCKED + [".eira/state.db"]:
                    self.assertNotIn(name, result["files"])
                self.assertFalse(any(path.startswith((".git/", ".eira/", ".ssh/", ".aws/")) for path in result["files"]))
                self.assertGreater(result["skipped"]["blocked"], 0)
                found = self.tools.call("search_files", {"query": "private-test-credential", "ignored": ignored})
                self.assertEqual(found["matches"], [])
                found = self.tools.call("search_files", {"query": "private-test", "regex": True, "ignored": ignored})
                self.assertEqual(found["matches"], [])

    def test_protected_user_configuration_stays_invisible(self):
        self.write("config/app/token.txt", "needle\n")
        self.write("src/a.txt", "needle\n")
        with patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.root / "config")}):
            self.assertEqual(self.listed(ignored=True), ["src/a.txt"])
            self.assertEqual([m["path"] for m in self.tools.search_files("needle", regex=True)["matches"]], ["src/a.txt"])
        self.assertIn("config/app/token.txt", self.listed())

    def test_symlinks_and_hard_links_stay_invisible(self):
        self.write("real.txt", "needle\n")
        os.symlink(self.root / "real.txt", self.root / "link.txt")
        os.link(self.root / "real.txt", self.root / "hard.txt")
        self.assertEqual(self.listed(ignored=True), [])
        self.write("plain.txt", "needle\n")
        self.assertEqual([m["path"] for m in self.tools.search_files("needle", regex=True)["matches"]], ["plain.txt"])


class GitignoreTests(NavigateTestCase):
    def test_prototype_cases(self):
        cases = [
            ("node_modules/", "node_modules", True, True), ("node_modules/", "node_modules", False, False),
            ("node_modules/", "a/node_modules", True, True), ("*.log", "x.log", False, True),
            ("*.log", "deep/dir/x.log", False, True), ("/build", "build", True, True),
            ("/build", "src/build", True, False), ("docs/**/*.tmp", "docs/a/b/c.tmp", False, True),
            ("docs/**/*.tmp", "docs/c.tmp", False, True), ("docs/**/*.tmp", "other/docs/c.tmp", False, False),
            ("a/**/b", "a/b", False, True), ("a/**/b", "a/x/y/b", False, True),
            ("**/foo", "foo", False, True), ("**/foo", "x/y/foo", False, True),
            ("abc/**", "abc/x/y", False, True), ("abc/**", "abc", True, False),
            ("\\#hash", "#hash", False, True), ("\\!bang", "!bang", False, True),
            ("[!a]x", "bx", False, True), ("[!a]x", "ax", False, False), ("f?o", "f/o", False, False),
            ("trail\\", "trail", False, False), ("space\\ ", "space ", False, True), ("pad   ", "pad", False, True),
        ]
        for pattern, path, is_dir, expected in cases:
            with self.subTest(pattern=pattern, path=path):
                self.assertEqual(is_ignored(path, is_dir, (("", parse_gitignore(pattern + "\n")),)), expected)
        self.assertEqual(parse_gitignore("# comment\n\n   \n"), ())
        rules = parse_gitignore("*.log\n!keep.log\n")
        self.assertTrue(is_ignored("a.log", False, (("", rules),)))
        self.assertFalse(is_ignored("keep.log", False, (("", rules),)))

    def test_root_and_nested_rules(self):
        self.write(".gitignore", "node_modules/\n*.log\n!keep.log\n/build\ndocs/**/*.tmp\n")
        self.write("sub/.gitignore", "!*.log\n")
        visible = ["keep.log", "src/build/out.txt", "docs/a.md", "sub/app.log", "sub/x.txt", "node_modules.txt",
                   "docs/x.tmp.md"]
        hidden = ["app.log", "deep/dir/trace.log", "node_modules/pkg/index.js", "lib/node_modules/x.js",
                  "build/out.txt", "docs/a/b/c.tmp", "docs/c.tmp"]
        for name in visible + hidden:
            self.write(name)
        self.write(".env", "secret\n")
        default = self.listed()
        for name in visible:
            self.assertIn(name, default)
        for name in hidden:
            self.assertNotIn(name, default)
        self.assertGreater(self.tools.list_files()["skipped"]["ignored"], 0)
        everything = self.listed(ignored=True)
        for name in visible + hidden:
            self.assertIn(name, everything)
        self.assertNotIn(".env", everything)
        searched = {m["path"] for m in self.tools.search_files("x")["matches"]}
        self.assertTrue(searched.isdisjoint(hidden))
        self.assertIn("sub/app.log", searched)

    def test_ignored_directory_cannot_be_reincluded_and_subpaths_inherit_rules(self):
        self.write(".gitignore", "logs/\n!logs/keep.txt\n*.tmp\n")
        self.write("logs/keep.txt")
        self.write("pkg/a.tmp")
        self.write("pkg/b.py")
        self.assertNotIn("logs/keep.txt", self.listed())
        self.assertEqual(self.listed(path="pkg"), ["pkg/b.py"])
        self.assertEqual(self.listed(path="pkg", ignored=True), ["pkg/a.tmp", "pkg/b.py"])

    def test_heavy_directories_are_skipped_and_counted(self):
        for name in ["venv/lib.py", ".venv/x.py", "__pycache__/m.pyc", ".pytest_cache/v", "app.py"]:
            self.write(name)
        result = self.tools.list_files()
        self.assertEqual(result["files"], ["app.py"])
        self.assertEqual(result["skipped"]["heavy_dirs"], 4)
        self.assertIn("venv/lib.py", self.listed(ignored=True))


class PagingTests(NavigateTestCase):
    def test_large_repository_pages_deterministically(self):
        names = sorted(f"d{i % 7}/f{i:04d}.txt" for i in range(3_000))
        for name in names:
            self.write(name, "")
        pages, offset = [], 0
        while offset is not None:
            result = self.tools.call("list_files", {"offset": offset})
            self.assertLessEqual(len(result["files"]), 500)
            pages.append(result["files"])
            self.assertEqual(result["truncated"], result["next_offset"] is not None)
            offset = result["next_offset"]
        self.assertEqual(len(pages), 6)
        self.assertEqual([path for page in pages for path in page], names)
        self.assertEqual(self.tools.call("list_files", {"limit": 2000})["next_offset"], 2000)
        self.assertEqual(self.tools.list_files(offset=2_990)["files"], names[2_990:])
        # Search is not limited to the first listing page.
        self.write("zz/last.txt", "needle\n")
        self.assertEqual(self.tools.search_files("needle")["matches"][0]["path"], "zz/last.txt")
        for arguments in ({"limit": 0}, {"limit": 2001}, {"offset": -1}):
            with self.subTest(arguments=arguments), self.assertRaises(HarnessError):
                self.tools.call("list_files", arguments)


class RegexTests(NavigateTestCase):
    def setUp(self):
        super().setUp()
        self.write("src/a.py", "import os\n\ndef first(x):\n    return x\n\ndef second():\n    pass\n")
        self.write("src/b.py", "# b\ndef third(y):\n    return y\n")

    def test_matches_with_context(self):
        result = self.tools.call("search_files", {"query": r"def (\w+)\(", "regex": True, "context": 2})
        self.assertEqual([(m["path"], m["line"]) for m in result["matches"]],
                         [("src/a.py", 3), ("src/a.py", 6), ("src/b.py", 2)])
        first, second, third = result["matches"]
        self.assertEqual(first["before"], ["import os", ""])
        self.assertEqual(first["after"], ["    return x", ""])
        self.assertEqual(second["before"], ["    return x", ""])
        self.assertEqual(second["after"], ["    pass"])
        self.assertEqual(third["before"], ["# b"])
        self.assertEqual(first["column"], 1)
        self.assertEqual(result["files_searched"], 2)
        self.assertFalse(result["truncated"])
        plain = self.tools.search_files(r"def (\w+)\(", regex=True)["matches"][0]
        self.assertNotIn("before", plain)
        literal = self.tools.search_files("def second", context=1)["matches"][0]
        self.assertEqual((literal["before"], literal["after"]), ([""], ["    pass"]))

    def test_output_modes_and_limits(self):
        files = self.tools.call("search_files", {"query": r"^def ", "regex": True, "output": "files"})
        self.assertEqual(files["files"], [{"path": "src/a.py", "count": 2}, {"path": "src/b.py", "count": 1}])
        self.assertEqual(files["total_matches"], 3)
        count = self.tools.call("search_files", {"query": "return", "output": "count"})
        self.assertEqual((count["total_matches"], count["files_with_matches"], count["files_searched"]), (2, 2, 2))
        self.assertNotIn("matches", count)
        self.write("src/c.py", "def a():\n    pass\ndef b():\n    pass\n")
        limited = self.tools.call("search_files", {"query": r"def \w+", "regex": True, "max_results": 3})
        self.assertEqual(len(limited["matches"]), 3)
        self.assertTrue(limited["truncated"])
        exact = self.tools.call("search_files", {"query": r"def \w+", "regex": True, "max_results": 5})
        self.assertEqual(len(exact["matches"]), 5)
        self.assertFalse(exact["truncated"])
        one = self.tools.search_files("def", output="files", max_results=1)
        self.assertEqual(one["files"], [{"path": "src/a.py", "count": 2}])
        self.assertTrue(one["truncated"])
        insensitive = self.tools.search_files("DEF THIRD", regex=True, ignore_case=True)
        self.assertEqual([m["line"] for m in insensitive["matches"]], [2])

    def test_catastrophic_pattern_is_killed(self):
        self.write("evil.txt", "a" * 5_000 + "b\n")
        spawned = []
        real = subprocess.Popen

        def record(*args, **kwargs):
            process = real(*args, **kwargs)
            spawned.append(process)
            return process

        with patch.object(navigate, "REGEX_TIMEOUT", 1), patch.object(navigate.subprocess, "Popen", record):
            started = time.monotonic()
            with self.assertRaisesRegex(HarnessError, "timed out after 1 s"):
                self.tools.call("search_files", {"query": "(a+)+$", "regex": True, "path": ".", "glob": "evil.txt"})
            elapsed = time.monotonic() - started
        self.assertLess(elapsed, 3.5)
        self.assertEqual(len(spawned), 1)
        self.assertIsNotNone(spawned[0].returncode)
        with self.assertRaises(ProcessLookupError):
            os.kill(spawned[0].pid, 0)

    def test_invalid_pattern_fails_fast_without_a_subprocess(self):
        with patch.object(navigate.subprocess, "Popen", side_effect=AssertionError("spawned")) as popen:
            with self.assertRaisesRegex(HarnessError, "Invalid regular expression"):
                self.tools.call("search_files", {"query": "(", "regex": True})
            with self.assertRaises(HarnessError):
                self.tools.call("search_files", {"query": "x", "output": "lines"})
            self.tools.call("search_files", {"query": "(", "regex": False})
        self.assertEqual(popen.call_count, 0)

    def test_worker_runs_isolated(self):
        seen = {}
        real = subprocess.Popen

        def record(argv, **kwargs):
            seen.update(argv=argv, **kwargs)
            return real(argv, **kwargs)

        self.write("sys.py", "raise SystemExit('workspace module imported')\n")
        self.write("json.py", "raise SystemExit('workspace module imported')\n")
        with patch.object(navigate.subprocess, "Popen", record):
            result = self.tools.search_files("workspace module", regex=True)
        self.assertEqual(len(result["matches"]), 2)
        self.assertEqual(seen["argv"][1:], ["-I", str(Path(navigate.__file__).with_name("search_worker.py"))])
        self.assertEqual((seen["cwd"], seen["env"]), ("/", {}))


class SkipAndLineTests(NavigateTestCase):
    def test_skipped_files_are_counted_by_reason(self):
        self.write("big.txt", b"needle\n" + b"x" * 6_000_000)
        self.write("bin.dat", b"needle\x00\x01")
        self.write("latin.txt", "needle caf\xe9\n".encode("latin-1"))
        self.write("ok.txt", "needle\n")
        for regex in (False, True):
            with self.subTest(regex=regex):
                result = self.tools.search_files("needle", regex=regex)
                self.assertEqual([m["path"] for m in result["matches"]], ["ok.txt"])
                self.assertEqual({key: result["skipped"][key] for key in ("binary", "too_large", "non_utf8")},
                                 {"binary": 1, "too_large": 1, "non_utf8": 1})
                self.assertEqual(result["skipped_files"], 3)
                self.assertEqual(result["files_searched"], 1)
                self.assertIn("skipped", result["note"])

    def test_files_up_to_five_megabytes_are_searched(self):
        self.write("large.txt", "y" * 4_000_000 + "\nneedle\n")
        self.assertEqual(self.tools.search_files("needle")["matches"][0]["line"], 2)

    def test_line_numbering_matches_apply_patch_rule(self):
        self.write("ff.txt", "a\x0cb\nc\n")
        self.assertEqual(self.tools.read_file("ff.txt")["total_lines"], 2)
        page = self.tools.read_file("ff.txt", 2, 2)
        self.assertEqual(page["content"], "c\n")
        for regex in (False, True):
            self.assertEqual(self.tools.search_files("c", regex=regex)["matches"][0]["line"], 2)
        self.write("mixed.txt", "one\r\ntwo\rthree still\nfour")
        self.assertEqual(self.tools.read_file("mixed.txt")["total_lines"], 4)
        for regex in (False, True):
            self.assertEqual(self.tools.search_files("four", regex=regex)["matches"][0]["line"], 4)
            self.assertEqual(self.tools.search_files("still", regex=regex)["matches"][0]["line"], 3)

    def test_worker_splitter_matches_text_module(self):
        rng = random.Random(7)
        alphabet = ["a", "\n", "\r", "\r\n", "\x0c", " ", "\x85", "\x0b", "z"]
        for _ in range(200):
            text = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 40)))
            self.assertEqual(search_worker.split_lines(text), split_lines(text))
        self.assertEqual(navigate.MAX_TEXT_FILE, tools_module.MAX_TEXT_FILE)


class DescribeTests(NavigateTestCase):
    def test_progress_details(self):
        describe = self.tools.describe
        self.assertEqual(describe("search_files", {"query": "x y", "path": "src", "glob": "*.py"}), '"x y" in src (*.py)')
        self.assertEqual(describe("search_files", {"query": r"def \w+", "regex": True, "path": "src"}), r"/def \w+/ in src")
        self.assertEqual(describe("list_files", {"path": "src", "glob": "*.py", "ignored": True}), "src (*.py) +ignored")
        self.assertEqual(describe("list_files", {}), ".")


if __name__ == "__main__":
    unittest.main()
