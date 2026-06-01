"""Tests for config loading and template generation."""
from __future__ import annotations

import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from superguardian.config import ConfigError, _make_template, load


# ── template ──────────────────────────────────────────────────────────────────

def test_template_is_valid_yaml():
    with patch("superguardian.config.getpass.getuser", return_value="testuser"):
        text = _make_template()
    parsed = yaml.safe_load(text)
    assert isinstance(parsed, dict)


def test_template_contains_username():
    with patch("superguardian.config.getpass.getuser", return_value="jdoe"):
        text = _make_template()
    assert "jdoe" in text


def test_template_sections_present():
    with patch("superguardian.config.getpass.getuser", return_value="x"):
        text = _make_template()
    parsed = yaml.safe_load(text)
    assert "discs" in parsed
    # keys with no entries parse to None, not missing
    assert "laptop_to_save_a" in parsed
    assert "mdisc_tracked" in parsed


def test_template_empty_lists_load_as_falsy():
    with patch("superguardian.config.getpass.getuser", return_value="x"):
        text = _make_template()
    parsed = yaml.safe_load(text)
    # code uses `cfg.get(...) or []` — None and [] both work
    assert not parsed.get("laptop_to_save_a")
    assert not parsed.get("mdisc_tracked")


# ── load ──────────────────────────────────────────────────────────────────────

def test_load_valid_yaml(tmp_path):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(textwrap.dedent("""\
        discs:
          SAVE_A: /mnt/SAVE_A
        laptop_to_save_a:
          - from: /src
            to:   /dst
        mdisc_tracked:
    """))
    with patch("superguardian.config.CONFIG_DIR", tmp_path), \
         patch("superguardian.config.CONFIG_FILE", cfg_file):
        result = load()
    assert result["discs"]["SAVE_A"] == "/mnt/SAVE_A"
    assert result["laptop_to_save_a"][0]["from"] == "/src"


def test_load_invalid_yaml_raises_config_error(tmp_path):
    cfg_file = tmp_path / "config.yaml"
    # mixing flow and block syntax — the mistake that triggered this fix
    cfg_file.write_text(textwrap.dedent("""\
        laptop_to_save_a: [
          - from: /src
            to:   /dst
            ]
    """))
    with patch("superguardian.config.CONFIG_DIR", tmp_path), \
         patch("superguardian.config.CONFIG_FILE", cfg_file):
        with pytest.raises(ConfigError, match="invalid YAML"):
            load()


def test_load_creates_template_when_missing(tmp_path):
    cfg_file = tmp_path / "config.yaml"
    with patch("superguardian.config.CONFIG_DIR", tmp_path), \
         patch("superguardian.config.CONFIG_FILE", cfg_file), \
         patch("superguardian.config.getpass.getuser", return_value="x"):
        result = load()
    assert cfg_file.exists()
    assert "discs" in result


def test_load_empty_file_returns_empty_dict(tmp_path):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("")
    with patch("superguardian.config.CONFIG_DIR", tmp_path), \
         patch("superguardian.config.CONFIG_FILE", cfg_file):
        result = load()
    assert result == {}
