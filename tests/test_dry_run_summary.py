"""Tests for DryRunSummary and dry_run_summary aggregation logic."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

from datasync.sync import DeletionRisk, DryRunSummary, dry_run_summary


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
