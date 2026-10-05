"""Real-daemon checks for Docker shell mode.

Skipped unless EIRA_DOCKER_IMAGE names a pre-pulled image with /bin/sh and
python, for example:

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


@unittest.skipUnless(daemon_ready(), "set EIRA_DOCKER_IMAGE to a pre-pulled image to run Docker integration tests")
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
                         "print([l.split()[1] for l in open('/proc/self/status') if l.startswith('CapEff')][0])\n"
                         "try:\n"
                         "    open('/usr/eira-test', 'w')\n"
                         "    print('root: writable')\n"
                         "except OSError:\n"
                         "    print('root: read-only')\n"
                         "EOF")
        self.assertEqual(result["exit_code"], 0, result)
        self.assertEqual(result["output"].split("\n")[:3], ["network: blocked", "0000000000000000", "root: read-only"])

    def test_eira_state_is_hidden_from_commands(self):
        self.assertTrue((self.root / ".eira" / "state.db").exists())
        result = self.sh("ls -A /workspace/.eira | wc -l")
        self.assertEqual(result["output"].strip(), "0")

    def test_timeout_and_output_limits_stop_and_remove_container(self):
        result = self.sh("sleep 20", timeout=2)
        self.assertEqual(result["stopped"], "timeout")
        result = self.sh("python -c \"import sys; sys.stdout.write('x' * 2000000)\"")
        self.assertEqual(result["stopped"], "output_limit")
        self.assertTrue(result["truncated"])
        leftovers = subprocess.run(["docker", "ps", "-aq", "--filter", "name=eira-"], capture_output=True, text=True)
        self.assertEqual(leftovers.stdout.strip(), "")

    def test_denied_command_never_starts_a_container(self):
        self.tools.policy.approve = lambda name, detail: False
        with self.assertRaises(HarnessError):
            self.sh("echo should-not-run > denied.txt")
        self.assertFalse((self.root / "denied.txt").exists())


if __name__ == "__main__":
    unittest.main()
