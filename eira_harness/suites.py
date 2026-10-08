"""Built-in behavioral eval suites. Checks run the task's own tests in the Docker sandbox."""
from __future__ import annotations


def _tests(command: str = "python -m unittest -q", restore: tuple = ("tests/test_main.py",)) -> dict:
    # Restore the fixture tests first, so an agent cannot pass by editing them.
    return {"type": "command_succeeds", "command": command, "timeout": 120, "restore": list(restore)}


_NOISE_TEST = '''import sys
import unittest

from service import config


class StartupTests(unittest.TestCase):
    def test_pool_size(self):
        for i in range(3000):
            print(f"debug: warming cache shard {i:04d} of 3000 (ok)", file=sys.stderr)
        # The cause is only visible at the very end of a long log.
        self.assertEqual(config.POOL_SIZE, 8, "POOL_SIZE must be 8: the database allows at most 8 connections")


if __name__ == "__main__":
    unittest.main()
'''

CODING_SUITE = {
    "name": "coding",
    "description": "Behavioral coding tasks. Each passes only if the task's tests pass in the Docker sandbox "
                   "after the agent finishes, with fixture tests restored first.",
    "sandbox": {"image": "python:3.11-slim"},
    "tasks": [
        {"id": "multi-module-bug", "agent_shell": True,
         "prompt": "The cart total is wrong for taxed items. Find and fix the bug so the test suite passes "
                   "(run it with: python -m unittest -q). Do not change the tests.",
         "files": {"pricing/__init__.py": "",
                   "pricing/tax.py": "def with_tax(amount, rate_percent):\n    return amount * (1 + rate_percent / 10)\n",
                   "pricing/cart.py": "from .tax import with_tax\n\n\ndef total(items, rate_percent):\n"
                                      "    return round(sum(with_tax(price * qty, rate_percent) for price, qty in items), 2)\n",
                   "tests/__init__.py": "",
                   "tests/test_main.py": "import unittest\n\nfrom pricing.cart import total\n\n\nclass CartTests(unittest.TestCase):\n"
                                         "    def test_total_with_tax(self):\n        self.assertEqual(total([(10.0, 2), (5.0, 1)], 8), 27.0)\n\n"
                                         "    def test_no_tax(self):\n        self.assertEqual(total([(3.0, 3)], 0), 9.0)\n"},
         "checks": [_tests()]},
        {"id": "implement-to-spec", "agent_shell": True,
         "prompt": "Implement slugify() in textkit/slug.py exactly as its docstring describes. The tests in "
                   "tests/test_main.py must pass (python -m unittest -q). Do not change the tests.",
         "files": {"textkit/__init__.py": "",
                   "textkit/slug.py": 'def slugify(text):\n    """Return a URL slug.\n\n    Lowercase the text, replace each run of characters that are not a-z or 0-9\n'
                                      '    with a single hyphen, and strip hyphens from both ends.\n    """\n    raise NotImplementedError\n',
                   "tests/__init__.py": "",
                   "tests/test_main.py": "import unittest\n\nfrom textkit.slug import slugify\n\n\nclass SlugTests(unittest.TestCase):\n"
                                         "    def test_examples(self):\n        self.assertEqual(slugify('Hello, World!'), 'hello-world')\n"
                                         "        self.assertEqual(slugify('  --Eira  0.5--  '), 'eira-0-5')\n        self.assertEqual(slugify('***'), '')\n"},
         "checks": [_tests()]},
        {"id": "package-rename",
         "prompt": "Rename the function load_cfg to load_config throughout the project, including every import "
                   "and call site, so the tests keep passing.",
         "files": {"app/__init__.py": "",
                   "app/settings.py": "def load_cfg(text):\n    return dict(line.split('=', 1) for line in text.splitlines() if '=' in line)\n",
                   "app/server.py": "from .settings import load_cfg\n\n\ndef port(text):\n    return int(load_cfg(text).get('port', '80'))\n",
                   "tests/__init__.py": "",
                   "tests/test_main.py": "import unittest\n\nfrom app.server import port\nfrom app.settings import load_config\n\n\n"
                                         "class RenameTests(unittest.TestCase):\n    def test_renamed(self):\n"
                                         "        self.assertEqual(load_config('a=1'), {'a': '1'})\n        self.assertEqual(port('port=8080'), 8080)\n"},
         "checks": [_tests(), {"type": "file_not_contains", "path": "app/settings.py", "text": "load_cfg"},
                    {"type": "file_not_contains", "path": "app/server.py", "text": "load_cfg"}]},
        {"id": "crlf-module-fix",
         "prompt": "geometry.py computes the area of a rectangle incorrectly. Fix it so the tests pass, keeping "
                   "the file's Windows line endings.",
         "files": {"geometry.py": "def area(width, height):\r\n    return width + height\r\n\r\n\r\ndef perimeter(width, height):\r\n"
                                  "    return 2 * (width + height)\r\n",
                   "tests/__init__.py": "",
                   "tests/test_main.py": "import unittest\n\nfrom geometry import area, perimeter\n\n\nclass GeometryTests(unittest.TestCase):\n"
                                         "    def test_area(self):\n        self.assertEqual(area(3, 4), 12)\n\n"
                                         "    def test_perimeter(self):\n        self.assertEqual(perimeter(3, 4), 14)\n"},
         "checks": [_tests(), {"type": "file_matches", "path": "geometry.py", "pattern": r"\A(?:[^\n]*\r\n)+\Z"}]},
        {"id": "long-traceback", "agent_shell": True,
         "prompt": "The test suite fails (python -m unittest -q) and prints a very long log. Find the cause and fix "
                   "the configuration so the tests pass. Do not change the tests.",
         "files": {"service/__init__.py": "", "service/config.py": "POOL_SIZE = 32\nTIMEOUT = 5\n",
                   "tests/__init__.py": "", "tests/test_main.py": _NOISE_TEST},
         "checks": [_tests(), {"type": "file_contains", "path": "service/config.py", "text": "TIMEOUT = 5"}]},
        {"id": "three-edits",
         "prompt": "In limits.py set MAX_USERS to 500, MAX_UPLOAD_MB to 25, and RETENTION_DAYS to 90. Leave every "
                   "other line unchanged.",
         "files": {"limits.py": "MAX_USERS = 100\n" + "".join(f"# reserved setting {i}\n" for i in range(40)) +
                                "MAX_UPLOAD_MB = 10\n" + "".join(f"# reserved option {i}\n" for i in range(40)) +
                                "RETENTION_DAYS = 30\nREGION = 'eu'\n",
                   "tests/__init__.py": "",
                   "tests/test_main.py": "import unittest\n\nimport limits\n\n\nclass LimitTests(unittest.TestCase):\n"
                                         "    def test_values(self):\n        self.assertEqual((limits.MAX_USERS, limits.MAX_UPLOAD_MB, "
                                         "limits.RETENTION_DAYS, limits.REGION), (500, 25, 90, 'eu'))\n"},
         "checks": [_tests(), {"type": "file_contains", "path": "limits.py", "text": "# reserved option 39\nRETENTION_DAYS = 90\n"}]},
        {"id": "argparse-flag", "agent_shell": True,
         "prompt": "Add a --limit N option (an integer, default 10) to tool.py's parser, and make main() print at most "
                   "N items. The tests in tests/test_main.py describe the behavior; make them pass without changing them.",
         "files": {"tool.py": "import argparse\n\n\ndef build_parser():\n    parser = argparse.ArgumentParser(prog='tool')\n"
                              "    parser.add_argument('items', nargs='*')\n    return parser\n\n\n"
                              "def main(argv=None):\n    args = build_parser().parse_args(argv)\n    return list(args.items)\n",
                   "tests/__init__.py": "",
                   "tests/test_main.py": "import unittest\n\nfrom tool import build_parser, main\n\n\nclass FlagTests(unittest.TestCase):\n"
                                         "    def test_default(self):\n        self.assertEqual(build_parser().parse_args([]).limit, 10)\n\n"
                                         "    def test_limit(self):\n        self.assertEqual(main(['a', 'b', 'c', '--limit', '2']), ['a', 'b'])\n"},
         "checks": [_tests()]},
        {"id": "already-passing",
         "prompt": "Check whether the tests in this project pass and fix anything that is broken. If nothing is broken, "
                   "say so and leave the files unchanged.",
         "files": {"mathx.py": "def double(x):\n    return 2 * x\n",
                   "tests/__init__.py": "",
                   "tests/test_main.py": "import unittest\n\nfrom mathx import double\n\n\nclass DoubleTests(unittest.TestCase):\n"
                                         "    def test_double(self):\n        self.assertEqual(double(4), 8)\n"},
         "checks": [_tests(), {"type": "file_unchanged", "path": "mathx.py"}]},
    ],
}
