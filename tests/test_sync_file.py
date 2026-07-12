"""Tests for single-file sync helpers (count_pending_file, run_rsync_file)."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from superguardian.sync import count_pending_file, run_rsync_file


def _run(coro):
    return asyncio.run(coro)


class _FakeProcess:
    def __init__(self, stdout: bytes, returncode: int = 0):
        self._stdout = stdout
        self.returncode = returncode

    async def communicate(self):
        return self._stdout, b""


def test_count_pending_file_no_change():
    fake = AsyncMock(return_value=_FakeProcess(b""))
    with patch("superguardian.sync.asyncio.create_subprocess_exec", fake):
        assert _run(count_pending_file("/src/vault.hc", "/dst/vault.hc")) == 0


def test_count_pending_file_changed():
    fake = AsyncMock(return_value=_FakeProcess(b">f.st...... vault.hc\n"))
    with patch("superguardian.sync.asyncio.create_subprocess_exec", fake):
        assert _run(count_pending_file("/src/vault.hc", "/dst/vault.hc")) == 1


def test_count_pending_file_no_trailing_slash_forced():
    # Companion functions must NOT get folder-style trailing slashes appended,
    # or rsync would treat the file path as a directory and fail.
    fake = AsyncMock(return_value=_FakeProcess(b""))
    with patch("superguardian.sync.asyncio.create_subprocess_exec", fake) as mock_exec:
        _run(count_pending_file("/src/vault.hc", "/dst/vault.hc"))
    args = mock_exec.call_args.args
    assert "/src/vault.hc" in args
    assert "/dst/vault.hc" in args
    assert "/src/vault.hc/" not in args
    assert "/dst/vault.hc/" not in args


def test_run_rsync_file_success(tmp_path):
    dst = tmp_path / "sub" / "vault.hc"
    fake = AsyncMock(return_value=_FakeProcess(b"sent 100 bytes"))
    with patch("superguardian.sync.asyncio.create_subprocess_exec", fake):
        log = _run(run_rsync_file("/src/vault.hc", str(dst)))
    assert "sent 100 bytes" in log
    assert dst.parent.exists()  # parent dir created for a first-ever backup


def test_run_rsync_file_failure_raises():
    fake = AsyncMock(return_value=_FakeProcess(b"rsync error", returncode=23))
    with patch("superguardian.sync.asyncio.create_subprocess_exec", fake):
        with pytest.raises(RuntimeError, match="exited with code 23"):
            _run(run_rsync_file("/src/vault.hc", "/tmp/sg-test-x/vault.hc"))
