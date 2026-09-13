#!/bin/sh
# Install Eira into the current user's local directories.
#
# The release ref is deliberately a full commit, rather than a branch or tag.
# The release process replaces RELEASE_COMMIT after the source commit exists.
set -eu

SOURCE_REF="RELEASE_COMMIT"
REPOSITORY="STARLIGHT-code-X/eira-harness"
SOURCE_URL="https://codeload.github.com/${REPOSITORY}/tar.gz/${SOURCE_REF}"
TMP_ROOT=""
TMP_DOWNLOAD=""

die() {
    printf '%s\n' "Eira installer: $*" >&2
    exit 1
}

[ "$(uname -s 2>/dev/null || true)" != "" ] || die "cannot determine the operating system"
case "$(uname -s 2>/dev/null || true)" in
    Linux|Darwin|FreeBSD|OpenBSD|NetBSD) ;;
    *) die "this installer supports POSIX systems only" ;;
esac

[ -n "${HOME:-}" ] || die "HOME must be set"

# Prefer the newest supported interpreter.  Checking the version by executing
# it also avoids accepting a command named python3 that is too old.
PYTHON=""
for candidate in python3.14 python3.13 python3.12 python3.11 python3; do
    if command -v "$candidate" >/dev/null 2>&1 &&
       "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' >/dev/null 2>&1; then
        PYTHON="$(command -v "$candidate")"
        break
    fi
done
[ -n "$PYTHON" ] || die "Python 3.11 or newer is required (looked for python3.14, python3.13, python3.12, python3.11, python3)"

INSTALL_DIR="${EIRA_INSTALL_DIR:-$HOME/.local/share/eira}"
BIN_DIR="${EIRA_BIN_DIR:-$HOME/.local/bin}"
ARCHIVE_OVERRIDE="${EIRA_INSTALL_ARCHIVE:-}"
[ -n "$INSTALL_DIR" ] || die "EIRA_INSTALL_DIR cannot be empty"
[ -n "$BIN_DIR" ] || die "EIRA_BIN_DIR cannot be empty"

# Make all paths absolute before putting them in the launcher.  This does not
# resolve symlinks: the destination check below intentionally rejects them.
INSTALL_DIR="$("$PYTHON" -I -c 'import os,sys; print(os.path.abspath(sys.argv[1]))' "$INSTALL_DIR")"
BIN_DIR="$("$PYTHON" -I -c 'import os,sys; print(os.path.abspath(sys.argv[1]))' "$BIN_DIR")"

# Destination components are checked before mkdir -p.  A symlink in a custom
# destination would otherwise allow an upgrade to write somewhere unexpected.
"$PYTHON" -I - "$INSTALL_DIR" "$BIN_DIR" <<'PY'
import os
import stat
import sys

for raw in sys.argv[1:]:
    path = os.path.abspath(raw)
    current = path
    while not os.path.lexists(current):
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    if os.path.islink(current):
        raise SystemExit(f"destination contains a symlink: {raw}")
    # Check every existing component, including components between an existing
    # ancestor and the destination itself.
    parts = path.split(os.sep)
    current = os.sep
    for part in parts:
        if not part:
            continue
        current = os.path.join(current, part)
        if os.path.islink(current):
            raise SystemExit(f"destination contains a symlink: {raw}")
PY

if [ -n "$ARCHIVE_OVERRIDE" ]; then
    ARCHIVE="$ARCHIVE_OVERRIDE"
    [ -f "$ARCHIVE" ] || die "EIRA_INSTALL_ARCHIVE is not a regular file: $ARCHIVE"
    [ ! -L "$ARCHIVE" ] || die "EIRA_INSTALL_ARCHIVE must not be a symlink"
else
    # The placeholder is useful for local archive tests while the repository is
    # being prepared.  A network install always requires the release SHA-1.
    case "$SOURCE_REF" in
        RELEASE_COMMIT) die "this installer has no release commit yet" ;;
    esac
    "$PYTHON" -I - "$SOURCE_REF" <<'PY'
import re
import sys
if not re.fullmatch(r"[0-9a-fA-F]{40}", sys.argv[1]):
    raise SystemExit("SOURCE_REF must be a 40-character commit SHA")
PY
    TMP_DOWNLOAD="$(mktemp "${TMPDIR:-/tmp}/eira-download.XXXXXX.tar.gz")" || die "cannot create a temporary download"
    trap 'rm -rf "$TMP_ROOT" "$TMP_DOWNLOAD"' EXIT HUP INT TERM
    curl --fail --silent --show-error --location --proto '=https' --proto-redir '=https' --tlsv1.2 \
        --connect-timeout 10 --max-time 120 --max-filesize 52428800 \
        --output "$TMP_DOWNLOAD" "$SOURCE_URL" || die "download failed"
    ARCHIVE="$TMP_DOWNLOAD"
fi

# Verify and extract the archive before pip sees it.  The validator rejects
# links, traversal, special files, duplicate names, and oversized members.
TMP_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/eira-install.XXXXXX")" || die "cannot create a temporary directory"
if [ -z "${TMP_DOWNLOAD:-}" ]; then
    trap 'rm -rf "$TMP_ROOT"' EXIT HUP INT TERM
else
    trap 'rm -rf "$TMP_ROOT" "$TMP_DOWNLOAD"' EXIT HUP INT TERM
fi
SOURCE_STAGE="$TMP_ROOT/source"
mkdir "$SOURCE_STAGE"

EXTRACT_REF="$SOURCE_REF"
[ -z "$ARCHIVE_OVERRIDE" ] || EXTRACT_REF="local-fixture"
"$PYTHON" -I - "$ARCHIVE" "$SOURCE_STAGE" "$EXTRACT_REF" <<'PY'
from pathlib import Path, PurePosixPath
import os
import re
import shutil
import stat
import sys
import tarfile

archive, output, source_ref = sys.argv[1:]
out = Path(output)
max_members = 10_000
max_unpacked = 100 * 1024 * 1024

try:
    with tarfile.open(archive, "r:gz") as tf:
        members = []
        for member in tf:
            members.append(member)
            if len(members) > max_members:
                raise ValueError("archive contains too many members")
        if not members:
            raise ValueError("archive is empty")
        if len(members) > max_members:
            raise ValueError("archive contains too many members")

        names = set()
        root = None
        unpacked = 0
        for member in members:
            name = member.name
            if not name or "\x00" in name or "\\" in name:
                raise ValueError("archive contains an invalid member name")
            if name.startswith("/"):
                raise ValueError("archive contains an absolute path")
            parts = PurePosixPath(name).parts
            if not parts or parts[0] in ("", ".", "..") or ".." in parts:
                raise ValueError("archive contains path traversal")
            normalized = "/".join(parts).rstrip("/")
            if not normalized or normalized in names:
                raise ValueError("archive contains duplicate member names")
            names.add(normalized)
            member_root = parts[0]
            if root is None:
                root = member_root
            elif member_root != root:
                raise ValueError("archive must contain one top-level directory")
            if member.issym() or member.islnk():
                raise ValueError("archive links are not allowed")
            if not (member.isdir() or member.isreg()):
                raise ValueError("archive contains a special file")
            if member.isreg():
                if member.size < 0 or member.size > max_unpacked - unpacked:
                    raise ValueError("archive is too large after extraction")
                unpacked += member.size

        if root is None or not root.startswith("eira-harness-"):
            raise ValueError("archive has an unexpected top-level directory")
        if re.fullmatch(r"[0-9a-fA-F]{40}", source_ref):
            allowed = {f"eira-harness-{source_ref}", f"eira-harness-{source_ref[:7]}"}
            if root not in allowed:
                raise ValueError("archive root does not match the pinned source commit")
        if "pyproject.toml" not in {name[len(root) + 1:] for name in names if name.startswith(root + "/")}:
            raise ValueError("archive does not contain pyproject.toml")

        extracted_root = out / root
        extracted_root.mkdir()
        for member in members:
            normalized = "/".join(PurePosixPath(member.name).parts).rstrip("/")
            relative = normalized[len(root):].lstrip("/")
            target = extracted_root / relative if relative else extracted_root
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                os.chmod(target, member.mode & 0o755)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            source = tf.extractfile(member)
            if source is None:
                raise ValueError("could not read archive member")
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                with os.fdopen(fd, "wb") as destination:
                    shutil.copyfileobj(source, destination)
            except BaseException:
                try:
                    target.unlink()
                except OSError:
                    pass
                raise
            os.chmod(target, member.mode & 0o755)
except (OSError, tarfile.TarError, ValueError) as exc:
    raise SystemExit(f"unsafe or invalid source archive: {exc}") from exc

print(extracted_root)
PY

SOURCE_ROOT="$SOURCE_STAGE/$(find "$SOURCE_STAGE" -mindepth 1 -maxdepth 1 -type d -print -quit | sed 's#^.*/##')"
[ -f "$SOURCE_ROOT/pyproject.toml" ] || die "source archive extraction failed"

# Check the install container and the launcher before doing any work that can
# affect an existing release.
if [ -L "$INSTALL_DIR" ]; then
    die "EIRA_INSTALL_DIR must not be a symlink"
elif [ -e "$INSTALL_DIR" ]; then
    [ -d "$INSTALL_DIR" ] || die "EIRA_INSTALL_DIR is not a directory"
else
    mkdir -p "$INSTALL_DIR"
fi
MARKER="$INSTALL_DIR/.eira-managed"
NEW_INSTALL=0
if [ -L "$MARKER" ]; then
    die "managed-install marker is a symlink"
elif [ -e "$MARKER" ]; then
    [ -f "$MARKER" ] || die "managed-install marker is not a regular file"
    [ "$(sed -n '1p' "$MARKER")" = "eira-harness managed install v1" ] || die "existing install is unmanaged"
else
    if find "$INSTALL_DIR" -mindepth 1 -maxdepth 1 -print -quit | grep -q .; then
        die "EIRA_INSTALL_DIR contains an unmanaged installation"
    fi
    NEW_INSTALL=1
fi

if [ -L "$BIN_DIR" ]; then
    die "EIRA_BIN_DIR must not be a symlink"
elif [ -e "$BIN_DIR" ]; then
    [ -d "$BIN_DIR" ] || die "EIRA_BIN_DIR is not a directory"
else
    mkdir -p "$BIN_DIR"
fi
LAUNCHER="$BIN_DIR/eira"
if [ -L "$LAUNCHER" ]; then
    die "existing launcher is a symlink and will not be overwritten"
elif [ -e "$LAUNCHER" ]; then
    [ -f "$LAUNCHER" ] || die "existing launcher is not a regular file"
    [ "$(sed -n '2p' "$LAUNCHER")" = "# EIRA_MANAGED_LAUNCHER v1" ] || die "existing launcher is unmanaged"
fi

RELEASES="$INSTALL_DIR/releases"
if [ -L "$RELEASES" ]; then
    die "releases directory is a symlink"
elif [ -e "$RELEASES" ]; then
    [ -d "$RELEASES" ] || die "releases path is not a directory"
elif [ "$NEW_INSTALL" -eq 0 ]; then
    mkdir "$RELEASES"
fi

CURRENT="$INSTALL_DIR/current"
if [ -L "$CURRENT" ]; then
    [ -d "$RELEASES" ] || die "releases directory is missing"
    CURRENT_TARGET="$(readlink "$CURRENT")"
    case "$CURRENT_TARGET" in
        releases/*) ;;
        *) die "current release points outside the managed install" ;;
    esac
    CURRENT_RELEASE="${CURRENT_TARGET#releases/}"
    case "$CURRENT_RELEASE" in
        ""|.*|*/*|*..*) die "current release has an unsafe target" ;;
    esac
    [ ! -L "$RELEASES/$CURRENT_RELEASE" ] || die "current release points through a symlink"
    [ -d "$RELEASES/$CURRENT_RELEASE" ] || die "current release is missing"
elif [ -e "$CURRENT" ]; then
    die "current release is not a managed symlink"
fi

if [ "$NEW_INSTALL" -eq 1 ]; then
    "$PYTHON" -I - "$MARKER" <<'PY'
import os
import sys
fd = os.open(sys.argv[1], os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
with os.fdopen(fd, "w", encoding="utf-8") as marker:
    marker.write("eira-harness managed install v1\n")
PY
fi

STAGE_INSTALL="$TMP_ROOT/install"
STAGE_VENV="$STAGE_INSTALL/venv"
mkdir -p "$STAGE_INSTALL"
printf '%s\n' "Creating an isolated Python environment with $PYTHON" >&2
"$PYTHON" -I -m venv "$STAGE_VENV" || die "could not create a Python virtual environment"
VENV_PY="$STAGE_VENV/bin/python"
"$VENV_PY" -I -m pip install --disable-pip-version-check --no-input --no-deps "$SOURCE_ROOT" || die "package installation failed; existing Eira was preserved"
"$VENV_PY" -I -c 'import eira_harness' || die "installed package could not be imported"

# Build the launcher before committing the release.  It always invokes the
# persistent environment through -m, so venv-generated scripts with staging
# shebangs are never exposed as the public command.
LAUNCHER_TMP="$BIN_DIR/.eira-launcher.$$"
[ ! -e "$LAUNCHER_TMP" ] && [ ! -L "$LAUNCHER_TMP" ] || die "temporary launcher already exists"
"$PYTHON" -I - "$LAUNCHER_TMP" "$CURRENT/bin/python" <<'PY'
import os
import shlex
import sys

target = sys.argv[2]
path = sys.argv[1]
fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o755)
with os.fdopen(fd, "w", encoding="utf-8") as f:
    f.write("#!/bin/sh\n# EIRA_MANAGED_LAUNCHER v1\nset -eu\nPYTHONSAFEPATH=1 exec " + shlex.quote(target) + " -I -m eira_harness \"$@\"\n")
os.chmod(path, 0o755)
PY

if [ -L "$RELEASES" ]; then
    die "releases directory is a symlink"
elif [ -e "$RELEASES" ]; then
    [ -d "$RELEASES" ] || die "releases path is not a directory"
else
    mkdir "$RELEASES"
fi

release_id="release-$(date +%s)-$$"
release_path="$RELEASES/$release_id"
[ ! -e "$release_path" ] && [ ! -L "$release_path" ] || die "release path already exists"
mv "$STAGE_VENV" "$release_path" || die "could not stage the new release"

CURRENT_TMP="$INSTALL_DIR/.current.$$"
[ ! -e "$CURRENT_TMP" ] && [ ! -L "$CURRENT_TMP" ] || die "temporary current link already exists"
ln -s "releases/$release_id" "$CURRENT_TMP"
"$PYTHON" -I - "$CURRENT_TMP" "$CURRENT" <<'PY'
import os
import sys
os.replace(sys.argv[1], sys.argv[2])
PY
"$PYTHON" -I - "$LAUNCHER_TMP" "$LAUNCHER" <<'PY'
import os
import sys
os.replace(sys.argv[1], sys.argv[2])
PY

printf '%s\n' "Eira installed. Run: $LAUNCHER --version" >&2
