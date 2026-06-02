"""Git repository scanning and push operations."""
from __future__ import annotations

import asyncio
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path


@dataclass
class RepoStatus:
    path: str
    name: str
    is_bare: bool
    current_branch: str | None   # None for bare repos or detached HEAD
    dirty: bool                  # uncommitted changes
    unpushed: dict[str, int]     # branch → commit count ahead of upstream
    remotes: list[str]
    error: str | None = None

    @property
    def total_unpushed(self) -> int:
        return sum(self.unpushed.values())


def scan_repo(path: str, worktree: str | None = None) -> RepoStatus:
    """Scan a single git repo (normal or bare) and return its status.

    worktree: explicit work-tree path for bare repos that manage a live directory
    (e.g. a dotfiles bare repo whose work-tree is $HOME). Falls back to reading
    core.worktree from the repo's own git config when not supplied.
    """
    p = Path(path).expanduser().resolve()
    name = p.name

    def _git(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", str(p), *args],
            capture_output=True, text=True, timeout=10,
        )

    def _git_wt(wt: str, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", str(p), "--work-tree", wt, *args],
            capture_output=True, text=True, timeout=10,
        )

    if _git("rev-parse", "--git-dir").returncode != 0:
        return RepoStatus(
            path=str(p), name=name, is_bare=False, current_branch=None,
            dirty=False, unpushed={}, remotes=[],
            error="not a git repository",
        )

    is_bare = _git("rev-parse", "--is-bare-repository").stdout.strip() == "true"

    # Resolve effective work-tree for bare repos (dotfiles pattern)
    effective_wt: str | None = worktree
    if is_bare and effective_wt is None:
        wt_cfg = _git("config", "--get", "core.worktree")
        if wt_cfg.returncode == 0 and wt_cfg.stdout.strip():
            effective_wt = wt_cfg.stdout.strip()

    remotes = [r for r in _git("remote").stdout.splitlines() if r]

    current_branch: str | None = None
    dirty = False

    if not is_bare:
        branch_out = _git("symbolic-ref", "--short", "HEAD")
        if branch_out.returncode == 0:
            current_branch = branch_out.stdout.strip()
        dirty = bool(_git("status", "--porcelain").stdout.strip())
    elif effective_wt is not None:
        branch_out = _git_wt(effective_wt, "symbolic-ref", "--short", "HEAD")
        if branch_out.returncode == 0:
            current_branch = branch_out.stdout.strip()
        dirty = bool(_git_wt(effective_wt, "status", "--porcelain").stdout.strip())

    unpushed: dict[str, int] = {}
    if remotes:
        ref_out = _git(
            "for-each-ref",
            "--format=%(refname:short) %(upstream:short)",
            "refs/heads/",
        )
        for line in ref_out.stdout.splitlines():
            parts = line.strip().split(" ", 1)
            if len(parts) != 2 or not parts[1].strip():
                continue
            branch, upstream = parts[0], parts[1].strip()
            count_out = _git("rev-list", "--count", f"{upstream}..{branch}")
            if count_out.returncode == 0:
                try:
                    count = int(count_out.stdout.strip())
                    if count > 0:
                        unpushed[branch] = count
                except ValueError:
                    pass

    return RepoStatus(
        path=str(p),
        name=name,
        is_bare=is_bare,
        current_branch=current_branch,
        dirty=dirty,
        unpushed=unpushed,
        remotes=remotes,
    )


async def push_all_branches(path: str, on_line: Callable[[str], None]) -> bool:
    """Push all local branches to every configured remote. Returns True on full success."""
    p = Path(path).expanduser().resolve()

    remotes_proc = await asyncio.create_subprocess_exec(
        "git", "-C", str(p), "remote",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await remotes_proc.communicate()
    remotes = [r for r in stdout.decode().splitlines() if r]

    if not remotes:
        on_line("[yellow]No remotes configured — nothing to push.[/yellow]")
        return True

    all_ok = True
    for remote in remotes:
        on_line(f"[bold]→ git push --all {remote}[/bold]")
        proc = await asyncio.create_subprocess_exec(
            "git", "-C", str(p), "push", remote, "--all",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        assert proc.stdout is not None
        async for raw in proc.stdout:
            line = raw.decode(errors="replace").rstrip()
            if line:
                on_line(f"  {line}")
        rc = await proc.wait()
        if rc != 0:
            on_line(f"  [red]✗ push to {remote} failed (exit {rc})[/red]")
            all_ok = False
        else:
            on_line(f"  [green]✓ {remote} — done[/green]")

    return all_ok
