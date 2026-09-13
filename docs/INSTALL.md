# Installing Eira

Eira installs into the current user's local directories and does not require
`sudo`:

```sh
curl -fsSL https://raw.githubusercontent.com/STARLIGHT-code-X/eira-harness/main/install.sh | sh
```

The installer downloads a tarball for a full, immutable source commit, checks
the archive before extraction, creates a private virtual environment, and
activates a release only after the package has installed and imported
successfully. The public `eira` launcher executes the persistent environment
with `python -m eira_harness`.

Python 3.11 or newer is required. The installer looks for `python3.14`,
`python3.13`, `python3.12`, `python3.11`, and then `python3` in that order.

By default, the managed environment is stored in `~/.local/share/eira` and the
launcher is written to `~/.local/bin/eira`. Use these variables for an isolated
installation or a package manager sandbox:

```sh
EIRA_INSTALL_DIR="$PWD/.eira-install" \
EIRA_BIN_DIR="$PWD/.eira-bin" \
  sh install.sh
```

An existing managed installation is upgraded atomically. An existing
installation directory or launcher that is not marked as managed is left
untouched and causes the installer to stop. Destination symlinks are rejected.

For offline testing, set `EIRA_INSTALL_ARCHIVE` to a local gzip tar archive
whose single top-level directory starts with `eira-harness-`:

```sh
EIRA_INSTALL_ARCHIVE=/path/to/eira-harness-fixture.tar.gz \
EIRA_INSTALL_DIR="$PWD/.eira-install" \
EIRA_BIN_DIR="$PWD/.eira-bin" \
  sh install.sh
```

The release script contains `SOURCE_REF`, a 40-character commit SHA. A release
build replaces its `RELEASE_COMMIT` placeholder before publishing the script.
