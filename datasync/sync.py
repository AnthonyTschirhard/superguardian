"""rsync wrapper with permanent-deletion safety check."""
from __future__ import annotations

import asyncio
import filecmp
import os
from collections.abc import Callable
from dataclasses import dataclass


@dataclass
class DeletionRisk:
    rel_path: str
    full_path: str
    permanent: bool  # True = no copy found in source → data would be lost


async def list_deletion_risks(source: str, destination: str) -> list[DeletionRisk]:
    """
    Dry-runs rsync and identifies destination files that would be permanently
    deleted — i.e. they have no size+content match anywhere in source.
    """
    src, dst = _slash(source), _slash(destination)
    to_delete = await _dry_run_deletions(src, dst)
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


async def count_pending(source: str, destination: str) -> int:
    """
    Returns the number of files that need to be transferred (new or changed).
    Uses --itemize-changes so we can count precisely without parsing filenames.
    """
    proc = await asyncio.create_subprocess_exec(
        "rsync", "-a", "--dry-run", "--itemize-changes",
        _slash(source), _slash(destination),
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


async def run_rsync(
    source: str,
    destination: str,
    *,
    dry_run: bool,
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

async def _dry_run_deletions(source: str, destination: str) -> list[str]:
    proc = await asyncio.create_subprocess_exec(
        "rsync", "-avr", "--delete", "--dry-run", source, destination,
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


def _slash(path: str) -> str:
    return path if path.endswith("/") else path + "/"
