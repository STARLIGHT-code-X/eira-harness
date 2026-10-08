"""Shell sandbox mount plan: masked secrets, read-only config, sanitized git config, post-run checks."""
from contextlib import redirect_stderr
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from eira_harness import sandbox
from eira_harness.agent import Agent
from eira_harness.cli import renderer
from eira_harness.security import HarnessError, Redactor, Workspace, protected_kind
from eira_harness.store import Store
from eira_harness.terminal import Terminal
from eira_harness.tools import Policy, Toolbox

try:
    from fakes import fake_docker
except ImportError:  # run as tests.test_sandbox from the repository root
    from tests.fakes import fake_docker

HARDENING = ["--pull=never", "--log-driver=none", "--network=none", "--read-only", "--cap-drop=ALL",
             "--security-opt=no-new-privileges", "--pids-limit=128", "--memory=512m", "--cpus=1"]
EXPECTED_ENV = {"HOME=/tmp", "LANG=C.UTF-8", "TERM=dumb", "NO_COLOR=1", "PAGER=cat", "GIT_PAGER=cat",
                "GIT_OPTIONAL_LOCKS=0", "GIT_CONFIG_NOSYSTEM=1"}
GIT_CONFIG = ('[core]\n\trepositoryformatversion = 0\n\tbare = false\n'
              '[remote "origin"]\n\turl = https://u:ghp_fixture@github.com/x/y\n'
              '\tfetch = +refs/heads/*:refs/remotes/origin/*\n'
              '[branch "main"]\n\tremote = origin\n\tmerge = refs/heads/main\n')


def write(root: Path, relative: str, text: str = "x\n") -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def mounts_in(argv):
    """(flag, spec) pairs for every protective mount, in argv order."""
    pairs = [(argv[i], argv[i + 1]) for i, arg in enumerate(argv[:-1]) if arg in {"--mount", "--tmpfs"}]
    return [pair for pair in pairs if "/workspace/" in pair[1] and "/workspace/.eira:" not in pair[1]]


def destination(spec):
    if "dst=" in spec:
        return spec.split("dst=", 1)[1].split(",", 1)[0]
    return spec.split(":", 1)[0]


class SandboxCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.store = Store(self.root, Redactor())
        self.session = self.store.create("sandbox")
        self.asked = []
        self.answer = True
        self.policy = Policy(approve=lambda name, detail: self.asked.append((name, detail)) or self.answer,
                             shell_mode="docker")
        self.tools = Toolbox(Workspace(self.root), self.store, self.policy, self.session)
        self.log = self.root.parent / f"{self.root.name}-docker.jsonl"
        self.addCleanup(lambda: self.log.unlink(missing_ok=True))

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def fixture(self):
        for relative in (".env", "api/.env.local", "keys/server.pem", ".ssh/id_rsa", ".git/hooks/pre-commit.sample",
                         ".github/workflows/ci.yml", ".vscode/settings.json", ".husky/pre-commit", "AGENTS.md",
                         "Makefile", "node_modules/p/.env", "src/app.py"):
            write(self.root, relative)
        write(self.root, ".git/config", GIT_CONFIG)
        write(self.root, "sub/.git", "gitdir: ../.git/modules/sub\n")


class MountPlanTests(SandboxCase):
    def test_fixture_argv(self):
        self.fixture()
        plan = sandbox.build(self.root, self.store.redact)
        sandbox.prepare(self.root, "eira-test", plan)
        self.addCleanup(sandbox.cleanup, self.root, "eira-test")
        argv = sandbox.docker_argv("docker", "eira-test", "img:1", self.root, 1000, 1001, "true", plan)
        mounts = mounts_in(argv)
        specs = [spec for _, spec in mounts]
        for secret in (".env", "api/.env.local", "keys/server.pem"):
            self.assertIn(f"type=bind,src=/dev/null,dst=/workspace/{secret},readonly", specs)
        self.assertIn(("--tmpfs", "/workspace/.ssh:ro,size=4k,mode=0500"), mounts)
        for config in (".git", "sub/.git", ".github/workflows", ".vscode", ".husky", "AGENTS.md"):
            self.assertIn(f"type=bind,src={self.root / config},dst=/workspace/{config},readonly", specs)
        copy = sandbox.copies_dir(self.root, "eira-test") / "git-config-1"
        self.assertIn(f"type=bind,src={copy},dst=/workspace/.git/config,readonly", specs)
        sanitized = copy.read_text()
        self.assertNotIn("ghp_fixture", sanitized)
        self.assertNotIn("u:", sanitized)
        self.assertIn("url = https://github.com/x/y\n", sanitized)
        self.assertEqual(copy.stat().st_mode & 0o777, 0o600)
        destinations = [destination(spec) for spec in specs]
        self.assertEqual(destinations, sorted(destinations))
        self.assertEqual(len(destinations), len(set(destinations)))
        self.assertEqual(len(destinations), 11)
        for absent in ("/workspace/Makefile", "/workspace/node_modules/p/.env", "/workspace/.git/hooks",
                       "/workspace/src/app.py"):
            self.assertNotIn(absent, destinations)
        for flag in HARDENING:
            self.assertIn(flag, argv)
        self.assertEqual(argv[argv.index("--user") + 1], "1000:1001")
        self.assertIn(f"type=bind,src={self.root},dst=/workspace", argv)
        self.assertIn("/workspace/.eira:rw,size=1m,mode=0700", argv)
        self.assertIn("/tmp:rw,size=64m,mode=1777", argv)
        # Protective mounts follow the workspace bind and the .eira tmpfs.
        self.assertGreater(argv.index(mounts[0][1]), argv.index("/workspace/.eira:rw,size=1m,mode=0700"))
        env = [argv[i + 1] for i, arg in enumerate(argv) if arg == "-e"]
        self.assertEqual(set(env), EXPECTED_ENV)
        self.assertEqual(len(env), len(EXPECTED_ENV))
        self.assertEqual(argv[argv.index("--label") + 1], "eira.managed=1")
        self.assertEqual(argv[-5:], ["--entrypoint", "/bin/sh", "img:1", "-c", "true"])
        self.assertEqual((plan["masked"], plan["read_only"], len(plan["git_configs"])), (4, 6, 1))
        self.assertEqual(plan["summary"], "Sandbox: no network, read-only system, 4 secret paths hidden, "
                                          "6 config paths read-only, 1 git config sanitized")
        sandbox.cleanup(self.root, "eira-test")
        self.assertFalse(sandbox.copies_dir(self.root, "eira-test").exists())

    def test_clean_git_config_is_not_copied(self):
        write(self.root, ".git/config", "[core]\n\tbare = false\n[remote \"origin\"]\n\turl = ssh://git@host/x\n")
        plan = sandbox.build(self.root, self.store.redact)
        self.assertEqual(plan["git_configs"], [])
        self.assertEqual(plan["mounts"], [("readonly", ".git")])

    def test_submodule_git_configs_are_sanitized(self):
        write(self.root, ".git/config", "[core]\n\tbare = false\n")
        write(self.root, ".git/modules/libs/a/HEAD", "ref: refs/heads/main\n")
        write(self.root, ".git/modules/libs/a/config", "[http]\n\textraheader = AUTHORIZATION: basic abc\n")
        write(self.root, ".git/modules/libs/a/modules/b/config", "[credential]\n\thelper = store\n")
        plan = sandbox.build(self.root, self.store.redact)
        self.assertEqual([relative for relative, _ in plan["git_configs"]],
                         [".git/modules/libs/a/config", ".git/modules/libs/a/modules/b/config"])
        self.assertEqual([data for _, data in plan["git_configs"]], [b"[http]\n", b""])

    def test_secrets_inside_config_directories_and_nested_state_are_masked(self):
        write(self.root, ".devcontainer/devcontainer.json")
        write(self.root, ".devcontainer/.env")
        write(self.root, ".devcontainer/.vscode/settings.json")
        write(self.root, "other/.eira/state.db")
        plan = sandbox.build(self.root, self.store.redact)
        self.assertEqual(plan["mounts"], [("readonly", ".devcontainer"), ("mask_file", ".devcontainer/.env"),
                                          ("mask_dir", "other/.eira")])

    def test_configured_credential_paths_inside_the_workspace_are_masked(self):
        write(self.root, "cloud/kubeconfig.yaml")
        with patch.dict(os.environ, {"KUBECONFIG": str(self.root / "cloud" / "kubeconfig.yaml")}):
            plan = sandbox.build(self.root, self.store.redact)
        self.assertEqual(plan["mounts"], [("mask_file", "cloud/kubeconfig.yaml")])

    def test_inexpressible_protected_path_fails_before_approval(self):
        write(self.root, "a,b/.vscode/settings.json")
        with fake_docker(self.log), self.assertRaisesRegex(HarnessError, "cannot be expressed safely"):
            self.tools.shell("true")
        self.assertEqual((self.asked, self.calls()), ([], []))
        for name in ('x:y/.env', 'q"/.env', "n\nl/.env"):
            with self.subTest(name=name):
                with tempfile.TemporaryDirectory() as other:
                    write(Path(other), name)
                    with self.assertRaisesRegex(HarnessError, "cannot be expressed safely"):
                        sandbox.build(Path(other))

    def test_symlinked_config_fails_closed_but_symlinked_secret_is_skipped(self):
        write(self.root, ".env")
        (self.root / ".env.prod").symlink_to(".env")
        plan = sandbox.build(self.root)
        self.assertEqual(plan["mounts"], [("mask_file", ".env")])
        with tempfile.TemporaryDirectory() as target:
            (self.root / ".idea").symlink_to(target)
            with fake_docker(self.log), self.assertRaisesRegex(HarnessError, r"\.idea.* is a symlink"):
                self.tools.shell("true")
        self.assertEqual((self.asked, self.calls()), ([], []))

    def test_symlinked_pair_parent_fails_closed(self):
        (self.root / "ci").mkdir()
        (self.root / ".github").symlink_to("ci")
        with self.assertRaisesRegex(HarnessError, r"\.github.* is a symlink"):
            sandbox.build(self.root)

    def test_scan_limits_fail_before_approval(self):
        for index in range(20):
            write(self.root, f"src/f{index}.py")
        with patch.object(sandbox, "SCAN_MAX_ENTRIES", 10), fake_docker(self.log):
            with self.assertRaisesRegex(HarnessError, "could not verify protected paths"):
                self.tools.shell("true")
        self.assertEqual((self.asked, self.calls()), ([], []))
        deep = self.root / "deep"
        with patch.object(sandbox, "SCAN_MAX_DEPTH", 3):
            (deep / "a" / "b" / "c" / "d" / "e").mkdir(parents=True)
            with self.assertRaisesRegex(HarnessError, "could not verify protected paths"):
                sandbox.build(self.root)

    def test_too_many_mounts_fail_closed(self):
        for index in range(6):
            write(self.root, f"p{index}/.env")
        with patch.object(sandbox, "MAX_MOUNTS", 5), self.assertRaisesRegex(HarnessError, "at most 5"):
            sandbox.build(self.root)


class ClassificationTests(unittest.TestCase):
    def test_protected_kind(self):
        self.assertEqual([protected_kind(name) for name in (".eira", ".Git", ".codex", ".ENV.local", "id_rsa",
                                                             ".npmrc", "server.KEY", "app.py", ".envrc")],
                         ["state", "vcs", "state", "secret", "secret", "secret", "secret", None, None])

    def test_requires_review(self):
        for path in (".github/workflows/ci.yml", "AGENTS.md", "pkg/agents.md", ".VSCode/x.json", "sub/.claude/a",
                     ".yarn/plugins/p.cjs", ".envrc", ".pre-commit-config.yaml", "Jenkinsfile", "a/.cargo/config.toml"):
            self.assertTrue(sandbox.requires_review(path), path)
        for path in ("src/app.py", "Makefile", "package.json", "docs/workflows/x.yml", "github/workflows/x",
                     ".github/CODEOWNERS", ".yarn/cache/x.zip", "pyproject.toml"):
            self.assertFalse(sandbox.requires_review(path), path)


class SanitizerTests(unittest.TestCase):
    def test_credentials_are_removed_and_other_lines_survive(self):
        keep = ('[core]\n\trepositoryformatversion = 0\n\tfilemode = true\n'
                '[remote "origin"]\n\turl = https://github.com/x/y.git\n\tfetch = +refs/heads/*:refs/remotes/origin/*\n')
        branch = '[branch "main"]\n\tremote = origin\n\tmerge = refs/heads/main\n'
        text = (keep + '[remote "fork"]\n\turl = https://bob:s3cret-pass@example.com/y.git\n'
                '[http "https://example.com/"]\n\textraheader = AUTHORIZATION: bearer abcdef\n'
                '[credential]\n\thelper = store\n\tusername = bob\n'
                '[credential "https://example.com"]\n\tusername = bob\n'
                '[user]\n\tpassword = hunter2\n\ttoken = t0ken\n\tname = Bob\n'
                '[gitlab]\n\thelper = "!f() { echo x; \\\n\t}; f"\n' + branch)
        clean = sandbox.sanitize_git_config(text)
        self.assertTrue(clean.startswith(keep))
        self.assertTrue(clean.endswith(branch))
        for secret in ("bob:", "s3cret", "extraheader", "abcdef", "helper", "[credential", "hunter2", "t0ken", "echo x"):
            self.assertNotIn(secret, clean)
        self.assertIn('[remote "fork"]\n\turl = https://example.com/y.git\n', clean)
        self.assertIn("[user]\n\tname = Bob\n", clean)
        self.assertEqual(sandbox.sanitize_git_config(keep + branch), keep + branch)
        crlf = keep.replace("\n", "\r\n")
        self.assertEqual(sandbox.sanitize_git_config(crlf), crlf)

    def test_redactor_and_inline_keys(self):
        with patch.dict(os.environ, {"EIRA_FIXTURE_TOKEN": "fixture-env-secret"}):
            redact = Redactor()
        text = "[core]\n\tsshCommand = ssh -o X=fixture-env-secret\n[http] extraHeader = X: y\n"
        self.assertEqual(sandbox.sanitize_git_config(text, redact),
                         "[core]\n\tsshCommand = ssh -o X=[REDACTED]\n[http]\n")
        self.assertEqual(sandbox.sanitize_git_config("\turl = ssh://git@host/x\n"), "\turl = ssh://git@host/x\n")
        self.assertEqual(sandbox.sanitize_git_config("\turl = ssh://git:pw@host/x\n"), "\turl = ssh://host/x\n")


@unittest.skipUnless(os.name == "posix" and Path("/bin/sh").exists(), "the fake Docker shim needs /bin/sh")
class PipelineTests(SandboxCase):
    def test_approval_shows_protection_summary_and_run_uses_plan(self):
        self.fixture()
        with fake_docker(self.log):
            result = self.tools.shell("printf ok")
        # Other features may add fields (output-truncation-spill adds sizes and an output id).
        self.assertEqual({k: result[k] for k in ("exit_code", "output", "truncated", "stopped")},
                         {"exit_code": 0, "output": "ok", "truncated": False, "stopped": None})
        self.assertTrue(self.asked[0][1].endswith("\nSandbox: no network, read-only system, 4 secret paths hidden, "
                                                  "6 config paths read-only, 1 git config sanitized"))
        run, cleanup = self.calls()
        container = run[run.index("--name") + 1]
        self.assertEqual(cleanup, ["rm", "-f", container])
        self.assertIn("type=bind,src=/dev/null,dst=/workspace/.env,readonly", run)
        self.assertFalse(sandbox.copies_dir(self.root, container).exists())

    def test_plan_contract(self):
        plan = self.tools._shell_plan("true", 30)
        self.assertIs(plan["protected"], True)
        self.assertEqual(plan["mounts"], [])
        self.assertIsInstance(plan["scan"], sandbox.Scan)

    def test_created_protected_path_is_reported_journaled_and_alerted(self):
        write(self.root, "src/app.py")
        emitted = []

        class Provider:
            model = "test-fixture"

            def __init__(self, commands):
                self.replies = [{"role": "assistant", "content": None, "tool_calls": [
                    {"id": f"call_{i}", "type": "function",
                     "function": {"name": "shell", "arguments": json.dumps({"command": command})}}]}
                    for i, command in enumerate(commands)] + [{"role": "assistant", "content": "done"}]

            def complete(self, messages, tools):
                return self.replies.pop(0), {"total_tokens": 10}
        agent = Agent(Provider(["echo y >> src/app.py", "mkdir .vscode && echo {} > .vscode/tasks.json"]),
                      self.store, self.tools, emitted.append)
        with fake_docker(self.log):
            self.assertEqual(agent.run("go")["status"], "completed")
        events = self.store.events(self.session)
        created = [e["payload"] for e in events if e["kind"] == "sandbox_protected_path_created"]
        self.assertEqual(created, [{"paths": [".vscode"]}])
        prepared = [e["payload"] for e in events if e["kind"] == "sandbox_prepared"]
        self.assertEqual(len(prepared), 2)
        self.assertEqual(set(prepared[0]), {"container", "masked", "read_only", "sanitized_git_config",
                                            "entries_scanned", "seconds"})
        self.assertTrue(prepared[0]["container"].startswith("eira-"))
        self.assertEqual(self.tools.shell_alerts, [".vscode"])
        self.assertIn({"event": "sandbox_protected_path_created", "session": self.session, "paths": [".vscode"]},
                      emitted)
        with fake_docker(self.log):
            result = self.tools.shell("echo z >> src/app.py")
            self.assertNotIn("protected_paths_created", result)
            result = self.tools.shell("mv .vscode .vscode-old && mkdir .vscode")
            self.assertEqual(result["protected_paths_created"], [".vscode"])
            self.assertIn("review them", result["warning"])
            result = self.tools.shell("mkdir -p a/.github/workflows")
            self.assertEqual(result["protected_paths_created"], ["a/.github/workflows"])
        self.assertEqual(self.tools.shell_alerts, [".vscode", ".vscode", "a/.github/workflows"])

    def test_change_during_approval_cancels_the_command(self):
        def approve(name, detail):
            (self.root / ".vscode").mkdir()
            return True
        self.policy.approve = approve
        with fake_docker(self.log), self.assertRaisesRegex(HarnessError, "changed during approval"):
            self.tools.shell("echo no > ran.txt")
        self.assertEqual(self.calls(), [])
        self.assertFalse((self.root / "ran.txt").exists())


class ReviewRequiredWriteTests(SandboxCase):
    def test_protected_config_writes_are_never_preapproved(self):
        self.policy.approve_writes = True
        self.answer = False
        ci = write(self.root, ".github/workflows/ci.yml", "on: push\n")
        app = write(self.root, "src/app.py", "x = 1\n")
        with self.assertRaisesRegex(HarnessError, "denied"):
            self.tools.call("edit_file", {"path": ".github/workflows/ci.yml", "old_string": "push", "new_string": "pr"})
        self.assertEqual(ci.read_text(), "on: push\n")
        self.assertEqual(self.asked[0][0], "edit_file")
        self.tools.call("edit_file", {"path": "src/app.py", "old_string": "1", "new_string": "2"})
        self.assertEqual((app.read_text(), len(self.asked)), ("x = 2\n", 1))
        with self.assertRaisesRegex(HarnessError, "denied"):
            self.tools.call("write_file", {"path": ".vscode/x.json", "content": "{}", "expected_sha256": "new"})
        self.assertEqual(len(self.asked), 2)
        self.assertFalse((self.root / ".vscode" / "x.json").exists())
        self.answer = True
        self.tools.call("write_file", {"path": ".vscode/x.json", "content": "{}", "expected_sha256": "new"})
        self.assertEqual((self.root / ".vscode" / "x.json").read_text(), "{}")


class RenderingTests(unittest.TestCase):
    def test_terminal_and_cli_warn(self):
        output = io.StringIO()
        Terminal(output).emit({"event": "sandbox_protected_path_created", "session": "s", "paths": [".vscode", "AGENTS.md"]})
        self.assertIn("protected config paths: .vscode, AGENTS.md", output.getvalue())
        errors = io.StringIO()
        with redirect_stderr(errors):
            renderer(False)({"event": "sandbox_protected_path_created", "session": "s", "paths": [".husky"]})
        self.assertIn("protected config paths: .husky", errors.getvalue())


if __name__ == "__main__":
    unittest.main()
