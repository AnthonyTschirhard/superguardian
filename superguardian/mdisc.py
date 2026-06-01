from dataclasses import dataclass
from pathlib import Path

from . import history


def fmt_size(size_bytes: int | float) -> str:
    n = float(size_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


@dataclass
class PendingFile:
    path: str
    size: int
    mtime: float
    reason: str  # "new" | "modified"

    @property
    def size_human(self) -> str:
        return fmt_size(self.size)


def scan_pending(tracked_dirs: list[str]) -> list[PendingFile]:
    """Return files not yet burned to any M-DISC, or modified since last burn."""
    burned = history.burned_map()
    result: list[PendingFile] = []

    for directory in tracked_dirs:
        p = Path(directory)
        if not p.exists():
            continue
        for entry in sorted(p.rglob("*")):
            if not entry.is_file(follow_symlinks=False):
                continue
            try:
                st = entry.stat()
            except OSError:
                continue
            path_str = str(entry)
            if path_str not in burned:
                result.append(PendingFile(
                    path=path_str, size=st.st_size,
                    mtime=st.st_mtime, reason="new",
                ))
            elif abs(burned[path_str] - st.st_mtime) > 1.0:
                result.append(PendingFile(
                    path=path_str, size=st.st_size,
                    mtime=st.st_mtime, reason="modified",
                ))

    return result


def total_pending_size(files: list[PendingFile]) -> str:
    return fmt_size(sum(f.size for f in files))
