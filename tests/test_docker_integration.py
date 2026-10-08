"""Real-daemon checks for Docker shell mode.

Skipped unless EIRA_DOCKER_IMAGE names a pre-pulled image with /bin/sh and
python. Set EIRA_REQUIRE_DOCKER=1 (as CI does) to fail instead of skipping
when the daemon or image is unavailable. For example:

    EIRA_DOCKER_IMAGE=python:3.11-slim python3 -m unittest tests.test_docker_integration -v
"""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from eira_harness.store import Store
from eira_harness.security import HarnessError, Workspace
from eira_harness.tools import Policy, Toolbox

IMAGE = os.environ.get("EIRA_DOCKER_IMAGE", "")


def daemon_ready() -> bool:
    if not IMAGE or not shutil.which("docker"):
        return False
    probe = subprocess.run(["docker", "image", "inspect", IMAGE], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return probe.returncode == 0


READY = daemon_ready()
REQUIRED = os.environ.get("EIRA_REQUIRE_DOCKER") == "1"


def eira_containers() -> set[str]:
    listed = subprocess.run(["docker", "ps", "-aq", "--filter", "name=^eira-"], capture_output=True, text=True)
    return set(listed.stdout.split())


class DockerRequirementTest(unittest.TestCase):
    @unittest.skipUnless(REQUIRED, "EIRA_REQUIRE_DOCKER is not set")
    def test_required_docker_is_available(self):
        self.assertTrue(READY, f"EIRA_REQUIRE_DOCKER=1 but Docker or image {IMAGE!r} is unavailable")


@unittest.skipUnless(READY, "set EIRA_DOCKER_IMAGE to a pre-pulled image to run Docker integration tests")
class DockerShellIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root)
        self.approved = []
        policy = Policy(approve=lambda name, detail: self.approved.append(detail) or True,
                        shell_mode="docker", docker_image=IMAGE)
        self.tools = Toolbox(Workspace(self.root), self.store, policy, self.store.create("docker"))

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def sh(self, command, timeout=30):
        return self.tools.call("shell", {"command": command, "timeout": timeout})

    def test_command_runs_in_workspace_and_can_write_project_files(self):
        result = self.sh("python -c 'print(6 * 7)' && pwd && echo made > out.txt")
        self.assertEqual(result["exit_code"], 0, result)
        self.assertEqual(result["output"].split(), ["42", "/workspace"])
        self.assertEqual((self.root / "out.txt").read_text(), "made\n")
        self.assertIn("Mode: docker", self.approved[0])

    def test_container_has_no_network_capabilities_or_writable_root(self):
        result = self.sh("python - <<'EOF'\n"
                         "import socket\n"
                         "try:\n"
                         "    socket.create_connection(('1.1.1.1', 443), timeout=3)\n"
                         "    print('network: open')\n"
                         "except OSError:\n"
                         "    print('network: blocked')\n"
                         "status = dict(l.split(':', 1) for l in open('/proc/self/status'))\n"
                         "print(status['CapEff'].strip(), status['CapBnd'].strip())\n"
                         "root = [l.split()[3].split(',') for l in open('/proc/mounts') if l.split()[1] == '/']\n"
                         "print('root:', 'ro' if 'ro' in root[-1] else 'rw')\n"
                         "EOF")
        self.assertEqual(result["exit_code"], 0, result)
        # The bounding set and mount flags hold regardless of the container
        # user, so these checks are meaningful for root and non-root runs.
        self.assertEqual(result["output"].split("\n")[:3],
                         ["network: blocked", "0000000000000000 0000000000000000", "root: ro"])

    def test_eira_state_is_hidden_from_commands(self):
        self.assertTrue((self.root / ".eira" / "state.db").exists())
        # The tmpfs over .eira is root-owned with mode 0700: empty to a root
        # container user, unlistable to anyone else. Either way the host's
        # journal must be neither visible nor readable.
        result = self.sh("if test -e .eira/state.db; then echo visible; else echo hidden; fi; "
                         "if cat .eira/state.db >/dev/null 2>&1; then echo readable; else echo unreadable; fi")
        self.assertEqual(result["output"].split(), ["hidden", "unreadable"])

    def test_timeout_and_output_limits_stop_and_remove_container(self):
        before = eira_containers()
        result = self.sh("sleep 20", timeout=2)
        self.assertEqual(result["stopped"], "timeout")
        result = self.sh("python -c \"import sys; sys.stdout.write('x' * 2000000)\"")
        self.assertEqual(result["stopped"], "output_limit")
        self.assertTrue(result["truncated"])
        self.assertEqual(eira_containers() - before, set())

    def test_denied_command_never_starts_a_container(self):
        self.tools.policy.approve = lambda name, detail: False
        with self.assertRaises(HarnessError):
            self.sh("echo should-not-run > denied.txt")
        self.assertFalse((self.root / "denied.txt").exists())


if __name__ == "__main__":
    unittest.main()
