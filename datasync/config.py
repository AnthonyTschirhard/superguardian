from pathlib import Path
from typing import Any

import yaml

CONFIG_DIR = Path.home() / ".config" / "datasync"
CONFIG_FILE = CONFIG_DIR / "config.yaml"
DB_FILE = CONFIG_DIR / "history.db"

_TEMPLATE = """\
# DataSync configuration — edit paths to match your setup.
# Restart the app or press R to reload after editing.

# Physical mount points for your SAVE discs
discs:
  SAVE_A: /run/media/anthony/SAVE_A
  SAVE_B: /run/media/anthony/SAVE_B
  SAVE_C: /run/media/anthony/SAVE_C

# Primary Save — folders to sync from laptop to SAVE_A
# Each entry maps a source folder to its destination.
# Optional 'exclude' accepts rsync patterns (same as --exclude=PATTERN).
laptop_to_save_a: []
  # - from: /home/anthony/ANTHONY/PHOTOS
  #   to:   /run/media/anthony/SAVE_A/ANTHONY/PHOTOS
  #   exclude:
  #     - .venv
  #     - __pycache__

# M-DISC — folders on SAVE_A whose contents must be burned to M-DISC
mdisc_tracked: []
  # - /run/media/anthony/SAVE_A/ANTHONY/PHOTOS
  # - /run/media/anthony/SAVE_A/MAGIC
"""


def load() -> dict[str, Any]:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    if not CONFIG_FILE.exists():
        CONFIG_FILE.write_text(_TEMPLATE)
    with CONFIG_FILE.open() as fh:
        return yaml.safe_load(fh) or {}


def disc_path(cfg: dict, name: str) -> str:
    return cfg.get("discs", {}).get(name, f"/run/media/anthony/{name}")


def is_mounted(path: str) -> bool:
    return Path(path).exists() and Path(path).is_mount()
