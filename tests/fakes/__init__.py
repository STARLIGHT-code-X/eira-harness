"""Test doubles that keep the suite offline."""
from contextlib import contextmanager
import os
from pathlib import Path
import shlex
import sys
import tempfile

DIRECTORY = Path(__file__).resolve().parent


@contextmanager
def fake_docker(log_path=None):
    """Put the fake docker shim first on PATH for the block, logging each argv to log_path.

    Toolbox runs docker with a minimal environment, so with a log path the
    directory prepended is a private one whose `docker` sets
    EIRA_FAKE_DOCKER_LOG itself and execs the shim.
    """
    saved = {key: os.environ.get(key) for key in ("PATH", "EIRA_FAKE_DOCKER_LOG")}
    with tempfile.TemporaryDirectory(prefix="eira-fake-docker-") as private:
        directory = DIRECTORY
        if log_path is not None:
            directory = Path(private)
            wrapper = directory / "docker"
            wrapper.write_text(f"#!/bin/sh\nEIRA_FAKE_DOCKER_LOG={shlex.quote(str(log_path))} "
                               f"exec {shlex.quote(sys.executable)} {shlex.quote(str(DIRECTORY / 'docker'))} \"$@\"\n")
            wrapper.chmod(0o700)
            os.environ["EIRA_FAKE_DOCKER_LOG"] = str(log_path)
        else:
            os.environ.pop("EIRA_FAKE_DOCKER_LOG", None)
        os.environ["PATH"] = str(directory) + os.pathsep + os.environ.get("PATH", "/usr/bin:/bin")
        try:
            yield directory / "docker"
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
