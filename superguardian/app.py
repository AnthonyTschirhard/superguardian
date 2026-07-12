"""Super Guardian TUI — lazygit-style backup manager."""
from __future__ import annotations

import os
import time
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

from . import config, history, mdisc, sync, vault
from .config import is_mounted

# ── constants ─────────────────────────────────────────────────────────────────

OPERATIONS: list[tuple[str, str, str]] = [
    ("part1",    "Primary Save",   ""),
    ("part2_ab", "Full Mirror",    ""),
    ("part2_ac", "Offsite Mirror", ""),
    ("part3",    "M-DISC",         ""),
    ("part4",    "Media",          "(coming soon)"),
]

# Sentinel values for _pending_counts
_SCAN_UNAVAIL = -1   # disc(s) not mounted / source not found
_SCAN_ERROR   = -2   # rsync or I/O error during scan

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
ListItem { padding: 1 1; height: auto; }
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

ConfirmSyncModal {
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

#confirm-list { height: auto; max-height: 14; border: none; background: transparent; padding: 0; }
#confirm-list > ListItem { padding: 0 1; }
#confirm-buttons { layout: horizontal; height: 3; align: center middle; margin-top: 1; }

BurnModal { align: center middle; }
#burn-box {
    background: $surface;
    border: round $primary;
    padding: 1 3;
    width: 52;
    height: auto;
}
#burn-buttons { layout: horizontal; height: 3; align: center middle; margin-top: 1; }

VaultPasswordModal { align: center middle; }
#vault-box {
    background: $surface;
    border: round $primary;
    padding: 1 3;
    width: 56;
    height: auto;
}
#vault-buttons { layout: horizontal; height: 3; align: center middle; margin-top: 1; }

#p1-vault-row { height: 3; margin-top: 1; }
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


def _fmt_summary(summary: sync.DryRunSummary | None, *, error: bool = False) -> str:
    if error:
        return "[yellow dim]Scan error[/yellow dim]"
    if summary is None:
        return "[dim]—[/dim]"
    if summary.to_add == 0 and summary.to_move == 0 and summary.to_delete == 0:
        return "[green]✓ Nothing pending[/green]"
    return (
        f"[bold green]+{summary.to_add:,}[/bold green] to add   "
        f"[bold cyan]→{summary.to_move:,}[/bold cyan] to move   "
        f"[bold red]✗{summary.to_delete:,}[/bold red] to delete"
    )


# ── modals ────────────────────────────────────────────────────────────────────

class ConfirmSyncModal(ModalScreen[bool]):
    """Ask the user to confirm before running a live sync."""

    BINDINGS = [
        Binding("y", "confirm_yes", show=False),
        Binding("n", "confirm_no", show=False),
        Binding("escape", "confirm_no", show=False),
    ]

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-box"):
            yield Label("[bold]Confirm Sync[/bold]")
            yield Label(
                "\n[dim]Rsync will run live — output streams to the log.[/dim]\n"
                "Files deleted from source will also be deleted from the backup."
            )
            yield Label("\nContinue?  [dim]y / n[/dim]")
            with Horizontal(id="confirm-buttons"):
                yield Button("Yes", variant="warning", id="btn-yes")
                yield Button("No", variant="default", id="btn-no")

    def on_mount(self) -> None:
        self.query_one("#btn-no", Button).focus()

    def action_confirm_yes(self) -> None:
        self.dismiss(True)

    def action_confirm_no(self) -> None:
        self.dismiss(False)

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


class VaultPasswordModal(ModalScreen[str | None]):
    """Ask for the VeraCrypt password before mounting the vault. Never stored."""

    def compose(self) -> ComposeResult:
        with Vertical(id="vault-box"):
            yield Label("[bold]Vault Backup[/bold]")
            yield Label("\nVeraCrypt password (asked every time, never stored):")
            yield Input(password=True, id="vault-password-input")
            with Horizontal(id="vault-buttons"):
                yield Button("Mount && Backup", variant="primary", id="btn-confirm")
                yield Button("Cancel", variant="default", id="btn-cancel")

    def on_mount(self) -> None:
        self.query_one("#vault-password-input", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self._confirm()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-confirm":
            self._confirm()
        else:
            self.dismiss(None)

    def _confirm(self) -> None:
        pw = self.query_one("#vault-password-input", Input).value
        self.dismiss(pw if pw else None)


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

    def set_status(self, op_id: str, content: str) -> None:
        try:
            self.query_one(f"#opstatus-{op_id}", Static).update(content)
        except NoMatches:
            pass


# ── per-operation detail views ────────────────────────────────────────────────

class SyncDetailView(ScrollableContainer):
    """Base for Part1View / Part2View — owns the scanning elapsed-time timer."""

    def on_mount(self) -> None:
        self._scan_start: float | None = None
        self._scan_timer = None

    @property
    def _summary_static(self) -> Static:
        raise NotImplementedError

    def update_summary(
        self,
        summary: sync.DryRunSummary | None,
        *,
        scanning: bool,
        error: bool = False,
    ) -> None:
        if scanning:
            if self._scan_timer is None:
                self._scan_start = time.monotonic()
                self._scan_timer = self.set_interval(1.0, self._tick_elapsed)
            self._render_scanning()
        else:
            if self._scan_timer is not None:
                self._scan_timer.stop()
                self._scan_timer = None
            self._scan_start = None
            self._summary_static.update(_fmt_summary(summary, error=error))

    def update_sync_progress(self, transferred: int, total: int, elapsed: float) -> None:
        if self._scan_timer is not None:
            self._scan_timer.stop()
            self._scan_timer = None
            self._scan_start = None
        elapsed_s = int(elapsed)
        if total > 0:
            pct = min(100, transferred * 100 // total)
            text = (
                f"[bold yellow]Syncing…[/bold yellow]  "
                f"[bold]{transferred:,}[/bold] / {total:,} files  "
                f"[dim]({pct}%  {elapsed_s}s)[/dim]"
            )
        else:
            text = (
                f"[bold yellow]Syncing…[/bold yellow]  "
                f"[bold]{transferred:,}[/bold] files  "
                f"[dim]({elapsed_s}s)[/dim]"
            )
        self._summary_static.update(text)

    def _tick_elapsed(self) -> None:
        self._render_scanning()

    def _render_scanning(self) -> None:
        if self._scan_start is not None:
            elapsed = int(time.monotonic() - self._scan_start)
            self._summary_static.update(f"[dim]Scanning… ({elapsed}s)[/dim]")


class Part1View(SyncDetailView):
    def compose(self) -> ComposeResult:
        yield Label("[bold]Primary Save[/bold]")
        yield Label("", id="p1-status")
        yield Label("PENDING CHANGES", classes="section-hdr")
        yield Static("[dim]—[/dim]", id="p1-summary")
        yield Label("MAPPINGS", classes="section-hdr")
        yield Static("", id="p1-mappings")
        yield Label("FILES", classes="section-hdr")
        yield Static("", id="p1-files")
        with Horizontal(id="p1-vault-row"):
            yield Button("Backup Vault (v)", id="btn-vault-backup")
        yield Label("LOG", classes="section-hdr")
        yield RichLog(id="p1-log", highlight=True, markup=True)

    @property
    def _summary_static(self) -> Static:
        return self.query_one("#p1-summary", Static)

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
            lines = "  [yellow]No mappings configured — edit ~/.config/superguardian/config.yaml[/yellow]"
        self.query_one("#p1-mappings", Static).update(lines)

        files = cfg.get("laptop_to_save_a_files") or []
        if files:
            file_lines = "\n".join(
                f"  [dim]{f['from']}[/dim]  →  [dim]{f['to']}[/dim]"
                for f in files
            )
        else:
            file_lines = "  [dim]None configured[/dim]"
        self.query_one("#p1-files", Static).update(file_lines)

    @property
    def log(self) -> RichLog:
        return self.query_one("#p1-log", RichLog)


class Part2View(SyncDetailView):
    """Used for both SAVE_A→SAVE_B and SAVE_A→SAVE_C."""

    def __init__(self, op_id: str, dest_name: str, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._op_id = op_id
        self._dest_name = dest_name

    def compose(self) -> ComposeResult:
        dest = self._dest_name
        label = "Full Mirror" if dest == "SAVE_B" else "Offsite Mirror"
        yield Label(f"[bold]{label}[/bold]")
        yield Label("", id=f"p2-status-{self._op_id}")
        yield Label("PENDING CHANGES", classes="section-hdr")
        yield Static("[dim]—[/dim]", id=f"p2-summary-{self._op_id}")
        yield Label("LOG", classes="section-hdr")
        yield RichLog(id=f"p2-log-{self._op_id}", highlight=True, markup=True)

    @property
    def _summary_static(self) -> Static:
        return self.query_one(f"#p2-summary-{self._op_id}", Static)

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


_TABLE_LIMIT = 500  # max rows rendered; _pending always holds the full list


class Part3View(ScrollableContainer):
    def on_mount(self) -> None:
        self._pending: list[mdisc.PendingFile] = []
        self._scanning = False

    def compose(self) -> ComposeResult:
        yield Label("[bold]M-DISC Tracking[/bold]")
        yield Label("", id="p3-status")
        yield Label("PENDING FILES (not yet burned)", classes="section-hdr")
        table = DataTable(id="p3-table", zebra_stripes=True)
        table.add_columns("Path", "Size", "Reason")
        yield table
        yield Label("[dim]m: mark selected as burned   r: refresh[/dim]")

    def refresh_view(self, cfg: dict[str, Any]) -> None:
        self._scanning = True
        tracked = cfg.get("mdisc_tracked") or []
        available = sum(1 for d in tracked if Path(d).exists())
        self.query_one("#p3-status", Label).update(
            f"Tracked folders: [bold]{len(tracked)}[/bold]"
            f"  Available: [bold]{available}[/bold]"
            f"  [dim]Scanning…[/dim]"
        )
        self.query_one("#p3-table", DataTable).clear()
        self._do_scan(cfg)

    @work(thread=True)
    def _do_scan(self, cfg: dict[str, Any]) -> None:
        tracked = cfg.get("mdisc_tracked") or []
        pending = mdisc.scan_pending(tracked)
        self.app.call_from_thread(self._apply_scan, pending, cfg)

    def _apply_scan(self, pending: list[mdisc.PendingFile], cfg: dict[str, Any]) -> None:
        self._scanning = False
        self._pending = pending
        tracked = cfg.get("mdisc_tracked") or []
        available = sum(1 for d in tracked if Path(d).exists())
        total = mdisc.total_pending_size(pending)
        self.query_one("#p3-status", Label).update(
            f"Tracked folders: [bold]{len(tracked)}[/bold]"
            f"  Available: [bold]{available}[/bold]"
            f"  Pending: [bold]{len(pending)}[/bold] files ({total})"
        )
        table = self.query_one("#p3-table", DataTable)
        table.clear()
        for f in pending[:_TABLE_LIMIT]:
            table.add_row(f.path, mdisc.fmt_size(f.size), f.reason)
        if len(pending) > _TABLE_LIMIT:
            table.add_row(
                f"[dim]… {len(pending) - _TABLE_LIMIT:,} more files not shown[/dim]",
                "", "",
            )
        try:
            self.app.query_one("#ops-panel", OperationsPanel).set_status(
                "part3", f"M-DISC\n[dim]{len(pending)} pending[/dim]"
            )
        except Exception:
            pass

    def selected_files(self) -> list[mdisc.PendingFile]:
        table = self.query_one("#p3-table", DataTable)
        idx = table.cursor_row
        if idx < min(len(self._pending), _TABLE_LIMIT):
            return [self._pending[idx]]
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

class SuperGuardianApp(App):
    CSS = CSS
    TITLE = "Super Guardian"

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("d", "rescan", "Rescan"),
        Binding("s", "sync_op", "Sync"),
        Binding("r", "refresh", "Refresh"),
        Binding("m", "mark_burned", "Mark burned"),
        Binding("v", "vault_backup", "Vault backup"),
        Binding("R", "reload_config", "Reload config"),
        Binding("j,down", "cursor_down", "Down", show=False),
        Binding("k,up", "cursor_up", "Up", show=False),
    ]

    current_op: reactive[str] = reactive("part1")

    def __init__(self) -> None:
        super().__init__()
        self._cfg: dict[str, Any] = {}
        # int = file count, None = scan in progress, key absent = not yet scanned
        self._pending_counts: dict[str, int | None] = {}
        self._dry_run_summaries: dict[str, sync.DryRunSummary] = {}
        self._syncing_ops: set[str] = set()

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
        self._scan_all_ops()

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
        self._pending_counts.clear()
        self._dry_run_summaries.clear()
        self._refresh_all_views()
        self._scan_all_ops()

    def action_reload_config(self) -> None:
        self._cfg = config.load()
        self._pending_counts.clear()
        self._dry_run_summaries.clear()
        self._refresh_all_views()
        self._scan_all_ops()
        self.notify("Config reloaded.")

    def action_rescan(self) -> None:
        op = self.current_op
        if op in ("part3", "part4"):
            self.notify("No pending-file scan for this operation.", severity="warning")
            return
        self._pending_counts.pop(op, None)
        self._dry_run_summaries.pop(op, None)
        self._refresh_op_status(op)
        self._refresh_op_summary(op)
        self._scan_all_ops(ops=[op])

    def action_sync_op(self) -> None:
        if self.current_op == "part4":
            self.notify("Not implemented yet.", severity="warning")
            return
        self._start_sync()

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

    def _start_sync(self) -> None:
        op = self.current_op
        if op == "part3":
            self.notify("Use 'm' to manage M-DISC burns.", severity="warning")
            return
        self._check_risks_then_sync(op)

    @work(exclusive=True, group="sync")
    async def _check_risks_then_sync(self, op: str) -> None:
        log = self._get_log(op)
        log.clear()

        pairs = self._get_sync_pairs(op)
        file_pairs = self._get_file_pairs(op)
        if not pairs and not file_pairs:
            log.write("[yellow]No mappings configured for this operation.[/yellow]")
            return

        for src, _dst, _excl in pairs:
            if not Path(src).exists():
                log.write(f"[red]Source not found: {src}[/red]")
                return
        for src, _dst in file_pairs:
            if not Path(src).is_file():
                log.write(f"[red]Source file not found: {src}[/red]")
                return

        # Safety: reject destinations that are too shallow below their disc root.
        if op == "part1":
            disc_root = config.disc_path(self._cfg, "SAVE_A")
            for _, dst, _ in pairs:
                try:
                    sync.check_destination_safety(dst, disc_root)
                except ValueError as exc:
                    log.write(f"[red bold]SAFETY BLOCK:[/red bold] {exc}")
                    log.write("[red]Sync aborted — fix the 'to:' path in your config.[/red]")
                    return
        elif op == "part2_ab":
            src_root = config.disc_path(self._cfg, "SAVE_A")
            dst_root = config.disc_path(self._cfg, "SAVE_B")
            if Path(src_root).resolve() == Path(dst_root).resolve():
                log.write("[red bold]SAFETY BLOCK:[/red bold] SAVE_A and SAVE_B resolve to the same path.")
                return
        elif op == "part2_ac":
            src_root = config.disc_path(self._cfg, "SAVE_A")
            dst_root = config.disc_path(self._cfg, "SAVE_C")
            if Path(src_root).resolve() == Path(dst_root).resolve():
                log.write("[red bold]SAFETY BLOCK:[/red bold] SAVE_A and SAVE_C resolve to the same path.")
                return

        confirmed = await self.push_screen_wait(ConfirmSyncModal())
        if not confirmed:
            log.write("[dim]Cancelled.[/dim]")
            return

        await self._execute_sync(op, pairs)

    async def _execute_sync(
        self,
        op: str,
        pairs: list[tuple[str, str, list[str]]],
    ) -> None:
        log = self._get_log(op)

        # Mark syncing — shows "Syncing…" badge in left panel immediately
        self._syncing_ops.add(op)
        self._refresh_op_status(op)

        sync_start = time.monotonic()
        transferred = [0]
        total = (self._dry_run_summaries.get(op) or sync.DryRunSummary(0, 0, 0)).to_add

        sync_view: SyncDetailView | None = None
        try:
            sync_view = self._get_view(op)  # type: ignore[assignment]
            sync_view.update_sync_progress(0, total, 0.0)
        except Exception:
            pass

        def on_progress_line(line: str) -> None:
            log.write(line)
            if sync.is_transfer_line(line):
                transferred[0] += 1
                if sync_view is not None:
                    sync_view.update_sync_progress(
                        transferred[0], total,
                        time.monotonic() - sync_start,
                    )

        run_id = history.start_run(op, dry_run=False)
        total_files = 0
        full_log_parts: list[str] = []
        status = "success"

        for src, dst, exclude in pairs:
            log.write(f"\n[bold]{'─' * 40}[/bold]")
            log.write(f"[bold]{src}[/bold]  →  [bold]{dst}[/bold]")
            try:
                os.makedirs(dst, exist_ok=True)
                files, part_log = await sync.run_rsync(
                    src, dst,
                    dry_run=False,
                    exclude=exclude,
                    on_line=on_progress_line,
                )
                total_files += files
                full_log_parts.append(part_log)
            except RuntimeError as exc:
                log.write(f"[red]ERROR: {exc}[/red]")
                status = "error"
                break

        if status == "success":
            for src, dst in self._get_file_pairs(op):
                log.write(f"\n[bold]{'─' * 40}[/bold]")
                log.write(f"[bold]{src}[/bold]  →  [bold]{dst}[/bold]")
                try:
                    file_log = await sync.run_rsync_file(src, dst)
                    log.write(file_log)
                    total_files += 1
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

        self._syncing_ops.discard(op)
        self._refresh_op_status(op)

        if status == "success":
            log.write(f"\n[green]Sync complete — {total_files} file(s) transferred.[/green]")
            self._dry_run_summaries[op] = sync.DryRunSummary(to_add=0, to_move=0, to_delete=0)
            self._set_pending(op, 0)
        else:
            log.write(f"\n[red]Sync failed.[/red]")
            self._dry_run_summaries.pop(op, None)
            self._refresh_op_summary(op)

    # ── background pending-file scan ──────────────────────────────────────────

    @work(exclusive=True, group="scan")
    async def _scan_all_ops(self, ops: list[str] | None = None) -> None:
        """
        Sequentially count pending files for each op.  Sequential (not parallel)
        to avoid hammering SAVE_A from multiple rsync processes at once.
        """
        to_scan = ops or ["part1", "part2_ab", "part2_ac"]
        for op in to_scan:
            await self._scan_op(op)

    async def _scan_op(self, op: str) -> None:
        pairs = self._get_sync_pairs(op)
        file_pairs = self._get_file_pairs(op)
        if not pairs and not file_pairs:
            return

        all_accessible = all(
            Path(src).exists() and Path(dst).exists()
            for src, dst, _ in pairs
        ) and all(
            self._file_pair_accessible(src) for src, _dst in file_pairs
        )
        if not all_accessible:
            self._set_pending(op, _SCAN_UNAVAIL)
            return

        self._set_pending(op, None)  # show "scanning…"
        try:
            to_add = to_move = to_delete = 0
            for src, dst, excl in pairs:
                part = await sync.dry_run_summary(src, dst, excl)
                to_add += part.to_add
                to_move += part.to_move
                to_delete += part.to_delete
            for src, dst in file_pairs:
                to_add += await sync.count_pending_file(src, dst)
            summary = sync.DryRunSummary(to_add=to_add, to_move=to_move, to_delete=to_delete)
            self._dry_run_summaries[op] = summary
            self._set_pending(op, to_add)
        except Exception:
            self._dry_run_summaries.pop(op, None)
            self._set_pending(op, _SCAN_ERROR)

    def _set_pending(self, op: str, count: int | None) -> None:
        self._pending_counts[op] = count
        self._refresh_op_status(op)
        self._refresh_op_summary(op)

    def _refresh_op_summary(self, op: str) -> None:
        if op not in ("part1", "part2_ab", "part2_ac"):
            return
        try:
            view: SyncDetailView = self._get_view(op)  # type: ignore[assignment]
            if op not in self._pending_counts:
                view.update_summary(None, scanning=False)
            elif self._pending_counts[op] is None:
                view.update_summary(None, scanning=True)
            elif self._pending_counts[op] == _SCAN_ERROR:
                view.update_summary(None, scanning=False, error=True)
            elif self._pending_counts[op] == _SCAN_UNAVAIL:
                view.update_summary(None, scanning=False)
            else:
                view.update_summary(self._dry_run_summaries.get(op), scanning=False)
        except Exception:
            pass

    def _pending_label(self, op: str) -> str:
        if op not in self._pending_counts:
            return ""
        count = self._pending_counts[op]
        if count is None:
            return "[dim]scanning…[/dim]"
        if count == _SCAN_UNAVAIL:
            return ""
        if count == _SCAN_ERROR:
            return "[yellow dim]scan error[/yellow dim]"
        if count == 0:
            return "[green]✓ up to date[/green]"
        return f"[bold yellow]{count:,}[/bold yellow] [dim]files pending[/dim]"

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

    # ── vault backup ──────────────────────────────────────────────────────────

    def action_vault_backup(self) -> None:
        if self.current_op != "part1":
            self.notify("Vault backup is only available from Primary Save.", severity="warning")
            return
        self._start_vault_backup()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-vault-backup":
            self.action_vault_backup()

    @work(exclusive=True, group="vault")
    async def _start_vault_backup(self) -> None:
        log = self._get_log("part1")
        try:
            vcfg = vault.VaultConfig.from_dict(self._cfg)
        except vault.VaultError as exc:
            self.notify(str(exc), severity="error")
            return

        password = await self.push_screen_wait(VaultPasswordModal())
        if not password:
            return

        log.write("\n[bold]── Vault Backup ──[/bold]")

        def on_vault_log(line: str) -> None:
            if line.startswith("WARNING"):
                # A failed dismount means the vault may still be sitting
                # mounted and decrypted — too important to leave as just
                # another dim scrollback line the user might miss.
                log.write(f"[bold red]{line}[/bold red]")
                self.notify(line, severity="error", timeout=15)
            else:
                log.write(f"[dim]{line}[/dim]")

        try:
            await vault.run_backup(vcfg, password, log=on_vault_log)
            log.write("[green]Vault backup complete.[/green]")
            self.notify("Vault backup complete.")
            self._pending_counts.pop("part1", None)
            self._dry_run_summaries.pop("part1", None)
            self._scan_all_ops(ops=["part1"])
        except vault.VaultError as exc:
            log.write(f"[red]Vault backup failed: {exc}[/red]")
            self.notify("Vault backup failed — see log.", severity="error")
        finally:
            password = ""  # best-effort clear; original Input value may still be referenced

    # ── helpers ───────────────────────────────────────────────────────────────

    def _get_sync_pairs(self, op: str) -> list[tuple[str, str, list[str]]]:
        cfg = self._cfg
        if op == "part1":
            return [
                (m["from"], m["to"], list(m.get("exclude") or []))
                for m in (cfg.get("laptop_to_save_a") or [])
            ]
        if op == "part2_ab":
            return [(config.disc_path(cfg, "SAVE_A"), config.disc_path(cfg, "SAVE_B"), [])]
        if op == "part2_ac":
            return [(config.disc_path(cfg, "SAVE_A"), config.disc_path(cfg, "SAVE_C"), [])]
        return []

    def _get_file_pairs(self, op: str) -> list[tuple[str, str]]:
        """Individual-file mappings (as opposed to folders) — part1 only."""
        if op != "part1":
            return []
        return [
            (f["from"], f["to"])
            for f in (self._cfg.get("laptop_to_save_a_files") or [])
        ]

    def _file_pair_accessible(self, src: str) -> bool:
        # Destination file may not exist yet on the first-ever backup — what
        # matters is whether the source file and the SAVE_A disc are there.
        return Path(src).is_file() and is_mounted(config.disc_path(self._cfg, "SAVE_A"))

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
                self._refresh_op_summary(op_id)
            except Exception:
                pass

    def _refresh_op_status(self, op_id: str) -> None:
        panel = self.query_one("#ops-panel", OperationsPanel)
        cfg = self._cfg

        def badge(ok: bool) -> str:
            return "[green]●[/green]" if ok else "[red]✗[/red]"

        if op_id == "part1":
            a_ok = is_mounted(config.disc_path(cfg, "SAVE_A"))
            last = history.last_successful_sync("part1")
            last_str = _ago(last["finished_at"]) if last else "never"
            extra = (
                "\n[bold yellow]Syncing…[/bold yellow]"
                if op_id in self._syncing_ops
                else (f"\n{p}" if (p := self._pending_label(op_id)) else "")
            )
            panel.set_status(op_id,
                f"Laptop → SAVE_A {badge(a_ok)}\n"
                f"[dim]Last: {last_str}[/dim]"
                + extra)
        elif op_id == "part2_ab":
            a_ok = is_mounted(config.disc_path(cfg, "SAVE_A"))
            b_ok = is_mounted(config.disc_path(cfg, "SAVE_B"))
            last = history.last_successful_sync("part2_ab")
            last_str = _ago(last["finished_at"]) if last else "never"
            extra = (
                "\n[bold yellow]Syncing…[/bold yellow]"
                if op_id in self._syncing_ops
                else (f"\n{p}" if (p := self._pending_label(op_id)) else "")
            )
            panel.set_status(op_id,
                f"SAVE_A {badge(a_ok)} → SAVE_B {badge(b_ok)}\n"
                f"[dim]Last: {last_str}[/dim]"
                + extra)
        elif op_id == "part2_ac":
            a_ok = is_mounted(config.disc_path(cfg, "SAVE_A"))
            c_ok = is_mounted(config.disc_path(cfg, "SAVE_C"))
            last = history.last_successful_sync("part2_ac")
            last_str = _ago(last["finished_at"]) if last else "never"
            extra = (
                "\n[bold yellow]Syncing…[/bold yellow]"
                if op_id in self._syncing_ops
                else (f"\n{p}" if (p := self._pending_label(op_id)) else "")
            )
            panel.set_status(op_id,
                f"SAVE_A {badge(a_ok)} → SAVE_C {badge(c_ok)}\n"
                f"[dim]Last: {last_str}[/dim]"
                + extra)
        elif op_id == "part3":
            try:
                view = self.query_one("#detail-part3", Part3View)
                if view._scanning:
                    panel.set_status(op_id, "M-DISC\n[dim]scanning…[/dim]")
                else:
                    panel.set_status(op_id, f"M-DISC\n[dim]{len(view._pending)} pending[/dim]")
            except Exception:
                pass
        elif op_id == "part4":
            panel.set_status(op_id, "Media\n[dim](coming soon)[/dim]")

    def _update_detail_title(self, op_id: str) -> None:
        label = next((lbl for oid, lbl, _ in OPERATIONS if oid == op_id), op_id)
        try:
            self.query_one("#detail-title", Label).update(f"  {label}  ")
        except NoMatches:
            pass
