"""Advisory "this path is version-control ignored" warning.

Warn-only: Filekeeper still performs the write, but the result carries a warning
so an agent can notice that a file it just wrote will not be committed. Git is
consulted through an argument list (never a shell) with a short timeout, and any
failure degrades to "unknown" rather than an error.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

_TIMEOUT_SECONDS = 3.0


def _inside_git_worktree(root: Path, target: Path) -> bool:
    for candidate in (root, *root.parents):
        if (candidate / ".git").exists():
            return True
    return False


def git_ignored(root: Path, target: Path) -> bool | None:
    """True/False when git can answer, ``None`` when it cannot or does not apply."""

    if not _inside_git_worktree(root, target):
        return None
    try:
        completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["git", "-C", str(root), "check-ignore", "--quiet", "--", str(target)],
            capture_output=True,
            timeout=_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode == 0:
        return True
    if completed.returncode == 1:
        return False
    return None


def ignore_warning(root: Path, target: Path, relative_path: str) -> str | None:
    if git_ignored(root, target) is True:
        return (
            f"{relative_path} is ignored by version control; this change will not "
            "appear in the repository"
        )
    return None
