"""Tests for destination path safety guards in sync.py."""
import pytest

from superguardian.sync import check_destination_safety


DISC_ROOT = "/run/media/anthony/SAVE_A"


# ── safe destinations ─────────────────────────────────────────────────────────

def test_safe_depth_2():
    check_destination_safety(f"{DISC_ROOT}/ANTHONY/save", DISC_ROOT)


def test_safe_depth_3():
    check_destination_safety(f"{DISC_ROOT}/ANTHONY/PROJECTS/save", DISC_ROOT)


def test_safe_trailing_slash():
    check_destination_safety(f"{DISC_ROOT}/ANTHONY/PROJECTS/save/", DISC_ROOT)


def test_safe_root_trailing_slash():
    check_destination_safety(f"{DISC_ROOT}/ANTHONY/PROJECTS/save/", f"{DISC_ROOT}/")


# ── blocked destinations ──────────────────────────────────────────────────────

def test_blocks_disc_root_itself():
    with pytest.raises(ValueError, match="only 0 level"):
        check_destination_safety(DISC_ROOT, DISC_ROOT)


def test_blocks_disc_root_trailing_slash():
    with pytest.raises(ValueError, match="only 0 level"):
        check_destination_safety(f"{DISC_ROOT}/", DISC_ROOT)


def test_blocks_depth_1():
    with pytest.raises(ValueError, match="only 1 level"):
        check_destination_safety(f"{DISC_ROOT}/ANTHONY", DISC_ROOT)


def test_blocks_depth_1_trailing_slash():
    with pytest.raises(ValueError, match="only 1 level"):
        check_destination_safety(f"{DISC_ROOT}/ANTHONY/", DISC_ROOT)


# ── wrong disc ────────────────────────────────────────────────────────────────

def test_blocks_destination_outside_disc():
    with pytest.raises(ValueError, match="not inside disc root"):
        check_destination_safety("/home/anthony/ANTHONY/save", DISC_ROOT)


def test_blocks_sibling_disc():
    with pytest.raises(ValueError, match="not inside disc root"):
        check_destination_safety("/run/media/anthony/SAVE_B/ANTHONY/save", DISC_ROOT)
