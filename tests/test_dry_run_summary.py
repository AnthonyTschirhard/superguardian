"""Tests for DryRunSummary, dry_run_summary, and is_transfer_line."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

from datasync.sync import DeletionRisk, DryRunSummary, dry_run_summary, is_transfer_line


def _run(coro):
    return asyncio.run(coro)


def _make_risk(permanent: bool) -> DeletionRisk:
    return DeletionRisk(rel_path="x", full_path="/dst/x", permanent=permanent)


def test_summary_all_zeros_when_nothing_pending():
    with (
        patch("datasync.sync.count_pending", AsyncMock(return_value=0)),
        patch("datasync.sync.list_deletion_risks", AsyncMock(return_value=[])),
    ):
        result = _run(dry_run_summary("/src/", "/dst/"))
    assert result == DryRunSummary(to_add=0, to_move=0, to_delete=0)


def test_summary_counts_additions():
    with (
        patch("datasync.sync.count_pending", AsyncMock(return_value=7)),
        patch("datasync.sync.list_deletion_risks", AsyncMock(return_value=[])),
    ):
        result = _run(dry_run_summary("/src/", "/dst/"))
    assert result.to_add == 7
    assert result.to_move == 0
    assert result.to_delete == 0


def test_summary_splits_moves_and_deletes():
    risks = [
        _make_risk(permanent=False),  # move (copy exists in source)
        _make_risk(permanent=False),  # move
        _make_risk(permanent=True),   # permanent delete
    ]
    with (
        patch("datasync.sync.count_pending", AsyncMock(return_value=3)),
        patch("datasync.sync.list_deletion_risks", AsyncMock(return_value=risks)),
    ):
        result = _run(dry_run_summary("/src/", "/dst/"))
    assert result == DryRunSummary(to_add=3, to_move=2, to_delete=1)


def test_summary_passes_exclude_to_both():
    excl = ["*.tmp", ".cache"]
    add_mock = AsyncMock(return_value=0)
    risk_mock = AsyncMock(return_value=[])
    with (
        patch("datasync.sync.count_pending", add_mock),
        patch("datasync.sync.list_deletion_risks", risk_mock),
    ):
        _run(dry_run_summary("/src/", "/dst/", excl))
    add_mock.assert_awaited_once_with("/src/", "/dst/", excl)
    risk_mock.assert_awaited_once_with("/src/", "/dst/", excl)


# ── is_transfer_line ──────────────────────────────────────────────────────────

def test_transfer_line_plain_path():
    assert is_transfer_line("ANTHONY/save/main.py") is True

def test_transfer_line_nested_path():
    assert is_transfer_line("ANTHONY/PHOTOS/2024/img001.jpg") is True

def test_transfer_line_rejects_empty():
    assert is_transfer_line("") is False

def test_transfer_line_rejects_directory():
    assert is_transfer_line("ANTHONY/save/") is False

def test_transfer_line_rejects_sent_stats():
    assert is_transfer_line("sent 12345 bytes  received 123 bytes") is False

def test_transfer_line_rejects_number_of():
    assert is_transfer_line("Number of regular files transferred: 5") is False

def test_transfer_line_rejects_total_size():
    assert is_transfer_line("total size is 99999  speedup is 1.00") is False

def test_transfer_line_rejects_deleting():
    assert is_transfer_line("deleting ANTHONY/old_file.txt") is False

def test_transfer_line_rejects_sending_header():
    assert is_transfer_line("sending incremental file list") is False

def test_transfer_line_not_fooled_by_sent_underscore():
    # A file literally named "sent_data.zip" should count as a transfer line
    assert is_transfer_line("sent_data.zip") is True
