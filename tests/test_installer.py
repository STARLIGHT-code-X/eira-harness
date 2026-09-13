from __future__ import annotations

import io
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "install.sh"


def make_archive(source: Path, destination: Path, *, unsafe: str | None = None) -> None:
    with tarfile.open(destination, "w:gz") as archive:
        top = "eira-harness-fixture"
        for path in sorted(source.rglob("*")):
            if ".git" in path.parts or path.name in {"eira_harness.egg-info", "pyproject.toml"}:
                continue
            relative = path.relative_to(source)
            archive.add(path, arcname=f"{top}/{relative}", recursive=False)
        add_text(
            archive,
            f"{top}/pyproject.toml",
            "[build-system]\nrequires = []\nbuild-backend = \"fixture_backend\"\nbackend-path = [\".\"]\n",
        )
        add_text(archive, f"{top}/fixture_backend.py", FIXTURE_BACKEND)
        if unsafe == "traversal":
            info = tarfile.TarInfo(f"{top}/../outside")
            payload = b"unsafe"
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
        elif unsafe == "symlink":
            info = tarfile.TarInfo(f"{top}/escape")
            info.type = tarfile.SYMTYPE
            info.linkname = "/tmp/eira-escape"
            archive.addfile(info)


def add_text(archive: tarfile.TarFile, name: str, text: str) -> None:
    payload = text.encode("utf-8")
    info = tarfile.TarInfo(name)
    info.size = len(payload)
    archive.addfile(info, io.BytesIO(payload))


FIXTURE_BACKEND = r'''
from pathlib import Path
import zipfile


def build_wheel(wheel_directory, config_settings=None, metadata_directory=None):
    name = "eira_harness-0.1.0-py3-none-any.whl"
    dist_info = "eira_harness-0.1.0.dist-info"
    root = Path(__file__).parent
    target = Path(wheel_directory) / name
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as wheel:
        for path in sorted((root / "eira_harness").rglob("*.py")):
            wheel.write(path, path.relative_to(root).as_posix())
        wheel.writestr(dist_info + "/METADATA", "Metadata-Version: 2.1\nName: eira-harness\nVersion: 0.1.0\n\n")
        wheel.writestr(dist_info + "/WHEEL", "Wheel-Version: 1.0\nGenerator: installer-test\nRoot-Is-Purelib: true\nTag: py3-none-any\n")
        wheel.writestr(dist_info + "/entry_points.txt", "[console_scripts]\neira = eira_harness.cli:main\n")
        wheel.writestr(dist_info + "/RECORD", "")
    return name
'''


class InstallerTests(unittest.TestCase):
    def run_installer(self, archive: Path, install: Path, bindir: Path) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env.update(
            EIRA_INSTALL_ARCHIVE=str(archive),
            EIRA_INSTALL_DIR=str(install),
            EIRA_BIN_DIR=str(bindir),
        )
        return subprocess.run(
            ["sh", str(INSTALLER)],
            cwd=ROOT,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

    def test_offline_install_runs_public_launcher(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            temp = Path(temporary)
            archive = temp / "source.tar.gz"
            install = temp / "share" / "eira"
            bindir = temp / "bin"
            make_archive(ROOT, archive)
            result = self.run_installer(archive, install, bindir)
            self.assertEqual(result.returncode, 0, result.stderr)
            version = subprocess.run(
                [str(bindir / "eira"), "--version"],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(version.returncode, 0, version.stderr)
            self.assertIn("Eira ", version.stdout)
            self.assertTrue((install / "current" / "bin" / "python").exists())

    def test_invalid_upgrade_preserves_previous_release(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            temp = Path(temporary)
            archive = temp / "source.tar.gz"
            bad_archive = temp / "bad.tar.gz"
            install = temp / "share" / "eira"
            bindir = temp / "bin"
            make_archive(ROOT, archive)
            first = self.run_installer(archive, install, bindir)
            self.assertEqual(first.returncode, 0, first.stderr)
            current = os.readlink(install / "current")
            make_archive(ROOT, bad_archive, unsafe="traversal")
            failed = self.run_installer(bad_archive, install, bindir)
            self.assertNotEqual(failed.returncode, 0)
            self.assertEqual(os.readlink(install / "current"), current)
            self.assertEqual(subprocess.run([str(bindir / "eira"), "--version"], check=False).returncode, 0)

    def test_successful_upgrade_and_isolated_launcher(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            temp = Path(temporary)
            archive, install, bindir = temp / 'source.tar.gz', temp / 'install', temp / 'bin'
            make_archive(ROOT, archive)
            first = self.run_installer(archive, install, bindir)
            self.assertEqual(first.returncode, 0, first.stderr)
            original = os.readlink(install / 'current')
            second = self.run_installer(archive, install, bindir)
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertNotEqual(os.readlink(install / 'current'), original)
            shadow = temp / 'shadow'; shadow.mkdir()
            (shadow / 'eira_harness.py').write_text('raise RuntimeError("workspace module shadowed installed package")')
            result = subprocess.run([str(bindir / 'eira'), '--version'], cwd=shadow,
                env={**os.environ, 'PYTHONPATH': str(shadow)}, text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('Eira ', result.stdout)

    def test_unmanaged_launcher_is_not_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            temp = Path(temporary)
            archive = temp / "source.tar.gz"
            install = temp / "share" / "eira"
            bindir = temp / "bin"
            bindir.mkdir(parents=True)
            launcher = bindir / "eira"
            launcher.write_text("#!/bin/sh\nexit 99\n", encoding="utf-8")
            make_archive(ROOT, archive)
            result = self.run_installer(archive, install, bindir)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(launcher.read_text(encoding="utf-8"), "#!/bin/sh\nexit 99\n")

    def test_symlink_destination_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            temp = Path(temporary)
            archive = temp / "source.tar.gz"
            real_install = temp / "real-install"
            install = temp / "install-link"
            bindir = temp / "bin"
            real_install.mkdir()
            install.symlink_to(real_install, target_is_directory=True)
            make_archive(ROOT, archive)
            result = self.run_installer(archive, install, bindir)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(list(real_install.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
