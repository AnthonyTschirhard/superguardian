import getpass
import os
from pathlib import Path
from typing import Any

import yaml

CONFIG_DIR = Path.home() / ".config" / "superguardian"
CONFIG_FILE = CONFIG_DIR / "config.yaml"
DB_FILE = CONFIG_DIR / "history.db"


def _make_template() -> str:
    user = getpass.getuser()
    uid = os.getuid()
    return f"""\
# Super Guardian configuration — edit paths to match your setup.
# Restart the app or press R to reload after editing.

# Physical mount points for your SAVE discs
discs:
  SAVE_A: /run/media/{user}/SAVE_A
  SAVE_B: /run/media/{user}/SAVE_B
  SAVE_C: /run/media/{user}/SAVE_C

# Primary Save — folders to sync from laptop to SAVE_A
# Each entry maps a source folder to its destination.
# Optional 'exclude' accepts rsync patterns (same as --exclude=PATTERN).
laptop_to_save_a: []
  # - from: /home/{user}/SAVE/PHOTOS
  #   to:   /run/media/{user}/SAVE_A/PHOTOS
  #   exclude:
  #     - .venv
  #     - __pycache__

# Primary Save — individual files to sync from laptop to SAVE_A
# (as opposed to whole folders above). Each entry maps one source file
# to one destination file path.
laptop_to_save_a_files: []
  # - from: /home/{user}/vault.hc
  #   to:   /run/media/{user}/SAVE_A/{user.upper()}/PERSO/vault.hc

# M-DISC — folders on SAVE_A whose contents must be burned to M-DISC
mdisc_tracked: []
  # - /run/media/{user}/SAVE_A/PHOTOS
  # - /run/media/{user}/SAVE_A/MAGIC

# Password/OTP vault backup — VeraCrypt-encrypted container holding a local
# (non-synced) git repo with a Firefox password export + Ente Auth OTP export.
# Mounted only while the backup is running; the VeraCrypt password is always
# prompted, never stored. The container itself is just a regular file — sync
# it to SAVE_A via a laptop_to_save_a_files entry above.
vault:
  container: /home/{user}/vault.hc
  mountpoint: /run/user/{uid}/superguardian-vault
  size_mb: 100
  firefox_profile: /home/{user}/snap/firefox/common/.mozilla/firefox/CHANGE_ME.default
  firefox_decrypt_path: /home/{user}/tools/firefox_decrypt/firefox_decrypt.py
  ente_login_match: ente.io
"""


def load() -> dict[str, Any]:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    if not CONFIG_FILE.exists():
        CONFIG_FILE.write_text(_make_template())
    with CONFIG_FILE.open() as fh:
        return yaml.safe_load(fh) or {}


def disc_path(cfg: dict, name: str) -> str:
    return cfg.get("discs", {}).get(name, f"/run/media/{getpass.getuser()}/{name}")


def is_mounted(path: str) -> bool:
    return Path(path).exists() and Path(path).is_mount()
