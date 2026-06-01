"""DataSync TUI — lazygit-style backup manager."""
from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from shutil import which
from typing import Any

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, ScrollableContainer, Vertical
from textual.css.query import NoMatches
from textual.message import Message
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual.widget import Widget
from textual.widgets import (
    Button,
    ContentSwitcher,
    DataTable,
    Footer,
    Input,
    Label,
    ListItem,
    ListView,
    RichLog,
    Static,
)

from . import config, history, mdisc, sync
from .config import is_mounted

# ── constants ─────────────────────────────────────────────────────────────────

OPERATIONS: list[tuple[str, str, str]] = [
    ("part1",    "Primary Save   Laptop → SAVE_A",  ""),
    ("part2_ab", "Full Mirror    SAVE_A → SAVE_B",  ""),
    ("part2_ac", "Offsite Mirror SAVE_A → SAVE_C",  ""),
    ("part3",    "M-DISC",                          ""),
    ("part4",    "Media",                           "(coming soon)"),
]

# ── CSS ───────────────────────────────────────────────────────────────────────

CSS = """
Screen { background: $surface; }

#body { height: 1fr; }

OperationsPanel {
    width: 38;
    border: round $primary-darken-1;
    padding: 0;
}

.panel-title {
    background: $primary-darken-2;
    color: $text;
    text-style: bold;
    width: 1fr;
    text-align: center;
    padding: 0 1;
}

ListView { border: none; background: transparent; padding: 0; }
ListItem { padding: 0 1; height: auto; }
ListItem.--highlight { background: $accent-darken-2; }

#detail-panel {
    width: 1fr;
    border: round $primary-darken-1;
    margin-left: 1;
}

ContentSwitcher { height: 1fr; }

.part-view {
    padding: 1 2;
    height: 1fr;
    overflow-y: auto;
}

.section-hdr {
    color: $accent;
    text-style: bold;
    padding: 1 0 0 0;
}

.ok   { color: $success; }
.warn { color: $warning; }
.err  { color: $error;   }
.dim  { color: $text-muted; }

RichLog {
    border: round $primary-darken-2;
    height: 14;
    min-height: 6;
    margin-top: 1;
}

DataTable { height: 14; margin-top: 1; }

#confirm-modal {
    align: center middle;
}

#confirm-box {
    background: $surface;
    border: round $warning;
    padding: 1 3;
    width: 70;
    height: auto;
    max-height: 30;
}

#confirm-scroll { height: auto; max-height: 14; }
#confirm-buttons { layout: horizontal; height: 3; align: center middle; margin-top: 1; }

#burn-modal { align: center middle; }
#burn-box {
    background: $surface;
    border: round $primary;
    padding: 1 3;
    width: 52;
    height: auto;
}
#burn-buttons { layout: horizontal; height: 3; align: center middle; margin-top: 1; }
"""

# ── helpers ───────────────────────────────────────────────────────────────────

def _ago(iso: str) -> str:
    try:
        dt = datetime.fromisoformat(iso)
        delta = datetime.now() - dt
        s = int(delta.total_seconds())
        if s < 60:
            return "just now"
        if s < 3600:
            return f"{s // 60}m ago"
        if s < 86400:
            return f"{s // 3600}h ago"
        return f"{s // 86400}d ago"
    except Exception:
        return iso


def _disc_badge(mounted: bool) -> str:
    return "[green]● connected[/green]" if mounted else "[red]✗ not connected[/red]"


# ── modals ────────────────────────────────────────────────────────────────────

class ConfirmSyncModal(ModalScreen[bool]):
    """Ask the user to confirm before running a destructive sync."""

    def __init__(self, risks: list[sync.DeletionRisk], dry_run: bool) -> None:
        super().__init__()
        self._risks = risks
        self._dry_run = dry_run

    def compose(self) -> ComposeResult:
        permanent = [r for r in self._risks if r.permanent]
        mode = "DRY-RUN" if self._dry_run else "SYNC"
        with Vertical(id="confirm-box"):
            yield Label(f"[bold]Confirm {mode}[/bold]")
            if permanent:
                yield Label(
                    f"\n[red][bold]{len(permanent)} file(s) will be permanently deleted[/bold][/red]\n"
                    "(no matching copy found in source):"
                )
                with ScrollableContainer(id="confirm-scroll"):
                    for r in permanent:
                        yield Label(f"  [red]{r.full_path}[/red]")
            elif self._risks:
                yield Label(
                    f"\n[yellow]{len(self._risks)} file(s) will be removed from destination[/yellow]\n"
                    "(copies exist in source — likely moved):"
                )
                with ScrollableContainer(id="confirm-scroll"):
                    for r in self._risks[:20]:
                        yield Label(f"  [dim]{r.full_path}[/dim]")
                    if len(self._risks) > 20:
                        yield Label(f"  [dim]… and {len(self._risks) - 20} more[/dim]")
            else:
                yield Label("\n[green]No deletions detected.[/green]")
            yield Label("\nDo you want to continue?")
            with Horizontal(id="confirm-buttons"):
                yield Button("Yes", variant="warning", id="btn-yes")
                yield Button("No", variant="default", id="btn-no")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "btn-yes")


class BurnModal(ModalScreen[str | None]):
    """Ask for the disc label when marking files as burned."""

    def compose(self) -> ComposeResult:
        with Vertical(id="burn-box"):
            yield Label("[bold]Mark files as burned to M-DISC[/bold]")
            yield Label("\nDisc label (e.g. MDISC-01):")
            yield Input(placeholder="MDISC-01", id="burn-label-input")
            with Horizontal(id="burn-buttons"):
                yield Button("Confirm", variant="primary", id="btn-confirm")
                yield Button("Cancel", variant="default", id="btn-cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-confirm":
            label = self.query_one("#burn-label-input", Input).value.strip()
            self.dismiss(label if label else None)
        else:
            self.dismiss(None)


# ── operations panel (left) ───────────────────────────────────────────────────

class OperationsPanel(Widget):
    class Selected(Message):
        def __init__(self, op_id: str) -> None:
            super().__init__()
            self.op_id = op_id

    def compose(self) -> ComposeResult:
        yield Label("  Operations  ", classes="panel-title")
        items = []
        for op_id, label, note in OPERATIONS:
            note_str = f" [dim]{note}[/dim]" if note else ""
            static = Static(f"{label}{note_str}", id=f"opstatus-{op_id}")
            item = ListItem(static)
            item.id = f"opitem-{op_id}"
            items.append(item)
        yield ListView(*items, id="ops-list")

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        if event.item and event.item.id:
            op_id = event.item.id.replace("opitem-", "")
            self.post_message(self.Selected(op_id))

    def set_status(self, op_id: str, status: str) -> None:
        try:
            label, note = next(
                (lbl, n) for oid, lbl, n in OPERATIONS if oid == op_id
            )
        except StopIteration:
            return
        note_str = f" [dim]{note}[/dim]" if note else ""
        status_str = f" [dim]{status}[/dim]" if status else ""
        try:
            self.query_one(f"#opstatus-{op_id}", Static).update(
                f"{label}{note_str}{status_str}"
            )
        except NoMatches:
            pass


# ── per-operation detail views ────────────────────────────────────────────────

class Part1View(ScrollableContainer):
    def compose(self) -> ComposeResult:
        yield Label("[bold]Primary Save — Laptop → SAVE_A[/bold]")
        yield Label("", id="p1-status")
        yield Label("MAPPINGS", classes="section-hdr")
        yield Static("", id="p1-mappings")
        yield Label("LOG", classes="section-hdr")
        yield RichLog(id="p1-log", highlight=True, markup=True)

    def refresh_view(self, cfg: dict[str, Any]) -> None:
        save_a = config.disc_path(cfg, "SAVE_A")
        mounted = is_mounted(save_a)
        last = history.last_successful_sync("part1")
        last_str = f"Last sync: {_ago(last['finished_at'])}" if last else "Last sync: never"
        self.query_one("#p1-status", Label).update(
            f"SAVE_A  {_disc_badge(mounted)}  {save_a}\n"
            f"[dim]{last_str}[/dim]"
        )
        mappings = cfg.get("laptop_to_save_a") or []
        if mappings:
            lines = "\n".join(
                f"  [dim]{m['from']}[/dim]  →  [dim]{m['to']}[/dim]"
                for m in mappings
            )
        else:
            lines = "  [yellow]No mappings configured — edit ~/.config/datasync/config.yaml[/yellow]"
        self.query_one("#p1-mappings", Static).update(lines)

    @property
    def log(self) -> RichLog:
        return self.query_one("#p1-log", RichLog)


class Part2View(ScrollableContainer):
    """Used for both SAVE_A→SAVE_B and SAVE_A→SAVE_C."""

    def __init__(self, op_id: str, dest_name: str, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._op_id = op_id
        self._dest_name = dest_name

    def compose(self) -> ComposeResult:
        dest = self._dest_name
        label = "Full Mirror" if dest == "SAVE_B" else "Offsite Mirror"
        yield Label(f"[bold]{label} — SAVE_A → {dest}[/bold]")
        yield Label("", id=f"p2-status-{self._op_id}")
        yield Label("LOG", classes="section-hdr")
        yield RichLog(id=f"p2-log-{self._op_id}", highlight=True, markup=True)

    def refresh_view(self, cfg: dict[str, Any]) -> None:
        save_a = config.disc_path(cfg, "SAVE_A")
        dest = config.disc_path(cfg, self._dest_name)
        a_ok = is_mounted(save_a)
        d_ok = is_mounted(dest)
        last = history.last_successful_sync(self._op_id)
        last_str = f"Last sync: {_ago(last['finished_at'])}" if last else "Last sync: never"
        note = "" if self._dest_name != "SAVE_C" else "  [dim](remote disc — connect when available)[/dim]"
        self.query_one(f"#p2-status-{self._op_id}", Label).update(
            f"SAVE_A  {_disc_badge(a_ok)}  {save_a}\n"
            f"{self._dest_name}  {_disc_badge(d_ok)}  {dest}{note}\n"
            f"[dim]{last_str}[/dim]"
        )

    @property
    def log(self) -> RichLog:
        return self.query_one(f"#p2-log-{self._op_id}", RichLog)


class Part3View(ScrollableContainer):
    _pending: list[mdisc.PendingFile] = []

    def compose(self) -> ComposeResult:
        yield Label("[bold]M-DISC Tracking[/bold]")
        yield Label("", id="p3-status")
        yield Label("PENDING FILES (not yet burned)", classes="section-hdr")
        table = DataTable(id="p3-table", zebra_stripes=True)
        table.add_columns("Path", "Size", "Reason")
        yield table
        yield Label("[dim]m: mark selected as burned   r: refresh[/dim]")

    def refresh_view(self, cfg: dict[str, Any]) -> None:
        tracked = cfg.get("mdisc_tracked") or []
        available = sum(1 for d in tracked if Path(d).exists())
        self.query_one("#p3-status", Label).update(
            f"Tracked folders: [bold]{len(tracked)}[/bold]"
            f"  Available: [bold]{available}[/bold]"
        )
        self._pending = mdisc.scan_pending(tracked)
        table = self.query_one("#p3-table", DataTable)
        table.clear()
        for f in self._pending:
            size_b = f.size
            for unit in ("B", "KB", "MB", "GB"):
                if size_b < 1024:
                    size_str = f"{size_b:.1f} {unit}"
                    break
                size_b /= 1024
            else:
                size_str = f"{size_b:.1f} TB"
            table.add_row(f.path, size_str, f.reason)

        total = mdisc.total_pending_size(self._pending)
        self.query_one("#p3-status", Label).update(
            f"Tracked folders: [bold]{len(tracked)}[/bold]"
            f"  Available: [bold]{available}[/bold]"
            f"  Pending: [bold]{len(self._pending)}[/bold] files ({total})"
        )

    def selected_files(self) -> list[mdisc.PendingFile]:
        table = self.query_one("#p3-table", DataTable)
        if table.cursor_row < len(self._pending):
            return [self._pending[table.cursor_row]]
        return []

    def all_pending(self) -> list[mdisc.PendingFile]:
        return list(self._pending)


class Part4View(ScrollableContainer):
    def compose(self) -> ComposeResult:
        yield Label("[bold]Media Drives[/bold]")
        yield Label(
            "\n[dim]Coming soon.[/dim]\n\n"
            "This section will handle syncing media drives (films, TV series)\n"
            "and videography rushes to tape or other archival media."
        )

    def refresh_view(self, cfg: dict[str, Any]) -> None:
        pass


# ── main app ──────────────────────────────────────────────────────────────────

class DataSyncApp(App):
    CSS = CSS
    TITLE = "DataSync"
    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("d", "dry_run", "Dry-run"),
        Binding("s", "sync_op", "Sync"),
        Binding("r", "refresh", "Refresh"),
        Binding("m", "mark_burned", "Mark burned"),
        Binding("R", "reload_config", "Reload config"),
        Binding("j,down", "cursor_down", "Down", show=False),
        Binding("k,up", "cursor_up", "Up", show=False),
    ]

    current_op: reactive[str] = reactive("part1")

    def __init__(self) -> None:
        super().__init__()
        self._cfg: dict[str, Any] = {}

    # ── compose ───────────────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        with Horizontal(id="body"):
            yield OperationsPanel(id="ops-panel")
            with Vertical(id="detail-panel"):
                yield Label("", id="detail-title", classes="panel-title")
                with ContentSwitcher(initial="detail-part1", id="switcher"):
                    yield Part1View(id="detail-part1", classes="part-view")
                    yield Part2View("part2_ab", "SAVE_B", id="detail-part2_ab", classes="part-view")
                    yield Part2View("part2_ac", "SAVE_C", id="detail-part2_ac", classes="part-view")
                    yield Part3View(id="detail-part3", classes="part-view")
                    yield Part4View(id="detail-part4", classes="part-view")
        yield Footer()

    # ── lifecycle ─────────────────────────────────────────────────────────────

    def on_mount(self) -> None:
        if not which("rsync"):
            self.notify("rsync not found — install it with: sudo apt install rsync", severity="error")
        self._cfg = config.load()
        self._refresh_all_views()

    # ── messages ──────────────────────────────────────────────────────────────

    def on_operations_panel_selected(self, event: OperationsPanel.Selected) -> None:
        self.current_op = event.op_id
        self.query_one("#switcher", ContentSwitcher).current = f"detail-{event.op_id}"
        self._update_detail_title(event.op_id)

    # ── actions ───────────────────────────────────────────────────────────────

    def action_cursor_down(self) -> None:
        self.query_one("#ops-list", ListView).action_cursor_down()

    def action_cursor_up(self) -> None:
        self.query_one("#ops-list", ListView).action_cursor_up()

    def action_refresh(self) -> None:
        self._cfg = config.load()
        self._refresh_all_views()
        self._refresh_pending_count()

    def action_reload_config(self) -> None:
        self._cfg = config.load()
        self._refresh_all_views()
        self.notify("Config reloaded.")

    def action_dry_run(self) -> None:
        if self.current_op == "part4":
            self.notify("Not implemented yet.", severity="warning")
            return
        self._start_sync(dry_run=True)

    def action_sync_op(self) -> None:
        if self.current_op == "part4":
            self.notify("Not implemented yet.", severity="warning")
            return
        self._start_sync(dry_run=False)

    def action_mark_burned(self) -> None:
        if self.current_op != "part3":
            self.notify("Mark burned is only available in M-DISC.", severity="warning")
            return
        view = self.query_one("#detail-part3", Part3View)
        files = view.all_pending()
        if not files:
            self.notify("No pending files to mark.")
            return
        self.push_screen(BurnModal(), self._handle_burn_label)

    # ── sync orchestration ────────────────────────────────────────────────────

    def _start_sync(self, *, dry_run: bool) -> None:
        op = self.current_op
        if op == "part3":
            self.notify("Use 'm' to manage M-DISC burns.", severity="warning")
            return
        self._check_risks_then_sync(op, dry_run=dry_run)

    @work(exclusive=True)
    async def _check_risks_then_sync(self, op: str, *, dry_run: bool) -> None:
        """Check for deletion risks, confirm with user, then run sync."""
        log = self._get_log(op)
        log.clear()

        pairs = self._get_sync_pairs(op)
        if not pairs:
            log.write("[yellow]No mappings configured for this operation.[/yellow]")
            return

        # Check all pairs for deletion risks
        all_risks: list[sync.DeletionRisk] = []
        for src, dst in pairs:
            if not Path(src).exists():
                log.write(f"[red]Source not found: {src}[/red]")
                return
            if not Path(dst).exists():
                log.write(f"[yellow]Destination not found (will be created): {dst}[/yellow]")
                continue
            log.write(f"[dim]Checking for deletion risks: {src} → {dst}[/dim]")
            try:
                risks = await sync.list_deletion_risks(src, dst)
                all_risks.extend(risks)
            except Exception as exc:
                log.write(f"[red]Risk check failed: {exc}[/red]")
                return

        confirmed = await self.push_screen_wait(ConfirmSyncModal(all_risks, dry_run))
        if not confirmed:
            log.write("[dim]Cancelled.[/dim]")
            return

        await self._execute_sync(op, pairs, dry_run=dry_run)

    @work(exclusive=True)
    async def _execute_sync(
        self,
        op: str,
        pairs: list[tuple[str, str]],
        *,
        dry_run: bool,
    ) -> None:
        log = self._get_log(op)
        run_id = history.start_run(op, dry_run=dry_run)
        total_files = 0
        full_log_parts: list[str] = []
        status = "success"
        mode = "[dim](dry-run)[/dim]" if dry_run else ""

        for src, dst in pairs:
            log.write(f"\n[bold]{'─' * 40}[/bold]")
            log.write(f"[bold]{src}[/bold]  →  [bold]{dst}[/bold]  {mode}")
            try:
                os.makedirs(dst, exist_ok=True)
                files, part_log = await sync.run_rsync(
                    src, dst,
                    dry_run=dry_run,
                    on_line=log.write,
                )
                total_files += files
                full_log_parts.append(part_log)
            except RuntimeError as exc:
                log.write(f"[red]ERROR: {exc}[/red]")
                status = "error"
                break

        history.finish_run(
            run_id,
            status=status,
            files_count=total_files,
            log="\n\n".join(full_log_parts),
        )

        verb = "Dry-run" if dry_run else "Sync"
        if status == "success":
            log.write(f"\n[green]{verb} complete — {total_files} file(s) transferred.[/green]")
            self._refresh_op_status(op)
        else:
            log.write(f"\n[red]{verb} failed.[/red]")

    # ── burn handling ─────────────────────────────────────────────────────────

    def _handle_burn_label(self, label: str | None) -> None:
        if not label:
            return
        view = self.query_one("#detail-part3", Part3View)
        files = view.all_pending()
        for f in files:
            history.mark_burned(
                disc_label=label,
                file_path=f.path,
                file_size=f.size,
                file_mtime=f.mtime,
            )
        view.refresh_view(self._cfg)
        self.notify(f"Marked {len(files)} file(s) as burned to {label}.")

    # ── helpers ───────────────────────────────────────────────────────────────

    def _get_sync_pairs(self, op: str) -> list[tuple[str, str]]:
        cfg = self._cfg
        if op == "part1":
            return [
                (m["from"], m["to"])
                for m in (cfg.get("laptop_to_save_a") or [])
            ]
        if op == "part2_ab":
            return [(config.disc_path(cfg, "SAVE_A"), config.disc_path(cfg, "SAVE_B"))]
        if op == "part2_ac":
            return [(config.disc_path(cfg, "SAVE_A"), config.disc_path(cfg, "SAVE_C"))]
        return []

    def _get_log(self, op: str) -> RichLog:
        if op == "part1":
            return self.query_one("#detail-part1 RichLog", RichLog)
        if op == "part2_ab":
            return self.query_one("#detail-part2_ab RichLog", RichLog)
        if op == "part2_ac":
            return self.query_one("#detail-part2_ac RichLog", RichLog)
        return self.query_one(f"#detail-{op} RichLog", RichLog)

    def _get_view(self, op: str) -> Part1View | Part2View | Part3View | Part4View:
        return self.query_one(f"#detail-{op}")  # type: ignore[return-value]

    def _refresh_all_views(self) -> None:
        cfg = self._cfg
        for op_id, _, _ in OPERATIONS:
            try:
                self._get_view(op_id).refresh_view(cfg)  # type: ignore[union-attr]
                self._refresh_op_status(op_id)
            except Exception:
                pass

    def _refresh_op_status(self, op_id: str) -> None:
        panel = self.query_one("#ops-panel", OperationsPanel)
        cfg = self._cfg
        if op_id == "part1":
            last = history.last_successful_sync("part1")
            a_ok = is_mounted(config.disc_path(cfg, "SAVE_A"))
            disc = "✓" if a_ok else "✗"
            last_str = _ago(last["finished_at"]) if last else "never"
            panel.set_status(op_id, f"[dim]SAVE_A:{disc}  last:{last_str}[/dim]")
        elif op_id == "part2_ab":
            a_ok = is_mounted(config.disc_path(cfg, "SAVE_A"))
            b_ok = is_mounted(config.disc_path(cfg, "SAVE_B"))
            last = history.last_successful_sync("part2_ab")
            last_str = _ago(last["finished_at"]) if last else "never"
            panel.set_status(op_id, f"[dim]A:{'✓' if a_ok else '✗'}  B:{'✓' if b_ok else '✗'}  last:{last_str}[/dim]")
        elif op_id == "part2_ac":
            a_ok = is_mounted(config.disc_path(cfg, "SAVE_A"))
            c_ok = is_mounted(config.disc_path(cfg, "SAVE_C"))
            last = history.last_successful_sync("part2_ac")
            last_str = _ago(last["finished_at"]) if last else "never"
            panel.set_status(op_id, f"[dim]A:{'✓' if a_ok else '✗'}  C:{'✓' if c_ok else '✗'}  last:{last_str}[/dim]")
        elif op_id == "part3":
            tracked = cfg.get("mdisc_tracked") or []
            pending = mdisc.scan_pending(tracked)
            panel.set_status(op_id, f"[dim]{len(pending)} pending[/dim]")

    @work(thread=True)
    def _refresh_pending_count(self) -> None:
        """Background thread to update files-behind counts (slow for large discs)."""
        cfg = self._cfg
        for op_id in ("part1", "part2_ab", "part2_ac"):
            pairs = self._get_sync_pairs(op_id)
            # This would need async — skipping for thread worker; status shows last sync instead

    def _update_detail_title(self, op_id: str) -> None:
        label = next((lbl for oid, lbl, _ in OPERATIONS if oid == op_id), op_id)
        try:
            self.query_one("#detail-title", Label).update(f"  {label}  ")
        except NoMatches:
            pass
