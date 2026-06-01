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
    ("part1",    "Primary Save",   ""),
    ("part2_ab", "Full Mirror",    ""),
    ("part2_ac", "Offsite Mirror", ""),
    ("part3",    "M-DISC",         ""),
    ("part4",    "Media",          "(coming soon)"),
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

    BINDINGS = [
        Binding("j", "cursor_down", show=False),
        Binding("k", "cursor_up", show=False),
        Binding("y", "confirm_yes", show=False),
        Binding("n", "confirm_no", show=False),
        Binding("escape", "confirm_no", show=False),
    ]

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
                yield ListView(
                    *[ListItem(Label(f"[red]{r.rel_path}[/red]")) for r in permanent],
                    id="confirm-list",
                )
            elif self._risks:
                yield Label(
                    f"\n[yellow]{len(self._risks)} file(s) will be removed from destination[/yellow]\n"
                    "(copies exist in source — likely moved):"
                )
                yield ListView(
                    *[ListItem(Label(f"[dim]{r.rel_path}[/dim]")) for r in self._risks],
                    id="confirm-list",
                )
            else:
                yield Label("\n[green]No deletions detected.[/green]")
            yield Label("\nDo you want to continue?  [dim]y / n[/dim]")
            with Horizontal(id="confirm-buttons"):
                yield Button("Yes", variant="warning", id="btn-yes")
                yield Button("No", variant="default", id="btn-no")

    def on_mount(self) -> None:
        try:
            self.query_one("#confirm-list", ListView).focus()
        except NoMatches:
            self.query_one("#btn-no", Button).focus()

    def action_cursor_down(self) -> None:
        try:
            self.query_one("#confirm-list", ListView).action_cursor_down()
        except NoMatches:
            pass

    def action_cursor_up(self) -> None:
        try:
            self.query_one("#confirm-list", ListView).action_cursor_up()
        except NoMatches:
            pass

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

class Part1View(ScrollableContainer):
    def compose(self) -> ComposeResult:
        yield Label("[bold]Primary Save[/bold]")
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
        yield Label(f"[bold]{label}[/bold]")
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
        """For dry-run: check deletion risks first. For sync: confirm immediately."""
        log = self._get_log(op)
        log.clear()

        pairs = self._get_sync_pairs(op)
        if not pairs:
            log.write("[yellow]No mappings configured for this operation.[/yellow]")
            return

        for src, dst, _exclude in pairs:
            if not Path(src).exists():
                log.write(f"[red]Source not found: {src}[/red]")
                return

        all_risks: list[sync.DeletionRisk] = []
        if dry_run:
            for src, dst, exclude in pairs:
                if not Path(dst).exists():
                    log.write(f"[yellow]Destination not found (will be created): {dst}[/yellow]")
                    continue
                log.write(f"[dim]Checking for deletion risks: {src} → {dst}[/dim]")
                try:
                    risks = await sync.list_deletion_risks(src, dst, exclude)
                    all_risks.extend(risks)
                except Exception as exc:
                    log.write(f"[red]Risk check failed: {exc}[/red]")
                    return

        confirmed = await self.push_screen_wait(ConfirmSyncModal(all_risks, dry_run))
        if not confirmed:
            log.write("[dim]Cancelled.[/dim]")
            return

        await self._execute_sync(op, pairs, dry_run=dry_run)

    async def _execute_sync(
        self,
        op: str,
        pairs: list[tuple[str, str, list[str]]],
        *,
        dry_run: bool,
    ) -> None:
        log = self._get_log(op)
        run_id = history.start_run(op, dry_run=dry_run)
        total_files = 0
        full_log_parts: list[str] = []
        status = "success"
        mode = "[dim](dry-run)[/dim]" if dry_run else ""

        for src, dst, exclude in pairs:
            log.write(f"\n[bold]{'─' * 40}[/bold]")
            log.write(f"[bold]{src}[/bold]  →  [bold]{dst}[/bold]  {mode}")
            try:
                os.makedirs(dst, exist_ok=True)
                files, part_log = await sync.run_rsync(
                    src, dst,
                    dry_run=dry_run,
                    exclude=exclude,
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

        def badge(ok: bool) -> str:
            return "[green]●[/green]" if ok else "[red]✗[/red]"

        if op_id == "part1":
            a_ok = is_mounted(config.disc_path(cfg, "SAVE_A"))
            last = history.last_successful_sync("part1")
            last_str = _ago(last["finished_at"]) if last else "never"
            panel.set_status(op_id,
                f"Laptop → SAVE_A {badge(a_ok)}\n[dim]Last: {last_str}[/dim]")
        elif op_id == "part2_ab":
            a_ok = is_mounted(config.disc_path(cfg, "SAVE_A"))
            b_ok = is_mounted(config.disc_path(cfg, "SAVE_B"))
            last = history.last_successful_sync("part2_ab")
            last_str = _ago(last["finished_at"]) if last else "never"
            panel.set_status(op_id,
                f"SAVE_A {badge(a_ok)} → SAVE_B {badge(b_ok)}\n[dim]Last: {last_str}[/dim]")
        elif op_id == "part2_ac":
            a_ok = is_mounted(config.disc_path(cfg, "SAVE_A"))
            c_ok = is_mounted(config.disc_path(cfg, "SAVE_C"))
            last = history.last_successful_sync("part2_ac")
            last_str = _ago(last["finished_at"]) if last else "never"
            panel.set_status(op_id,
                f"SAVE_A {badge(a_ok)} → SAVE_C {badge(c_ok)}\n[dim]Last: {last_str}[/dim]")
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
