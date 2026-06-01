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
  SAVE_A: /media/anthony/SAVE_A
  SAVE_B: /media/anthony/SAVE_B
  SAVE_C: /media/anthony/SAVE_C

# PART 1 — folders to sync from laptop to SAVE_A
# Each entry maps a source folder to its destination.
laptop_to_save_a: []
  # - from: /home/anthony/ANTHONY/PHOTOS
  #   to:   /media/anthony/SAVE_A/ANTHONY/PHOTOS
  # - from: /home/anthony/ANTHONY/MAGIC
  #   to:   /media/anthony/SAVE_A/MAGIC

# PART 3 — folders on SAVE_A whose contents must be burned to M-DISC
mdisc_tracked: []
  # - /media/anthony/SAVE_A/ANTHONY/PHOTOS
  # - /media/anthony/SAVE_A/MAGIC
"""


def load() -> dict[str, Any]:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    if not CONFIG_FILE.exists():
        CONFIG_FILE.write_text(_TEMPLATE)
    with CONFIG_FILE.open() as fh:
        return yaml.safe_load(fh) or {}


def disc_path(cfg: dict, name: str) -> str:
    return cfg.get("discs", {}).get(name, f"/media/anthony/{name}")


def is_mounted(path: str) -> bool:
    return Path(path).exists() and Path(path).is_mount()
