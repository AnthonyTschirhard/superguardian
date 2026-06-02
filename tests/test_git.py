"""Tests for git.py — repo scanning and push helpers."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from superguardian.git import RepoStatus, scan_repo

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _git(path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(path), *args],
        capture_output=True, text=True, check=True,
    )


def _init_repo(path: Path, *, bare: bool = False) -> None:
    args = ["git", "init", str(path)]
    if bare:
        args.append("--bare")
    subprocess.run(args, capture_output=True, check=True)
    if not bare:
        _git(path, "config", "user.email", "test@example.com")
        _git(path, "config", "user.name", "Test")


def _commit(path: Path, message: str = "init") -> None:
    (path / "file.txt").write_text(message)
    _git(path, "add", ".")
    _git(path, "commit", "-m", message)


# ---------------------------------------------------------------------------
# scan_repo — not a git repo
# ---------------------------------------------------------------------------

def test_scan_non_git_dir(tmp_path):
    result = scan_repo(str(tmp_path))
    assert result.error is not None
    assert "not a git repository" in result.error
    assert result.name == tmp_path.name


# ---------------------------------------------------------------------------
# scan_repo — normal repo, no commits
# ---------------------------------------------------------------------------

def test_scan_empty_normal_repo(tmp_path):
    repo = tmp_path / "myrepo"
    repo.mkdir()
    _init_repo(repo)
    result = scan_repo(str(repo))
    assert result.error is None
    assert result.name == "myrepo"
    assert result.is_bare is False
    assert result.remotes == []
    assert result.total_unpushed == 0


# ---------------------------------------------------------------------------
# scan_repo — bare repo
# ---------------------------------------------------------------------------

def test_scan_bare_repo(tmp_path):
    repo = tmp_path / "myrepo.git"
    _init_repo(repo, bare=True)
    result = scan_repo(str(repo))
    assert result.error is None
    assert result.is_bare is True
    assert result.dirty is False
    assert result.current_branch is None


# ---------------------------------------------------------------------------
# scan_repo — dirty working tree
# ---------------------------------------------------------------------------

def test_scan_dirty_repo(tmp_path):
    repo = tmp_path / "dirty"
    repo.mkdir()
    _init_repo(repo)
    _commit(repo)
    (repo / "untracked.txt").write_text("hello")
    result = scan_repo(str(repo))
    assert result.error is None
    assert result.dirty is True


def test_scan_clean_repo(tmp_path):
    repo = tmp_path / "clean"
    repo.mkdir()
    _init_repo(repo)
    _commit(repo)
    result = scan_repo(str(repo))
    assert result.error is None
    assert result.dirty is False


# ---------------------------------------------------------------------------
# scan_repo — unpushed commits
# ---------------------------------------------------------------------------

def _make_remote_with_commit(tmp_path: Path) -> Path:
    """Create a bare repo seeded with an initial commit so clones get tracking refs."""
    src = tmp_path / "_seed"
    src.mkdir()
    _init_repo(src)
    _commit(src, "initial")
    remote = tmp_path / "remote.git"
    subprocess.run(
        ["git", "clone", "--bare", str(src), str(remote)],
        capture_output=True, check=True,
    )
    return remote


def test_scan_unpushed_commits(tmp_path):
    remote = _make_remote_with_commit(tmp_path)

    # Clone and make one commit that is not yet pushed
    clone = tmp_path / "clone"
    subprocess.run(
        ["git", "clone", str(remote), str(clone)],
        capture_output=True, check=True,
    )
    _git(clone, "config", "user.email", "test@example.com")
    _git(clone, "config", "user.name", "Test")
    _commit(clone, "unpushed work")

    result = scan_repo(str(clone))
    assert result.error is None
    assert result.remotes == ["origin"]
    assert result.total_unpushed == 1
    branch = result.current_branch
    assert branch is not None
    assert result.unpushed[branch] == 1


def test_scan_pushed_commits_shows_zero_unpushed(tmp_path):
    remote = _make_remote_with_commit(tmp_path)

    clone = tmp_path / "clone"
    subprocess.run(
        ["git", "clone", str(remote), str(clone)],
        capture_output=True, check=True,
    )
    _git(clone, "config", "user.email", "test@example.com")
    _git(clone, "config", "user.name", "Test")
    _commit(clone, "pushed work")
    _git(clone, "push", "origin", "HEAD")

    result = scan_repo(str(clone))
    assert result.error is None
    assert result.total_unpushed == 0


# ---------------------------------------------------------------------------
# RepoStatus.total_unpushed
# ---------------------------------------------------------------------------

def test_total_unpushed_sums_branches():
    r = RepoStatus(
        path="/x", name="x", is_bare=False, current_branch="main",
        dirty=False, unpushed={"main": 3, "feat": 5}, remotes=["origin"],
    )
    assert r.total_unpushed == 8


def test_total_unpushed_empty():
    r = RepoStatus(
        path="/x", name="x", is_bare=False, current_branch="main",
        dirty=False, unpushed={}, remotes=["origin"],
    )
    assert r.total_unpushed == 0
