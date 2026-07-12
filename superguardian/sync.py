"""rsync wrapper with permanent-deletion safety check."""
from __future__ import annotations

import asyncio
import filecmp
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path


@dataclass
class DeletionRisk:
    rel_path: str
    full_path: str
    permanent: bool  # True = no copy found in source → data would be lost


@dataclass
class DryRunSummary:
    to_add: int     # files to be transferred (new or changed)
    to_move: int    # dest-only files with a content copy elsewhere in source (rename/move)
    to_delete: int  # dest-only files with no copy in source (permanent loss)


async def list_deletion_risks(
    source: str,
    destination: str,
    exclude: list[str] | None = None,
) -> list[DeletionRisk]:
    """
    Dry-runs rsync and identifies destination files that would be permanently
    deleted — i.e. they have no size+content match anywhere in source.
    """
    src, dst = _slash(source), _slash(destination)
    to_delete = await _dry_run_deletions(src, dst, exclude or [])
    if not to_delete:
        return []

    # Index source by file size for fast lookup before byte comparison
    source_index = await asyncio.to_thread(_index_by_size, src)
    risks: list[DeletionRisk] = []

    for rel in to_delete:
        full = os.path.join(dst, rel)
        if not os.path.isfile(full) or os.path.islink(full):
            continue
        size = os.stat(full, follow_symlinks=False).st_size
        if size not in source_index:
            risks.append(DeletionRisk(rel_path=rel, full_path=full, permanent=True))
        else:
            has_copy = any(
                filecmp.cmp(full, ref, shallow=False)
                for ref in source_index[size]
            )
            risks.append(DeletionRisk(rel_path=rel, full_path=full, permanent=not has_copy))

    return risks


async def dry_run_summary(
    source: str,
    destination: str,
    exclude: list[str] | None = None,
) -> DryRunSummary:
    """
    Returns a categorised count of pending changes for source → destination.
    Runs count_pending and list_deletion_risks concurrently so both rsync
    passes happen at the same time.
    """
    add_count, risks = await asyncio.gather(
        count_pending(source, destination, exclude),
        list_deletion_risks(source, destination, exclude),
    )
    to_move = sum(1 for r in risks if not r.permanent)
    to_delete = sum(1 for r in risks if r.permanent)
    return DryRunSummary(to_add=add_count, to_move=to_move, to_delete=to_delete)


async def count_pending(
    source: str,
    destination: str,
    exclude: list[str] | None = None,
) -> int:
    """
    Returns the number of files that need to be transferred (new or changed).
    Uses --itemize-changes so we can count precisely without parsing filenames.
    Accepts the same exclude patterns as run_rsync so the count reflects the
    actual sync (e.g. .venv excluded from both).
    """
    cmd = ["rsync", "-a", "--dry-run", "--itemize-changes"]
    for p in (exclude or []):
        cmd.append(f"--exclude={p}")
    cmd += [_slash(source), _slash(destination)]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()
    return sum(
        1
        for line in stdout.decode(errors="replace").splitlines()
        # Lines starting with < or > or c are file transfers; h = hard link
        if line and line[0] in "<>ch" and len(line) > 10
    )


async def count_pending_file(source: str, destination: str) -> int:
    """
    Returns 1 if the single file at *source* differs from *destination* (or
    destination doesn't exist yet), 0 otherwise. Companion to count_pending()
    for laptop_to_save_a_files entries — source/destination are exact file
    paths, not directories, so no trailing-slash folder semantics apply.
    """
    cmd = ["rsync", "-a", "--dry-run", "--itemize-changes", source, destination]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()
    return 1 if stdout.decode(errors="replace").strip() else 0


async def run_rsync_file(source: str, destination: str) -> str:
    """
    Copies a single file source -> destination, creating parent directories
    as needed. Returns the rsync log. Raises RuntimeError on failure.
    Companion to run_rsync() for laptop_to_save_a_files entries — no
    --delete, since deletion-risk detection doesn't apply to a single
    explicitly-named file (there's no directory tree to prune).
    """
    os.makedirs(os.path.dirname(destination), exist_ok=True)
    proc = await asyncio.create_subprocess_exec(
        "rsync", "-a", "--stats", source, destination,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    stdout, _ = await proc.communicate()
    log = stdout.decode(errors="replace")
    if proc.returncode != 0:
        raise RuntimeError(f"rsync exited with code {proc.returncode}")
    return log


async def run_rsync(
    source: str,
    destination: str,
    *,
    dry_run: bool,
    exclude: list[str] | None = None,
    on_line: Callable[[str], None] | None = None,
) -> tuple[int, str]:
    """
    Runs rsync source → destination.
    Returns (files_transferred, full_log).
    Raises RuntimeError on failure (rsync exit ≠ 0 or 24).
    """
    src, dst = _slash(source), _slash(destination)
    cmd = ["rsync", "-avr", "--delete", "--stats"]
    if dry_run:
        cmd.append("--dry-run")
    for pattern in (exclude or []):
        cmd.append(f"--exclude={pattern}")
    cmd += [src, dst]

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    assert proc.stdout is not None

    lines: list[str] = []
    async for raw in proc.stdout:
        line = raw.decode(errors="replace").rstrip()
        lines.append(line)
        if on_line:
            on_line(line)

    await proc.wait()
    if proc.returncode not in (0, 24):
        raise RuntimeError(f"rsync exited with code {proc.returncode}")

    return _parse_files_count(lines), "\n".join(lines)


# ── helpers ────────────────────────────────────────────────────────────────────

# Destinations with --delete must be at least this many levels below the disc root.
# Depth 1 would allow wiping top-level disc directories; 2 is the safe minimum.
_MIN_DEST_DEPTH = 2


def check_destination_safety(destination: str, disc_root: str) -> None:
    """
    Raise ValueError if *destination* is dangerously shallow under *disc_root*.

    Prevents a misconfigured ``to:`` path from letting rsync --delete wipe an
    entire top-level directory on a backup disc.
    """
    dst = Path(destination.rstrip("/"))
    root = Path(disc_root.rstrip("/"))
    try:
        rel = dst.relative_to(root)
    except ValueError:
        raise ValueError(
            f"Destination {str(destination)!r} is not inside disc root {str(disc_root)!r}. "
            "Check your config."
        )
    depth = len(rel.parts)
    if depth < _MIN_DEST_DEPTH:
        raise ValueError(
            f"Destination {str(destination)!r} is only {depth} level(s) below disc root "
            f"{str(disc_root)!r} (minimum {_MIN_DEST_DEPTH}). "
            "Syncing here with --delete would risk wiping top-level disc content. "
            "Add a subdirectory (e.g. SAVE_A/ANTHONY/PROJECT/save/)."
        )


_RISK_EXCLUDES = (
    # Regeneratable — never a permanent data-loss risk
    "--exclude=.git",
    "--exclude=.venv",
    "--exclude=venv",
    "--exclude=__pycache__",
    "--exclude=node_modules",
    "--exclude=*.pyc",
)


async def _dry_run_deletions(source: str, destination: str, exclude: list[str]) -> list[str]:
    user_excludes = tuple(f"--exclude={p}" for p in exclude)
    proc = await asyncio.create_subprocess_exec(
        "rsync", "-r", "--delete", "--dry-run", *_RISK_EXCLUDES, *user_excludes, source, destination,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()
    return [
        line[len("deleting "):].strip()
        for line in stdout.decode(errors="replace").splitlines()
        if line.startswith("deleting ")
    ]


def _index_by_size(directory: str) -> dict[int, list[str]]:
    index: dict[int, list[str]] = {}
    for root, _, files in os.walk(directory):
        for name in files:
            full = os.path.join(root, name)
            try:
                size = os.stat(full, follow_symlinks=False).st_size
                index.setdefault(size, []).append(full)
            except OSError:
                pass
    return index


def _parse_files_count(lines: list[str]) -> int:
    for line in reversed(lines):
        if "Number of regular files transferred:" in line:
            try:
                return int(line.split(":")[-1].strip().replace(",", ""))
            except ValueError:
                pass
    return 0


_NON_TRANSFER_PREFIXES = (
    "sending incremental file list",
    "building file list",
    "sent ",
    "total size is",
    "Number of",
    "speedup is",
    "deleting ",
    "rsync error",
    "rsync:",
    "IO error",
    "cannot ",
    "skipping ",
)


def is_transfer_line(line: str) -> bool:
    """
    Returns True if a line from rsync -avr output is a file being transferred,
    as opposed to a stats header, deletion notice, or directory entry.
    Used to count real-time transfer progress without --itemize-changes.
    """
    if not line or line.endswith("/"):
        return False
    return not any(line.startswith(p) for p in _NON_TRANSFER_PREFIXES)


def _slash(path: str) -> str:
    return path if path.endswith("/") else path + "/"
