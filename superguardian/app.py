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

from rich.text import Text

from . import config, git as git_ops, history, mdisc, sync
from .config import is_mounted

# ── constants ─────────────────────────────────────────────────────────────────

OPERATIONS: list[tuple[str, str, str]] = [
    ("git",      "Git Repos",      ""),
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


class GitView(ScrollableContainer):
    """Git repositories tab — scan status and push-all."""

    def on_mount(self) -> None:
        self._repos: list[git_ops.RepoStatus] = []
        self._scanning = False
        self._pushing = False

    def compose(self) -> ComposeResult:
        yield Label("[bold]Git Repositories[/bold]")
        yield Label("", id="git-status")
        yield Label("REPOSITORIES", classes="section-hdr")
        table = DataTable(id="git-table", zebra_stripes=True)
        table.add_columns("Name", "Type", "Branch", "Dirty", "Unpushed", "Remotes")
        yield table
        yield Label("[dim]s: push all   d: rescan[/dim]")
        yield Label("PUSH LOG", classes="section-hdr")
        yield RichLog(id="git-log", highlight=True, markup=True)

    def refresh_view(self, cfg: dict[str, Any]) -> None:
        pass  # data arrives via refresh_status() after background scan

    def refresh_status(
        self,
        repos: list[git_ops.RepoStatus],
        *,
        scanning: bool,
    ) -> None:
        self._repos = repos
        self._scanning = scanning

        if scanning:
            self.query_one("#git-status", Label).update("[dim]Scanning repositories…[/dim]")
            self.query_one("#git-table", DataTable).clear()
            return

        total = len(repos)
        errors = sum(1 for r in repos if r.error)
        total_unpushed = sum(r.total_unpushed for r in repos if not r.error)
        never_pushed = sum(
            1 for r in repos
            if not r.error and r.remotes and r.untracked_branches
        )

        if total == 0:
            status = "[dim]No repositories configured — add git_repos to config.yaml[/dim]"
        else:
            parts = [f"[bold]{total}[/bold] repos"]
            if total_unpushed:
                parts.append(f"[bold yellow]{total_unpushed}[/bold yellow] unpushed commits")
            if never_pushed:
                parts.append(f"[bold magenta]{never_pushed}[/bold magenta] never pushed")
            if not total_unpushed and not never_pushed:
                parts.append("[green]✓ all pushed[/green]")
            if errors:
                parts.append(f"[red]{errors} errors[/red]")
            status = "  ".join(parts)
        self.query_one("#git-status", Label).update(status)

        table = self.query_one("#git-table", DataTable)
        table.clear()
        for r in repos:
            if r.error:
                table.add_row(
                    Text(r.name, style="dim"),
                    Text("?"),
                    Text("?"),
                    Text("?"),
                    Text("?"),
                    Text(r.error, style="red"),
                    key=r.path,
                )
                continue
            repo_type = Text("bare", style="dim") if r.is_bare else Text("normal")
            branch = Text(
                r.current_branch or ("bare" if r.is_bare else "detached"),
                style="dim" if not r.current_branch else "",
            )
            if r.is_bare and r.current_branch is None:
                dirty = Text("—", style="dim")
            elif r.dirty:
                dirty = Text("✗ dirty", style="yellow")
            else:
                dirty = Text("✓ clean", style="green")
            if not r.remotes:
                unpushed_text = Text("—", style="dim")
            elif r.untracked_branches and r.total_unpushed == 0:
                unpushed_text = Text("? never pushed", style="magenta")
            elif r.total_unpushed == 0:
                unpushed_text = Text("✓ 0", style="green")
            else:
                unpushed_text = Text(str(r.total_unpushed), style="bold yellow")
            remotes_str = ", ".join(r.remotes) if r.remotes else "none"
            remotes_text = Text(remotes_str, style="dim" if not r.remotes else "")
            table.add_row(
                r.name, repo_type, branch, dirty, unpushed_text, remotes_text,
                key=r.path,
            )

    @property
    def log(self) -> RichLog:
        return self.query_one("#git-log", RichLog)


class Part1View(SyncDetailView):
    def compose(self) -> ComposeResult:
        yield Label("[bold]Primary Save[/bold]")
        yield Label("", id="p1-status")
        yield Label("PENDING CHANGES", classes="section-hdr")
        yield Static("[dim]—[/dim]", id="p1-summary")
        yield Label("MAPPINGS", classes="section-hdr")
        yield Static("", id="p1-mappings")
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
        if not (cfg.get("laptop_to_save_a") or []):
            self.query_one("#p1-mappings", Static).update(
                "  [yellow]No mappings configured — edit ~/.config/superguardian/config.yaml[/yellow]"
            )
        # else: update_mapping_summaries() owns the MAPPINGS display

    def update_mapping_summaries(
        self,
        pairs: list[tuple[str, str, list[str]]],
        summaries: list[Any],
    ) -> None:
        if not pairs:
            return
        lines = []
        for i, (src, dst, _) in enumerate(pairs):
            if i >= len(summaries):
                status = "[dim]—[/dim]"
            else:
                s = summaries[i]
                if s is None:
                    status = "[dim]scanning…[/dim]"
                elif s == _SCAN_UNAVAIL:
                    status = (
                        "[red]✗ source not found[/red]"
                        if not Path(src).exists()
                        else "[yellow]⚠ disc not mounted[/yellow]"
                    )
                elif s == _SCAN_ERROR:
                    status = "[yellow dim]scan error[/yellow dim]"
                else:
                    status = _fmt_summary(s)
            lines.append(
                f"  [dim]{src}[/dim]\n"
                f"  [dim]→ {dst}[/dim]\n"
                f"  {status}"
            )
        self.query_one("#p1-mappings", Static).update("\n\n".join(lines))

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
        Binding("R", "reload_config", "Reload config"),
        Binding("j,down", "cursor_down", "Down", show=False),
        Binding("k,up", "cursor_up", "Up", show=False),
    ]

    current_op: reactive[str] = reactive("git")

    def __init__(self) -> None:
        super().__init__()
        self._cfg: dict[str, Any] = {}
        # int = file count, None = scan in progress, key absent = not yet scanned
        self._pending_counts: dict[str, int | None] = {}
        self._dry_run_summaries: dict[str, sync.DryRunSummary] = {}
        # per-mapping scan results: None=scanning, int sentinel=error/unavail, DryRunSummary=done
        self._mapping_summaries: dict[str, list[Any]] = {}
        self._syncing_ops: set[str] = set()

    # ── compose ───────────────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        with Horizontal(id="body"):
            yield OperationsPanel(id="ops-panel")
            with Vertical(id="detail-panel"):
                yield Label("", id="detail-title", classes="panel-title")
                with ContentSwitcher(initial="detail-git", id="switcher"):
                    yield GitView(id="detail-git", classes="part-view")
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
        try:
            self._cfg = config.load()
        except config.ConfigError as exc:
            self.notify(str(exc), severity="error", timeout=60)
        self._refresh_all_views()
        self._scan_all_ops()
        self._start_git_scan()
        self._update_detail_title("git")

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
        try:
            self._cfg = config.load()
        except config.ConfigError as exc:
            self.notify(str(exc), severity="error", timeout=60)
            return
        self._pending_counts.clear()
        self._dry_run_summaries.clear()
        self._mapping_summaries.clear()
        self._refresh_all_views()
        self._scan_all_ops()
        self._start_git_scan()

    def action_reload_config(self) -> None:
        try:
            self._cfg = config.load()
        except config.ConfigError as exc:
            self.notify(str(exc), severity="error", timeout=60)
            return
        self._pending_counts.clear()
        self._dry_run_summaries.clear()
        self._mapping_summaries.clear()
        self._refresh_all_views()
        self._scan_all_ops()
        self._start_git_scan()
        self.notify("Config reloaded.")

    def action_rescan(self) -> None:
        op = self.current_op
        if op == "git":
            self._start_git_scan()
            return
        if op in ("part3", "part4"):
            self.notify("No pending-file scan for this operation.", severity="warning")
            return
        self._pending_counts.pop(op, None)
        self._dry_run_summaries.pop(op, None)
        self._mapping_summaries.pop(op, None)
        self._refresh_op_status(op)
        self._refresh_op_summary(op)
        self._refresh_mapping_summaries(op)
        self._scan_all_ops(ops=[op])

    def action_sync_op(self) -> None:
        if self.current_op == "git":
            self._git_push_all()
            return
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
        if not pairs:
            log.write("[yellow]No mappings configured for this operation.[/yellow]")
            return

        for src, _dst, _excl in pairs:
            if not Path(src).exists():
                log.write(f"[red]Source not found: {src}[/red]")
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
            self._mapping_summaries.pop(op, None)
            self._dry_run_summaries[op] = sync.DryRunSummary(to_add=0, to_move=0, to_delete=0)
            self._set_pending(op, 0)
            self._refresh_mapping_summaries(op)
        else:
            log.write(f"\n[red]Sync failed.[/red]")
            self._dry_run_summaries.pop(op, None)
            self._refresh_op_summary(op)

    # ── git scan & push ───────────────────────────────────────────────────────

    def _start_git_scan(self) -> None:
        try:
            view = self.query_one("#detail-git", GitView)
            view.refresh_status([], scanning=True)
        except Exception:
            pass
        self._refresh_op_status("git")
        self._do_scan_git_repos()

    @work(thread=True)
    def _do_scan_git_repos(self) -> None:
        entries = self._cfg.get("git_repos") or []
        repos = []
        for entry in entries:
            if isinstance(entry, dict):
                path = str(Path(entry["path"]).expanduser())
                worktree = entry.get("worktree")
                worktree = str(Path(worktree).expanduser()) if worktree else None
            else:
                path = str(Path(entry).expanduser())
                worktree = None
            repos.append(git_ops.scan_repo(path, worktree=worktree))
        self.call_from_thread(self._apply_git_scan, repos)

    def _apply_git_scan(self, repos: list[git_ops.RepoStatus]) -> None:
        try:
            view = self.query_one("#detail-git", GitView)
            view.refresh_status(repos, scanning=False)
        except Exception:
            pass
        self._refresh_op_status("git")

    @work(exclusive=True, group="git-push")
    async def _git_push_all(self) -> None:
        view = self.query_one("#detail-git", GitView)
        log = view.log
        log.clear()
        repos = list(view._repos)

        pushable = [r for r in repos if not r.error and r.remotes]
        if not repos:
            log.write("[yellow]No repositories configured — add git_repos to config.yaml[/yellow]")
            return
        if not pushable:
            log.write("[yellow]No repositories have remotes configured — nothing to push.[/yellow]")
            return

        view._pushing = True
        self._refresh_op_status("git")

        any_fail = False
        for repo in repos:
            if repo.error:
                log.write(f"\n[dim]── {repo.name}: skipped ({repo.error})[/dim]")
                continue
            if not repo.remotes:
                log.write(f"\n[dim]── {repo.name}: no remotes, skipped[/dim]")
                continue
            log.write(f"\n[bold cyan]── {repo.name}[/bold cyan]  [dim]{repo.path}[/dim]")
            ok = await git_ops.push_all_branches(repo.path, log.write)
            if not ok:
                any_fail = True

        view._pushing = False

        if any_fail:
            log.write("\n[bold red]Some pushes failed.[/bold red]")
        else:
            log.write("\n[bold green]All pushes complete.[/bold green]")

        self._start_git_scan()

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
        if not pairs:
            return

        n = len(pairs)
        self._mapping_summaries[op] = [None] * n
        self._set_pending(op, None)
        self._refresh_mapping_summaries(op)

        to_add = to_move = to_delete = 0
        any_success = False
        for i, (src, dst, excl) in enumerate(pairs):
            if not Path(src).exists():
                self._mapping_summaries[op][i] = _SCAN_UNAVAIL
                continue
            if not Path(dst).exists():
                self._mapping_summaries[op][i] = _SCAN_UNAVAIL
                continue
            try:
                part = await sync.dry_run_summary(src, dst, excl)
                self._mapping_summaries[op][i] = part
                to_add += part.to_add
                to_move += part.to_move
                to_delete += part.to_delete
                any_success = True
            except Exception:
                self._mapping_summaries[op][i] = _SCAN_ERROR

        self._refresh_mapping_summaries(op)

        if any_success:
            self._dry_run_summaries[op] = sync.DryRunSummary(
                to_add=to_add, to_move=to_move, to_delete=to_delete
            )
            self._set_pending(op, to_add)
        elif all(s == _SCAN_UNAVAIL for s in self._mapping_summaries[op]):
            self._dry_run_summaries.pop(op, None)
            self._set_pending(op, _SCAN_UNAVAIL)
        else:
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

    def _refresh_mapping_summaries(self, op: str) -> None:
        if op != "part1":
            return
        try:
            view = self.query_one("#detail-part1", Part1View)
            pairs = self._get_sync_pairs(op)
            summaries = self._mapping_summaries.get(op, [])
            view.update_mapping_summaries(pairs, summaries)
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

    def _get_view(self, op: str) -> GitView | Part1View | Part2View | Part3View | Part4View:
        return self.query_one(f"#detail-{op}")  # type: ignore[return-value]

    def _refresh_all_views(self) -> None:
        cfg = self._cfg
        for op_id, _, _ in OPERATIONS:
            try:
                self._get_view(op_id).refresh_view(cfg)  # type: ignore[union-attr]
                self._refresh_op_status(op_id)
                self._refresh_op_summary(op_id)
                self._refresh_mapping_summaries(op_id)
            except Exception:
                pass

    def _refresh_op_status(self, op_id: str) -> None:
        panel = self.query_one("#ops-panel", OperationsPanel)
        cfg = self._cfg

        def badge(ok: bool) -> str:
            return "[green]●[/green]" if ok else "[red]✗[/red]"

        if op_id == "git":
            try:
                view = self.query_one("#detail-git", GitView)
                if view._scanning:
                    panel.set_status("git", "Git Repos\n[dim]scanning…[/dim]")
                elif view._pushing:
                    panel.set_status("git", "Git Repos\n[bold yellow]Pushing…[/bold yellow]")
                else:
                    repos = view._repos
                    total_unpushed = sum(r.total_unpushed for r in repos if not r.error)
                    never_pushed = sum(
                        1 for r in repos
                        if not r.error and r.remotes and r.untracked_branches
                    )
                    if not repos:
                        panel.set_status("git", "Git Repos\n[dim]—[/dim]")
                    elif total_unpushed:
                        panel.set_status(
                            "git",
                            f"Git Repos\n[bold yellow]{total_unpushed}[/bold yellow] [dim]unpushed[/dim]",
                        )
                    elif never_pushed:
                        panel.set_status(
                            "git",
                            f"Git Repos\n[magenta]{never_pushed}[/magenta] [dim]never pushed[/dim]",
                        )
                    else:
                        panel.set_status("git", f"Git Repos\n[green]✓ all pushed[/green]")
            except Exception:
                panel.set_status("git", "Git Repos\n[dim]—[/dim]")
        elif op_id == "part1":
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
