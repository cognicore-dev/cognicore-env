"""
Find the repository a folder belongs to, without running git.

Hooks run on every turn and must never hang, so this only reads the file
system: it walks up from the folder to the nearest ``.git`` (a folder, or a
file for worktrees and submodules). A repository at the home folder (a
dotfiles repo) doesn't count for the folders inside it, or every project
without its own repository would collapse into one.

``main_worktree=True`` maps a linked worktree (``git worktree add``) to the
main checkout, so every worktree of one repository is one project.
"""

import os
from pathlib import Path
from typing import Optional


def _home() -> Optional[Path]:
    try:
        return Path.home().resolve()
    except (RuntimeError, OSError):
        return None


def _main_checkout(worktree: Path, dot_git: Path) -> Path:
    """For a linked worktree, the main checkout's folder; otherwise *worktree* unchanged."""
    try:
        line = dot_git.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return worktree
    if not line.lower().startswith("gitdir:"):
        return worktree
    gitdir = Path(line[len("gitdir:"):].strip())
    if not gitdir.is_absolute():
        gitdir = worktree / gitdir
    try:
        common_ref = (gitdir / "commondir").read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return worktree  # a submodule (no commondir): it is its own project
    common = Path(common_ref)
    if not common.is_absolute():
        common = gitdir / common
    try:
        common = common.resolve()
    except OSError:
        return worktree
    if common.name == ".git" and common.parent.is_dir():
        return common.parent
    return worktree  # worktree of a bare repository: no main checkout to point at


def repo_root(path: Optional[str] = None, main_worktree: bool = False) -> Optional[Path]:
    """The top-level folder of the repository containing *path*, or None outside any repository."""
    try:
        start = Path(os.path.expanduser(path or os.getcwd())).resolve()
    except (OSError, RuntimeError, ValueError):
        return None
    home = _home()
    for folder in (start, *start.parents):
        if folder == home and folder != start:
            return None
        dot_git = folder / ".git"
        try:
            if dot_git.is_dir():
                return folder
            if dot_git.is_file():
                return _main_checkout(folder, dot_git) if main_worktree else folder
        except OSError:
            return None
    return None


def project_folder(path: Optional[str] = None) -> Path:
    """The folder that names a project: the repository's main checkout, else *path* itself."""
    found = repo_root(path, main_worktree=True)
    if found is not None:
        return found
    try:
        return Path(os.path.expanduser(path or os.getcwd())).resolve()
    except (OSError, RuntimeError, ValueError):
        return Path(path or ".")
