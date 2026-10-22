"""Picks the right prebuilt engine for this machine and makes it importable.

The distribution ships one compiled engine per platform under ``engine/``. This
module works out which one belongs to the interpreter that is running, puts the
command-line binary on PATH and copies the extension module into the ``soccer``
package where Python will find it.

Everything here is standard library and runs before anything imports ``soccer``.
There is no build step, no virtual environment and nothing to install: the point
of the distribution is that a student unzips it and it runs.
"""

from __future__ import annotations

import os
import platform
import shutil
import struct
import sys
from pathlib import Path

#: Where the distribution keeps its per-platform binaries.
ENGINE_DIR = "engine"

#: The package the extension module has to end up inside.
PACKAGE = Path("student-sdk") / "soccer"


class BootstrapError(Exception):
    """A problem the student can act on, reported as a plain message."""


def _machine() -> str:
    """This CPU architecture, under one name per architecture.

    Every platform spells the same two architectures differently — Windows says
    AMD64, Linux says x86_64, macOS says arm64 where Linux says aarch64 — so the
    directory names in the zip would be unguessable without normalising first.
    """
    raw = platform.machine().lower()
    if raw in ("amd64", "x86_64", "x64"):
        return "x86_64"
    if raw in ("arm64", "aarch64"):
        return "arm64"
    return raw


def platform_slot() -> str:
    """The ``engine/`` subdirectory this interpreter needs."""
    system = platform.system().lower()
    if system == "darwin":
        system = "macos"
    elif system.startswith("win"):
        system = "windows"
    return f"{system}-{_machine()}"


def _check_interpreter() -> None:
    """Rejects interpreters the prebuilt engine cannot possibly load.

    Both of these otherwise surface as an ImportError naming a file that is
    plainly right there, which is a genuinely baffling thing to debug.
    """
    if sys.version_info < (3, 8):
        raise BootstrapError(
            "Python {}.{} is too old; this needs Python 3.8 or newer.\n"
            "Install it from https://www.python.org/downloads/".format(
                *sys.version_info[:2]
            )
        )
    if struct.calcsize("P") * 8 != 64:
        raise BootstrapError(
            "This is a 32-bit Python, and the engine is built 64-bit.\n"
            "Install the 64-bit build from https://www.python.org/downloads/"
        )


def _describe_available(engine_root: Path) -> str:
    if not engine_root.is_dir():
        return "none — the engine/ directory is missing from this download"
    names = sorted(p.name for p in engine_root.iterdir() if p.is_dir())
    return ", ".join(names) if names else "none"


def _copy_if_stale(source: Path, destination: Path) -> bool:
    """Copies only when the destination differs, and reports whether it did.

    Size and mtime rather than a hash: the file is a couple of megabytes and
    this runs on every start, and the only writer is this function.
    """
    if destination.exists():
        old, new = destination.stat(), source.stat()
        if old.st_size == new.st_size and int(old.st_mtime) >= int(new.st_mtime):
            return False
    try:
        shutil.copy2(source, destination)
    except PermissionError:
        raise BootstrapError(
            "Could not update the engine at {}.\n"
            "Something is still using it — close any dashboard, match or Python\n"
            "prompt from this folder, then try again.".format(destination)
        ) from None
    return True


def ensure_engine(root: Path | str | None = None) -> dict:
    """Prepares this machine's engine and returns what was set up.

    Safe to call more than once: the copy is skipped when it is already current.
    """
    _check_interpreter()

    root = Path(root or Path(__file__).resolve().parent)
    slot = platform_slot()
    engine_root = root / ENGINE_DIR
    source_dir = engine_root / slot

    if not source_dir.is_dir():
        raise BootstrapError(
            "No engine bundled for this machine ({}).\n"
            "This download contains: {}\n"
            "Ask for a build for your platform, or build from the full "
            "repository.".format(slot, _describe_available(engine_root))
        )

    # The extension module is named by the platform that produced it
    # (_engine.pyd on Windows, _engine.abi3.so elsewhere), so it is found
    # rather than assumed.
    extensions = sorted(
        p for p in source_dir.iterdir() if p.name.startswith("_engine.")
    )
    if not extensions:
        raise BootstrapError(
            "The engine for {} is incomplete: no _engine module in {}.".format(
                slot, source_dir
            )
        )

    package = root / PACKAGE
    if not package.is_dir():
        raise BootstrapError(
            "The soccer package is missing from this download (expected {}).\n"
            "Unzip the whole archive, keeping its folder structure.".format(package)
        )

    extension = extensions[0]
    copied = _copy_if_stale(extension, package / extension.name)

    # The command-line engine is found through PATH rather than copied, so
    # there is exactly one of it on disk. Zip files do not carry the executable
    # bit, so it is restored here on the platforms that need one.
    cli = None
    for name in ("soccer-cli.exe", "soccer-cli"):
        candidate = source_dir / name
        if candidate.exists():
            cli = candidate
            break
    if cli is None:
        raise BootstrapError(
            "The engine for {} is incomplete: no soccer-cli in {}.".format(
                slot, source_dir
            )
        )
    if os.name != "nt" and not os.access(cli, os.X_OK):
        cli.chmod(cli.stat().st_mode | 0o111)

    path = os.environ.get("PATH", "")
    if str(source_dir) not in path.split(os.pathsep):
        os.environ["PATH"] = str(source_dir) + os.pathsep + path

    # Importing `soccer` from anywhere, including the worker processes the
    # launcher spawns for student teams.
    sdk = str(root / "student-sdk")
    if sdk not in sys.path:
        sys.path.insert(0, sdk)
    existing = os.environ.get("PYTHONPATH", "")
    if sdk not in existing.split(os.pathsep):
        os.environ["PYTHONPATH"] = (
            sdk + os.pathsep + existing if existing else sdk
        )

    return {
        "slot": slot,
        "engine_dir": source_dir,
        "extension": package / extension.name,
        "cli": cli,
        "refreshed": copied,
    }
