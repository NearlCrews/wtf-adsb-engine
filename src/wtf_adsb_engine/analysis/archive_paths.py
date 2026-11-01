"""Private paths shared by standalone archive maintenance commands."""

import os
import stat
import subprocess
from contextlib import contextmanager
from pathlib import Path


def destination(path):
    """Resolve a private artifact target, refusing installed code and Git sources."""
    path = Path(path).expanduser()
    if path.is_symlink():
        raise ValueError("Archive output must not be a symbolic link")
    path = path.resolve()
    package = Path(__file__).resolve().parents[1]
    if path == path.parent or path.is_relative_to(package):
        raise ValueError("Choose a dedicated private archive output directory")
    existing = path if path.is_dir() else path.parent
    while not existing.exists():
        existing = existing.parent
    try:
        repository = subprocess.run(
            ["git", "-C", str(existing), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except FileNotFoundError:
        if any((parent / ".git").exists() for parent in (existing, *existing.parents)):
            raise ValueError("Git is required to check archive output inside a worktree") from None
        return path
    if repository.returncode:
        if any((parent / ".git").exists() for parent in (existing, *existing.parents)):
            raise ValueError("Git could not verify that this archive output is ignored")
        return path
    root = Path(repository.stdout.strip()).resolve()
    if not path.is_relative_to(root):
        return path
    relative = str(path.relative_to(root))
    tracked = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z", "--", relative],
        capture_output=True,
        timeout=10,
        check=False,
    )
    ignored = subprocess.run(
        ["git", "-C", str(root), "check-ignore", "-q", "--", relative],
        capture_output=True,
        timeout=10,
        check=False,
    )
    if tracked.returncode or tracked.stdout or ignored.returncode:
        raise ValueError("Archive output must be outside a Git worktree or in an ignored private directory")
    return path


def same_file(path, other):
    """Also reject hard links when an operator supplies the active journal path."""
    if other is None:
        return False
    path, other = Path(path).expanduser(), Path(other).expanduser()
    if path.resolve() == other.resolve():
        return True
    try:
        return path.samefile(other)
    except OSError:
        return False


def journal_backup(path, *, live=None):
    """Validate a user-supplied backup for in-place private archive analysis."""
    path = destination(path)
    if same_file(path, live):
        raise ValueError("Use a separate SQLite backup copy, not the active collector journal")
    if not path.is_file():
        raise ValueError("--database must be an existing SQLite backup of the collector journal")
    if stat.S_IMODE(path.parent.stat().st_mode) & 0o077:
        raise ValueError("The backup must be stored in a private directory with mode 0700")
    path.chmod(0o600)
    return path


@contextmanager
def private_umask():
    previous = os.umask(0o077)
    try:
        yield
    finally:
        os.umask(previous)
