# Super Guardian

Lazygit-style TUI backup manager built with Python + Textual.

## Run

```bash
.venv/bin/python main.py
```

## Test

```bash
.venv/bin/pytest tests/
```

## Config

Lives at `~/.config/superguardian/config.yaml` (created on first run).
History DB at `~/.config/superguardian/history.db`.

## Architecture

- `superguardian/config.py` — YAML config loader + disc-mount helpers
- `superguardian/history.py` — SQLite sync history + M-DISC burn records
- `superguardian/mdisc.py` — scan pending files for M-DISC burning
- `superguardian/sync.py` — rsync wrapper with permanent-deletion safety check
- `superguardian/app.py` — complete Textual TUI (all UI in one file)

## Key behaviours

- rsync runs via subprocess; never use a Python rsync package
- Deletion safety: dry-runs rsync first, flags dest-only files with no source copy as permanent deletes
- Move detection: dest-only files whose content exists anywhere in source are flagged as moves
- Destinations must be ≥2 levels below disc root — enforced before any `--delete` sync
- Config directory was previously `~/.config/datasync/` — migrate with:
  `mv ~/.config/datasync ~/.config/superguardian`
