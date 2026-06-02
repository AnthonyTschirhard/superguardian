import getpass
from pathlib import Path
from typing import Any

import yaml

CONFIG_DIR = Path.home() / ".config" / "superguardian"
CONFIG_FILE = CONFIG_DIR / "config.yaml"
DB_FILE = CONFIG_DIR / "history.db"


class ConfigError(Exception):
    """Raised when the config file cannot be parsed."""


def _make_template() -> str:
    user = getpass.getuser()
    return f"""\
# Super Guardian configuration — edit paths to match your setup.
# Restart the app or press R to reload after editing.

# Physical mount points for your SAVE discs
discs:
  SAVE_A: /run/media/{user}/SAVE_A
  SAVE_B: /run/media/{user}/SAVE_B
  SAVE_C: /run/media/{user}/SAVE_C

# Primary Save — folders to sync from laptop to SAVE_A
# Each entry needs 'from' (source) and 'to' (destination).
# 'exclude' is optional and accepts rsync patterns.
#
# laptop_to_save_a:
#   - from: /home/{user}/Documents
#     to:   /run/media/{user}/SAVE_A/Documents
#   - from: /home/{user}/Photos
#     to:   /run/media/{user}/SAVE_A/Photos
#     exclude:
#       - .venv
#       - __pycache__
laptop_to_save_a:

# M-DISC — folders on SAVE_A whose contents must be burned to M-DISC
#
# mdisc_tracked:
#   - /run/media/{user}/SAVE_A/Photos
#   - /run/media/{user}/SAVE_A/ImportantDocs
mdisc_tracked:

# Git Repos — list of git repositories (normal or bare) to back up by pushing
# to their configured remotes. Press 's' on the Git tab to push all branches.
#
# git_repos:
#   - /home/{user}/source/myproject
#   - /home/{user}/source/anotherrepo
#   - /backup/repos/myproject.git
git_repos:
"""


def load() -> dict[str, Any]:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    if not CONFIG_FILE.exists():
        CONFIG_FILE.write_text(_make_template())
    try:
        with CONFIG_FILE.open() as fh:
            return yaml.safe_load(fh) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(
            f"Cannot parse {CONFIG_FILE} — invalid YAML.\n\n{exc}\n\n"
            "Fix the file and press R to reload."
        ) from exc


def disc_path(cfg: dict, name: str) -> str:
    return cfg.get("discs", {}).get(name, f"/run/media/{getpass.getuser()}/{name}")


def is_mounted(path: str) -> bool:
    return Path(path).exists() and Path(path).is_mount()
