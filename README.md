# Super Guardian

A keyboard-driven TUI backup manager for external drives, built with Python and [Textual](https://github.com/Textualize/textual).

Designed for a 3-disc setup (one local, one mirror, one offsite), with M-DISC burn tracking and permanent-deletion safety guards. Looks and feels like lazygit.

![Python](https://img.shields.io/badge/python-3.11%2B-blue)

---

## What it does

| Operation | Description |
|---|---|
| **Primary Save** | Sync folders from your laptop to your main backup disc (SAVE_A) |
| **Full Mirror** | Mirror SAVE_A → SAVE_B (local redundancy) |
| **Offsite Mirror** | Mirror SAVE_A → SAVE_C (offsite / family copy) |
| **M-DISC** | Track which files still need to be burned to archival M-DISC |
| **Media** | *(coming soon)* |

Every sync operation:
- Does a **dry run first** and shows you exactly how many files will be added, moved, or permanently deleted before you confirm
- Detects **moved/renamed files** by content hash (so they don't show up as deletes)
- **Blocks syncs** that would wipe an entire top-level disc directory (destination depth guard)
- Streams **live progress** to the log as rsync runs

---

## Requirements

- **Python 3.11+**
- **rsync** (must be on your PATH)
- **uv** (recommended) or pip

```bash
# Check rsync is available
rsync --version

# Install uv if you don't have it
curl -LsSf https://astral.sh/uv/install.sh | sh
```

---

## Installation

```bash
git clone git@github.com:AnthonyTschirhard/superguardian.git
cd superguardian

# Create virtualenv and install dependencies
uv venv
uv pip install -r requirements.txt
```

---

## Running

```bash
.venv/bin/python main.py
```

Or add a shell alias:

```bash
alias sg="cd ~/path/to/superguardian && .venv/bin/python main.py"
```

---

## Configuration

On first launch, Super Guardian creates a config file at:

```
~/.config/superguardian/config.yaml
```

Open it in any editor and fill in your paths. Here is a full example:

```yaml
# Physical mount points for your backup discs.
# Adjust to match how your OS mounts them.
discs:
  SAVE_A: /run/media/youruser/SAVE_A   # primary disc (always connected)
  SAVE_B: /run/media/youruser/SAVE_B   # local mirror
  SAVE_C: /run/media/youruser/SAVE_C   # offsite disc (connect when available)

# Primary Save — folders to sync from laptop → SAVE_A.
# Each entry needs a 'from' (source) and 'to' (destination).
# 'exclude' is optional and accepts rsync patterns.
laptop_to_save_a:
  - from: /home/youruser/Documents
    to:   /run/media/youruser/SAVE_A/Documents
  - from: /home/youruser/Photos
    to:   /run/media/youruser/SAVE_A/Photos
    exclude:
      - .venv
      - __pycache__
      - "*.tmp"

# M-DISC — folders whose contents should be tracked for archival burning.
# Super Guardian flags files that are new or modified since their last burn.
mdisc_tracked:
  - /run/media/youruser/SAVE_A/Photos
  - /run/media/youruser/SAVE_A/ImportantDocs
```

Press `R` inside the app to reload the config without restarting.

### Destination safety rule

Super Guardian refuses to sync into a directory that is fewer than 2 levels below the disc root. This prevents a misconfigured path from letting rsync `--delete` wipe an entire top-level folder on your disc.

**Bad** (blocked): `to: /run/media/youruser/SAVE_A`  
**Good**: `to: /run/media/youruser/SAVE_A/Documents`

---

## Keyboard shortcuts

| Key | Action |
|---|---|
| `j` / `↓` | Move down in the operations list |
| `k` / `↑` | Move up in the operations list |
| `s` | Sync the selected operation |
| `d` | Rescan pending file count for the selected operation |
| `r` | Refresh all views (re-reads disc mounts and last sync times) |
| `R` | Reload config from disk |
| `m` | Mark M-DISC files as burned (M-DISC tab only) |
| `q` | Quit |

---

## How sync works

1. On startup Super Guardian runs a background dry run for each configured operation and shows how many files are pending in the left panel.
2. Press `s` to sync. A confirmation modal appears showing what rsync will do.
3. Press `y` to confirm. rsync runs live; the log streams output and the header updates with a live file count and elapsed time.
4. On completion the pending count resets to ✓.

### Move vs. delete detection

When a dry run finds files that would be deleted from the destination, Super Guardian checks whether a byte-identical copy exists anywhere in the source. If it does, the file is classified as **moved/renamed** (shown in cyan). If no copy is found it is a **permanent delete** (shown in red). You see both counts before confirming.

---

## M-DISC tracking

The M-DISC tab lists every file under your `mdisc_tracked` directories that has either never been burned or has been modified since its last burn.

- Press `m` to open the burn dialog and enter a disc label (e.g. `MDISC-2024-01`).
- All currently listed files are marked as burned to that label in the local database (`~/.config/superguardian/history.db`).
- If a file is later modified its mtime changes and it reappears as **modified** in the pending list.

---

## Data files

All runtime data lives outside the repo, in `~/.config/superguardian/`:

| File | Purpose |
|---|---|
| `config.yaml` | Your disc paths and sync mappings |
| `history.db` | SQLite database of sync runs and M-DISC burn records |

Neither file is touched by git. Back up `history.db` if you want to preserve your M-DISC burn history across machines.

---

## Adapting to your setup

Super Guardian was designed for a three-disc setup but the config is flexible:

- **Fewer discs**: just configure the operations you need; leave the rest empty or skip them.
- **More source folders**: add as many `laptop_to_save_a` entries as you like.
- **No M-DISC**: leave `mdisc_tracked` empty — the tab will show nothing to do.
- **Different mount paths**: macOS mounts under `/Volumes/`, adjust accordingly.

---

## License

MIT
