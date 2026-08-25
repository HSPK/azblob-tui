"""Interactive Azure Blob Storage browser with AML snapshot filtering."""

from __future__ import annotations

import curses
import sqlite3
import time
from dataclasses import replace
from pathlib import Path
from typing import Iterable
from urllib.parse import quote

from .azure import AppError, AzureCatalog, StorageError
from .blob import (
    BlobRestClient,
    human_count,
    human_size,
    list_hierarchy_page,
    list_containers,
)
from .filters import is_aml_snapshot
from .models import (
    BlobItem,
    ContainerItem,
    ContainerUsage,
    FolderUsage,
    StorageAccount,
    Subscription,
)
from .scan_paths import resolve_scan_paths
from .state import load_container_folder_usage, load_usage_state
from .stats import AccountQueueStats, QueueStats, StatsProvider
from .table import Column, fit_columns, render_header, render_row


REFRESH_TIMEOUT_MS = 1000


def shorten(value: str, width: int) -> str:
    if width <= 0:
        return ""
    if len(value) <= width:
        return value
    if width <= 3:
        return value[:width]
    return f"{value[: width - 3]}..."


def safe_addstr(
    window: curses.window,
    y: int,
    x: int,
    value: str,
    attributes: int = 0,
) -> None:
    height, width = window.getmaxyx()
    if y < 0 or y >= height or x < 0 or x >= width:
        return
    try:
        window.addstr(y, x, value[: max(width - x - 1, 0)], attributes)
    except curses.error:
        pass


class BlobBrowser:
    def __init__(
        self,
        catalog: AzureCatalog,
        state_path: Path | None,
        stats_provider: StatsProvider | None,
        page_size: int,
        show_snapshots: bool,
        initial_subscription: Subscription | None,
        initial_account: str | None,
        state_path_override: Path | None = None,
        scan_log_path_override: Path | None = None,
        scan_path_working_directory: Path | None = None,
    ) -> None:
        self.catalog = catalog
        self.subscriptions = catalog.subscriptions
        self.subscription = initial_subscription
        self.accounts: list[StorageAccount] = []
        self.client: BlobRestClient | None = None
        self.snapshot_prefixes: dict[str, set[str]] = {}
        self.state_path = state_path
        self.stats_provider = stats_provider
        self.state_path_override = state_path_override
        self.scan_log_path_override = scan_log_path_override
        self.scan_path_working_directory = (
            scan_path_working_directory or Path.cwd()
        )
        self.stats: QueueStats | None = None
        self.last_stats_refresh = 0.0
        self.page_size = page_size
        self.show_snapshots = show_snapshots
        self.show_deleted = False
        self.initial_account = initial_account

        self.screen = "accounts" if initial_subscription else "subscriptions"
        self.previous_screen = self.screen
        self.subscription_picker_return_screen: str | None = None
        self.containers_return_screen: str | None = None
        self.account: StorageAccount | None = None
        self.container = ""
        self.prefix = ""
        self.query = ""
        self.selected = 0
        self.offset = 0
        self.detail_offset = 0
        self.status = "Ready"
        self.sort_keys = {
            "subscriptions": "name",
            "accounts": "name",
            "containers": "name",
            "blobs": "name",
            "stats": "size",
        }
        self.sort_descending = {
            "subscriptions": False,
            "accounts": False,
            "containers": False,
            "blobs": False,
            "stats": True,
        }

        self.container_items: list[ContainerItem] = []
        self.container_usage: dict[str, ContainerUsage] = {}
        self.folder_usage: dict[tuple[int, str], FolderUsage] = {}
        self.hidden_snapshot_count = 0

        self.blob_items: list[BlobItem] = []
        self.current_marker = ""
        self.next_marker = ""
        self.marker_history: list[str] = []
        self.service_pages_loaded = 0

        self.window: curses.window | None = None

    def run(self, window: curses.window) -> None:
        self.window = window
        curses.curs_set(0)
        window.keypad(True)
        window.timeout(REFRESH_TIMEOUT_MS)
        self._init_colors()
        if self.subscription is not None:
            try:
                self._load_subscription()
                if self.initial_account:
                    account = next(
                        (
                            item
                            for item in self.accounts
                            if item.name.lower()
                            == self.initial_account.lower()
                        ),
                        None,
                    )
                    if account is None:
                        raise AppError(
                            f"Storage account not found: {self.initial_account}"
                        )
                    self.account = account
                    self.screen = "containers"
                    self._load_containers()
            except StorageError as error:
                self._modal(
                    "Azure Storage error",
                    str(error).splitlines(),
                )
                self.account = None
                self.screen = "accounts"
            except AppError as error:
                self._modal("Error", str(error).splitlines())
                self.screen = "subscriptions"
                self.subscription = None

        while True:
            self._refresh_stats_if_due()
            self._draw()
            key = window.getch()
            if key == -1:
                continue
            try:
                self._handle_key(key)
            except StorageError as error:
                self._modal("Azure Storage error", str(error).splitlines())
            except AppError as error:
                self._modal("Error", str(error).splitlines())

    @staticmethod
    def _init_colors() -> None:
        if not curses.has_colors():
            return
        curses.start_color()
        curses.use_default_colors()
        curses.init_pair(1, curses.COLOR_CYAN, -1)
        curses.init_pair(2, curses.COLOR_BLACK, curses.COLOR_CYAN)
        curses.init_pair(3, curses.COLOR_YELLOW, -1)
        curses.init_pair(4, curses.COLOR_RED, -1)
        curses.init_pair(5, curses.COLOR_GREEN, -1)

    @staticmethod
    def _color(pair: int) -> int:
        return curses.color_pair(pair) if curses.has_colors() else 0

    def _draw(self) -> None:
        assert self.window is not None
        self.window.erase()
        height, width = self.window.getmaxyx()
        if height < 8 or width < 50:
            safe_addstr(
                self.window,
                0,
                0,
                "Terminal must be at least 50 columns by 8 rows.",
                self._color(4),
            )
            self.window.refresh()
            return

        title = self._title()
        safe_addstr(
            self.window,
            0,
            0,
            " " + shorten(title, width - 2).ljust(width - 2),
            self._color(2) | curses.A_BOLD,
        )
        rows = self._visible_rows()
        list_height = height - 4
        self._normalize_selection(len(rows), list_height)

        details_width = max(36, width // 3) if width >= 100 else 0
        list_width = width - details_width - (1 if details_width else 0)
        fitted_columns = fit_columns(
            self._columns(),
            max(list_width - 1, 1),
        )
        safe_addstr(
            self.window,
            1,
            0,
            render_header(fitted_columns),
            curses.A_BOLD | self._color(1),
        )
        if details_width:
            divider_x = list_width
            for y in range(1, height - 2):
                safe_addstr(self.window, y, divider_x, "|")

        for visible_index in range(list_height):
            row_index = self.offset + visible_index
            if row_index >= len(rows):
                break
            attributes = (
                curses.A_REVERSE | curses.A_BOLD
                if row_index == self.selected
                else 0
            )
            safe_addstr(
                self.window,
                visible_index + 2,
                0,
                render_row(
                    fitted_columns,
                    self._row_values(rows[row_index]),
                ),
                attributes,
            )

        if details_width:
            queue_lines = self._queue_lines()
            queue_height = min(
                len(queue_lines) + 1,
                max(list_height * 3 // 5, 3),
                list_height,
            )
            queue_top = 2 + list_height - queue_height
            detail_height = max(queue_top - 2, 0)
            if rows and detail_height:
                self._draw_details(
                    rows[self.selected],
                    x=list_width + 2,
                    width=details_width - 2,
                    height=detail_height,
                )
            self._draw_queue(
                x=list_width + 2,
                width=details_width - 2,
                top=queue_top,
                lines=queue_lines,
            )

        status = self.status
        if self.query:
            status = f"{status} | filter={self.query!r}"
        safe_addstr(
            self.window,
            height - 2,
            0,
            shorten(status, width - 1),
            self._color(3),
        )
        help_text = self._help_line()
        safe_addstr(
            self.window,
            height - 1,
            0,
            shorten(help_text, width - 1),
            self._color(1),
        )
        self.window.refresh()

    def _title(self) -> str:
        if self.screen == "stats":
            if self.stats is None:
                return "Azure Blob Browser | Statistics unavailable"
            return (
                f"Azure Blob Browser | Statistics | "
                f"done {self.stats.completed}/{self.stats.total} | "
                f"partial {self.stats.partial} | "
                f"pending/failed {self.stats.pending_or_failed}"
            )
        if self.screen == "subscriptions":
            return (
                f"Azure Blob Browser | Subscriptions "
                f"({len(self.subscriptions)})"
            )
        if self.screen == "accounts":
            return (
                f"Azure Blob Browser | {self.subscription.name} | "
                f"Storage accounts ({len(self.accounts)})"
            )
        if self.screen == "containers":
            hidden = (
                ""
                if self.show_snapshots
                else f" | hidden AML snapshots: {self.hidden_snapshot_count}"
            )
            return (
                f"Azure Blob Browser | {self.account.name} | "
                f"Containers{hidden} | soft deleted: "
                f"{'shown' if self.show_deleted else 'hidden'}"
            )
        return (
            f"Azure Blob Browser | {self.account.name}/{self.container}/"
            f"{self.prefix} | soft deleted: "
            f"{'shown' if self.show_deleted else 'hidden'}"
        )

    def _help_line(self) -> str:
        common = (
            "Arrows move  Enter open  q back  / filter  "
            "h help  Ctrl+C exit"
        )
        if self.screen == "stats":
            return f"Auto-refresh 1s  r refresh | {common}"
        if self.screen in ("subscriptions", "accounts"):
            return common
        if self.screen == "containers":
            return f"u UUID  x soft-deleted | {common}"
        return f"x soft-deleted  n/p page  i details | {common}"

    def _visible_rows(self) -> list[object]:
        query = self.query.lower()
        if self.screen == "stats":
            rows: list[object] = (
                list(self.stats.accounts)
                if self.stats is not None
                else []
            )
            if query:
                rows = [
                    row
                    for row in rows
                    if query in row.account.lower()
                ]
            return self._sort_rows(rows)
        if self.screen == "subscriptions":
            rows: list[object] = self.subscriptions
            if query:
                rows = [
                    subscription
                    for subscription in rows
                    if query
                    in (
                        f"{subscription.name} {subscription.id} "
                        f"{subscription.tenant_id}"
                    ).lower()
                ]
            return self._sort_rows(rows)
        if self.screen == "accounts":
            rows = self.accounts
            if query:
                rows = [
                    account
                    for account in rows
                    if query
                    in (
                        f"{account.name} {account.resource_group} "
                        f"{account.location}"
                    ).lower()
                ]
            return self._sort_rows(rows)

        if self.screen == "containers":
            items = self.container_items
            if not self.show_deleted:
                items = [item for item in items if not item.is_deleted]
            if not self.show_snapshots:
                items = [
                    item
                    for item in items
                    if not self._is_aml_snapshot(item.name)
                ]
            if query:
                items = [
                    item
                    for item in items
                    if query
                    in (
                        f"{item.name} {item.deleted_time} {item.version}"
                    ).lower()
                ]
            return self._sort_rows(list(items))

        rows = list(self.blob_items)
        if query:
            rows = [
                item
                for item in rows
                if query in item.name.lower()
            ]
        return self._sort_rows(rows)

    def _sort_rows(self, rows: list[object]) -> list[object]:
        key = self.sort_keys.get(self.screen, "name")
        descending = self.sort_descending.get(self.screen, False)
        sorted_rows = sorted(
            rows,
            key=lambda row: self._sort_value(row, key),
            reverse=descending,
        )
        if self.screen == "blobs":
            sorted_rows.sort(
                key=lambda row: (
                    0
                    if isinstance(row, BlobItem) and row.is_prefix
                    else 1
                )
            )
        return sorted_rows

    def _sort_value(self, row: object, key: str) -> object:
        if isinstance(row, Subscription):
            return {
                "name": row.name.casefold(),
                "id": row.id,
                "tenant": row.tenant_id,
                "default": row.is_default,
            }.get(key, row.name.casefold())
        if isinstance(row, StorageAccount):
            return {
                "name": row.name.casefold(),
                "group": row.resource_group.casefold(),
                "location": row.location.casefold(),
                "hns": row.hns_enabled,
            }.get(key, row.name.casefold())
        if isinstance(row, ContainerItem):
            state = (
                None
                if row.is_deleted
                else self.container_usage.get(row.name)
            )
            return {
                "name": row.name.casefold(),
                "type": self._container_type(row),
                "status": (
                    "deleted"
                    if row.is_deleted
                    else state.status if state else ""
                ),
                "size": state.size_bytes if state else -1,
                "blobs": state.blob_count if state else -1,
                "retention": (
                    row.remaining_retention_days
                    if row.remaining_retention_days is not None
                    else -1
                ),
            }.get(key, row.name.casefold())
        if isinstance(row, BlobItem):
            folder_state = self._folder_usage_for(row)
            return {
                "kind": 0 if row.is_prefix else 1,
                "name": row.name.casefold(),
                "size": (
                    folder_state.size_bytes
                    if folder_state
                    else row.size_bytes
                ),
                "blobs": (
                    folder_state.blob_count
                    if folder_state
                    else 1 if not row.is_prefix else -1
                ),
                "tier": row.access_tier.casefold(),
                "modified": row.last_modified,
                "retention": (
                    row.remaining_retention_days
                    if row.remaining_retention_days is not None
                    else -1
                ),
            }.get(key, row.name.casefold())
        if isinstance(row, AccountQueueStats):
            return {
                "account": row.account.casefold(),
                "done": row.completed,
                "partial": row.partial,
                "pending": row.pending_or_failed,
                "blobs": row.blob_count,
                "size": row.size_bytes,
                "pages": row.page_count,
            }.get(key, row.account.casefold())
        return str(row).casefold()

    def _cycle_sort(self) -> None:
        keys = [column.key for column in self._columns()]
        current = self.sort_keys.get(self.screen, keys[0])
        index = keys.index(current) if current in keys else -1
        self.sort_keys[self.screen] = keys[(index + 1) % len(keys)]
        self.selected = 0
        self.offset = 0
        self.status = self._sort_status()

    def _toggle_sort_direction(self) -> None:
        self.sort_descending[self.screen] = not self.sort_descending.get(
            self.screen,
            False,
        )
        self.selected = 0
        self.offset = 0
        self.status = self._sort_status()

    def _sort_status(self) -> str:
        direction = (
            "descending"
            if self.sort_descending.get(self.screen, False)
            else "ascending"
        )
        return (
            f"Sort: {self.sort_keys.get(self.screen, 'name')} "
            f"{direction}"
        )

    def _normalize_selection(self, row_count: int, list_height: int) -> None:
        if row_count == 0:
            self.selected = 0
            self.offset = 0
            return
        self.selected = max(0, min(self.selected, row_count - 1))
        if self.selected < self.offset:
            self.offset = self.selected
        elif self.selected >= self.offset + list_height:
            self.offset = self.selected - list_height + 1
        self.offset = max(0, min(self.offset, max(row_count - list_height, 0)))

    def _columns(self) -> list[Column]:
        if self.screen == "subscriptions":
            columns = [
                Column(
                    "name",
                    "Subscription",
                    18,
                    30,
                    flex=1,
                    max_width=38,
                ),
                Column(
                    "id",
                    "Subscription ID",
                    12,
                    36,
                    flex=1,
                    max_width=36,
                ),
                Column(
                    "tenant",
                    "Tenant",
                    12,
                    36,
                    hide_priority=20,
                    max_width=36,
                ),
                Column("default", "Default", 7, 7, hide_priority=30),
            ]
        elif self.screen == "accounts":
            columns = [
                Column(
                    "name",
                    "Storage Account",
                    18,
                    26,
                    flex=1,
                    max_width=30,
                ),
                Column(
                    "group",
                    "Resource Group",
                    12,
                    22,
                    flex=1,
                    max_width=28,
                ),
                Column("location", "Location", 10, 18, hide_priority=10),
                Column("hns", "HNS", 5, 5, hide_priority=20),
            ]
        elif self.screen == "containers":
            columns = [
                Column(
                    "name",
                    "Container",
                    24,
                    40,
                    flex=1,
                    max_width=48,
                ),
                Column("type", "Type", 8, 12, hide_priority=20),
                Column("status", "Scan", 8, 9, hide_priority=5),
                Column(
                    "size",
                    "Size",
                    10,
                    12,
                    align="right",
                    hide_priority=10,
                ),
                Column(
                    "blobs",
                    "Blobs",
                    8,
                    14,
                    align="right",
                    hide_priority=15,
                ),
                Column(
                    "retention",
                    "Retain",
                    7,
                    9,
                    align="right",
                    hide_priority=25,
                ),
            ]
        elif self.screen == "stats":
            columns = [
                Column(
                    "account",
                    "Storage Account",
                    18,
                    26,
                    flex=1,
                    max_width=30,
                ),
                Column("done", "Done", 6, 7, align="right"),
                Column("partial", "Partial", 7, 8, align="right"),
                Column(
                    "pending",
                    "Pending/Failed",
                    10,
                    14,
                    align="right",
                ),
                Column(
                    "blobs",
                    "Blobs",
                    9,
                    14,
                    align="right",
                    hide_priority=10,
                ),
                Column(
                    "size",
                    "Size",
                    10,
                    12,
                    align="right",
                    hide_priority=5,
                ),
                Column(
                    "pages",
                    "Pages",
                    7,
                    10,
                    align="right",
                    hide_priority=20,
                ),
            ]
        else:
            columns = [
                Column("kind", "Kind", 6, 7),
                Column(
                    "name",
                    "Name",
                    20,
                    32,
                    flex=1,
                    max_width=40,
                ),
                Column("size", "Size", 10, 12, align="right"),
                Column(
                    "blobs",
                    "Blobs",
                    7,
                    9,
                    align="right",
                    hide_priority=15,
                ),
                Column("tier", "Tier", 7, 9, hide_priority=10),
                Column(
                    "modified",
                    "Last Modified",
                    16,
                    29,
                    hide_priority=20,
                ),
                Column(
                    "retention",
                    "Retain",
                    7,
                    9,
                    align="right",
                    hide_priority=25,
                ),
            ]

        sort_key = self.sort_keys.get(self.screen)
        descending = self.sort_descending.get(self.screen, False)
        marker = "[D]" if descending else "[A]"
        return [
            replace(
                column,
                label=(
                    f"{column.label} {marker}"
                    if column.key == sort_key
                    else column.label
                ),
            )
            for column in columns
        ]

    def _row_values(self, row: object) -> dict[str, object]:
        if isinstance(row, Subscription):
            return {
                "name": row.name,
                "id": row.id,
                "tenant": row.tenant_id,
                "default": "yes" if row.is_default else "",
            }
        if isinstance(row, StorageAccount):
            return {
                "name": row.name,
                "group": row.resource_group,
                "location": row.location,
                "hns": "yes" if row.hns_enabled else "no",
            }
        if isinstance(row, ContainerItem):
            state = (
                None
                if row.is_deleted
                else self.container_usage.get(row.name)
            )
            return {
                "name": row.name,
                "type": self._container_type(row),
                "status": (
                    "deleted"
                    if row.is_deleted
                    else state.status if state else "-"
                ),
                "size": human_size(state.size_bytes) if state else "-",
                "blobs": human_count(state.blob_count) if state else "-",
                "retention": (
                    f"{row.remaining_retention_days}d"
                    if row.remaining_retention_days is not None
                    else "-"
                ),
            }
        if isinstance(row, BlobItem):
            relative = (
                row.name[len(self.prefix) :]
                if row.name.startswith(self.prefix)
                else row.name
            )
            folder_state = self._folder_usage_for(row)
            return {
                "kind": (
                    "DIR"
                    if row.is_prefix
                    else "DELETED"
                    if row.is_deleted
                    else "BLOB"
                ),
                "name": relative,
                "size": (
                    human_size(folder_state.size_bytes)
                    if folder_state
                    else "-" if row.is_prefix else human_size(row.size_bytes)
                ),
                "blobs": (
                    human_count(folder_state.blob_count)
                    if folder_state
                    else "-"
                ),
                "tier": "" if row.is_prefix else row.access_tier,
                "modified": "" if row.is_prefix else row.last_modified,
                "retention": (
                    f"{row.remaining_retention_days}d"
                    if row.remaining_retention_days is not None
                    else "-"
                ),
            }
        if isinstance(row, AccountQueueStats):
            return {
                "account": row.account,
                "done": f"{row.completed:,}",
                "partial": f"{row.partial:,}",
                "pending": f"{row.pending_or_failed:,}",
                "blobs": human_count(row.blob_count),
                "size": human_size(row.size_bytes),
                "pages": f"{row.page_count:,}",
            }
        return {"name": str(row)}

    def _folder_usage_for(self, item: BlobItem) -> FolderUsage | None:
        if not item.is_prefix:
            return None
        folder = item.name.rstrip("/")
        if not folder:
            return None
        depth = folder.count("/") + 1
        return self.folder_usage.get((depth, folder))

    def _scope_usage(self) -> ContainerUsage | FolderUsage | None:
        if not self.prefix:
            return self.container_usage.get(self.container)
        folder = self.prefix.rstrip("/")
        depth = folder.count("/") + 1
        return self.folder_usage.get((depth, folder))

    def _direct_file_usage(self) -> tuple[FolderUsage | None, bool]:
        max_depth = max(
            (depth for depth, _ in self.folder_usage),
            default=0,
        )
        if not self.prefix:
            return self.folder_usage.get((1, "_root")), max_depth >= 1
        folder = self.prefix.rstrip("/")
        depth = folder.count("/") + 1
        return (
            self.folder_usage.get((depth + 1, folder)),
            max_depth >= depth + 1,
        )

    def _scope_status(
        self,
        loaded_items: int,
        service_pages: int,
    ) -> str:
        parts = [
            f"Loaded {loaded_items:,} items via "
            f"{service_pages:,} service page"
            f"{'s' if service_pages != 1 else ''}"
        ]
        scope = self._scope_usage()
        if scope is not None:
            state = self.container_usage.get(self.container)
            aggregate_status = (
                "complete" if state and state.completed else "partial"
            )
            parts.append(
                f"Total {human_size(scope.size_bytes)} / "
                f"{human_count(scope.blob_count)} Blobs "
                f"({aggregate_status})"
            )
            direct, direct_available = self._direct_file_usage()
            if direct is not None:
                parts.append(
                    f"Direct files {human_size(direct.size_bytes)} / "
                    f"{human_count(direct.blob_count)}"
                )
            elif direct_available:
                parts.append("Direct files 0")
            else:
                parts.append("Direct files unavailable at this depth")
        if self.next_marker:
            parts.append("current page only; n for next")
        return " | ".join(parts)

    def _draw_details(
        self,
        row: object,
        x: int,
        width: int,
        height: int,
    ) -> None:
        assert self.window is not None
        lines = self._detail_lines(row)
        max_offset = max(len(lines) - height, 0)
        self.detail_offset = min(self.detail_offset, max_offset)
        visible_lines = lines[
            self.detail_offset : self.detail_offset + height
        ]
        if lines:
            start = self.detail_offset + 1
            end = self.detail_offset + len(visible_lines)
            safe_addstr(
                self.window,
                1,
                x,
                shorten(f"Details {start}-{end}/{len(lines)}", width),
                curses.A_BOLD | self._color(1),
            )
        for index, line in enumerate(visible_lines):
            safe_addstr(
                self.window,
                index + 2,
                x,
                shorten(line, width),
            )

    def _draw_queue(
        self,
        x: int,
        width: int,
        top: int,
        lines: list[str],
    ) -> None:
        assert self.window is not None
        safe_addstr(
            self.window,
            top,
            x,
            "-" * max(width, 0),
            self._color(1),
        )
        safe_addstr(
            self.window,
            top,
            x + 2,
            " Queue ",
            curses.A_BOLD | self._color(1),
        )
        max_y = self.window.getmaxyx()[0] - 2
        for index, line in enumerate(lines):
            y = top + index + 1
            if y >= max_y:
                break
            safe_addstr(self.window, y, x, shorten(line, width))

    def _queue_lines(self) -> list[str]:
        if self.stats is None:
            return ["State: unavailable"]
        if self.stats.reported_total is not None:
            processed = self.stats.reported_processed or 0
            active = self.stats.reported_active or 0
            failed = self.stats.reported_failed or 0
            total = self.stats.reported_total
            return [
                "Source: scanner log",
                f"Processed: {processed}/{total}",
                f"Completed: {max(processed - failed, 0)}",
                f"Active: {active}",
                f"Failed: {failed}",
                f"Queued: {max(total - processed - active, 0)}",
                f"Blobs: {human_count(self.stats.reported_blob_count or 0)}",
                f"Pages: {(self.stats.reported_page_count or 0):,}",
                f"Size: {self.stats.reported_size or '-'}",
                f"Rate: {self.stats.reported_rate}",
                f"Elapsed: {self.stats.reported_elapsed}",
            ]
        return [
            "Source: SQLite checkpoint",
            f"Completed: {self.stats.completed}/{self.stats.total}",
            f"Partial: {self.stats.partial}",
            f"Pending/failed: {self.stats.pending_or_failed}",
            f"Blobs: {human_count(self.stats.blob_count)}",
            f"Pages: {self.stats.page_count:,}",
            f"Size: {human_size(self.stats.size_bytes)}",
        ]

    def _detail_lines(self, row: object) -> list[str]:
        if isinstance(row, AccountQueueStats):
            return [
                f"Account: {row.account}",
                f"Containers: {row.total:,}",
                f"Completed: {row.completed:,}",
                f"Partial: {row.partial:,}",
                f"Pending/failed: {row.pending_or_failed:,}",
                f"Blobs checkpointed: {human_count(row.blob_count)}",
                f"Size checkpointed: {human_size(row.size_bytes)}",
                f"Pages checkpointed: {row.page_count:,}",
            ]
        if isinstance(row, Subscription):
            return [
                f"Subscription: {row.name}",
                f"ID: {row.id}",
                f"Tenant: {row.tenant_id}",
                f"Default: {'yes' if row.is_default else 'no'}",
            ]
        if isinstance(row, StorageAccount):
            return [
                f"Account: {row.name}",
                f"Resource group: {row.resource_group}",
                f"Location: {row.location}",
                f"HNS: {'enabled' if row.hns_enabled else 'disabled'}",
                "",
                row.resource_id,
            ]
        if isinstance(row, ContainerItem):
            state = (
                None
                if row.is_deleted
                else self.container_usage.get(row.name)
            )
            lines = [
                f"Container: {row.name}",
                f"Soft deleted: {'yes' if row.is_deleted else 'no'}",
                f"AML snapshot: "
                f"{'yes' if self._is_aml_snapshot(row.name) else 'no'}",
            ]
            if row.is_deleted:
                lines.extend(
                    [
                        f"Version: {row.version or '-'}",
                        f"Deleted time: {row.deleted_time or '-'}",
                        "Remaining retention: "
                        f"{row.remaining_retention_days} days"
                        if row.remaining_retention_days is not None
                        else "Remaining retention: -",
                    ]
                )
            if state:
                lines.extend(
                    [
                        f"Scan status: {state.status}",
                        f"Scanned size: {human_size(state.size_bytes)}",
                        f"Scanned blobs: {human_count(state.blob_count)}",
                        f"Scanned pages: {state.page_count:,}",
                    ]
                )
            return lines
        if isinstance(row, BlobItem):
            relative = (
                row.name[len(self.prefix) :]
                if row.name.startswith(self.prefix)
                else row.name
            )
            if row.is_prefix:
                folder_state = self._folder_usage_for(row)
                lines = [
                    "Virtual folder",
                    f"Name: {relative}",
                    f"Path: {row.name}",
                ]
                if folder_state:
                    container_state = self.container_usage.get(self.container)
                    aggregate_status = (
                        "complete"
                        if container_state and container_state.completed
                        else "partial"
                    )
                    lines.extend(
                        [
                            f"Depth: {folder_state.depth}",
                            f"Aggregate: {aggregate_status}",
                            f"Size: {human_size(folder_state.size_bytes)}",
                            f"Blobs: {human_count(folder_state.blob_count)}",
                        ]
                    )
                else:
                    lines.append("Aggregate: unavailable for this depth")
                return lines
            assert self.account is not None
            url = (
                f"{self.account.endpoint.rstrip('/')}/"
                f"{quote(self.container, safe='')}/"
                f"{quote(row.name, safe='/')}"
            )
            lines = [
                f"Name: {relative}",
                f"Path: {row.name}",
                f"URL: {url}",
                f"Soft deleted: {'yes' if row.is_deleted else 'no'}",
                f"Size: {human_size(row.size_bytes)} "
                f"({row.size_bytes:,} bytes)",
            ]
            if row.is_deleted:
                lines.extend(
                    [
                        f"Version ID: {row.version_id or '-'}",
                        f"Deleted time: {row.deleted_time or '-'}",
                        "Remaining retention: "
                        f"{row.remaining_retention_days} days"
                        if row.remaining_retention_days is not None
                        else "Remaining retention: -",
                    ]
                )
            lines.append("Properties:")
            preferred_properties = [
                "BlobType",
                "AccessTier",
                "AccessTierChangeTime",
                "ArchiveStatus",
                "Last-Modified",
                "Creation-Time",
                "Etag",
                "Content-Length",
                "Content-Type",
                "Content-Encoding",
                "Content-Language",
                "Content-Disposition",
                "Cache-Control",
                "LeaseStatus",
                "LeaseState",
                "LeaseDuration",
                "ServerEncrypted",
                "EncryptionScope",
                "CopyStatus",
                "CopySource",
                "CopyCompletionTime",
            ]
            rendered_properties: set[str] = set()
            for key in preferred_properties:
                if key in row.properties:
                    lines.append(f"  {key}: {row.properties[key]}")
                    rendered_properties.add(key)
            lines.extend(
                f"  {key}: {value}"
                for key, value in sorted(row.properties.items())
                if key not in rendered_properties
            )
            lines.append("Metadata:")
            if row.metadata:
                lines.extend(
                    f"  {key}={value}"
                    for key, value in sorted(row.metadata.items())
                )
            else:
                lines.append("  (none)")
            return lines
        return []

    def _handle_key(self, key: int) -> None:
        rows = self._visible_rows()
        original_selected = self.selected
        if key in (curses.KEY_UP, ord("k")):
            self.selected -= 1
        elif key in (curses.KEY_DOWN, ord("j")):
            self.selected += 1
        elif key == curses.KEY_PPAGE:
            self.selected -= 10
        elif key == curses.KEY_NPAGE:
            self.selected += 10
        elif key == ord("g"):
            self.selected = 0
        elif key == ord("G"):
            self.selected = max(len(rows) - 1, 0)
        elif key == ord("/"):
            self.query = self._prompt("Filter")
            self.selected = 0
            self.offset = 0
        elif key == ord("c"):
            self.query = ""
            self.selected = 0
            self.offset = 0
        elif key in (ord("h"), ord("H"), ord("?")):
            self._show_help()
        elif key == ord("s"):
            if self.screen != "stats":
                self.previous_screen = self.screen
                self.screen = "stats"
                self.query = ""
                self.selected = 0
                self.offset = 0
            self._load_stats()
        elif key == ord("S"):
            if self.screen != "subscriptions":
                self.subscription_picker_return_screen = self.screen
                self.screen = "subscriptions"
                self.query = ""
                self.selected = 0
                self.offset = 0
                self.status = "Select a subscription"
        elif key == ord("o"):
            self._cycle_sort()
        elif key == ord("O"):
            self._toggle_sort_direction()
        elif key == ord("D"):
            if rows:
                self._delete_selected(rows[self.selected])
        elif key == ord("["):
            self.detail_offset = max(self.detail_offset - 1, 0)
        elif key == ord("]"):
            self.detail_offset += 1
        elif key in (
            curses.KEY_BACKSPACE,
            127,
            8,
            ord("b"),
            ord("q"),
            ord("Q"),
        ):
            self._go_back()
        elif key in (10, 13, curses.KEY_ENTER):
            if rows:
                self._open(rows[self.selected])
        elif key == ord("r"):
            self._refresh()
        elif self.screen == "containers" and key == ord("u"):
            self.show_snapshots = not self.show_snapshots
            self.selected = 0
            self.offset = 0
            self.status = (
                "AML snapshot containers are visible"
                if self.show_snapshots
                else "AML snapshot containers are hidden"
            )
        elif self.screen in ("containers", "blobs") and key == ord("x"):
            self.show_deleted = not self.show_deleted
            self.selected = 0
            self.offset = 0
            self.current_marker = ""
            self.marker_history = []
            if self.screen == "containers":
                self._load_containers()
            else:
                self._load_blob_page()
        elif self.screen == "blobs" and key == ord("n"):
            self._next_page()
        elif self.screen == "blobs" and key == ord("p"):
            self._previous_page()
        elif self.screen == "blobs" and key == ord("i"):
            if rows:
                self._show_item_details(rows[self.selected])
        elif self.screen == "blobs" and key == ord("J"):
            self._jump_prefix()
        if self.selected != original_selected:
            self.detail_offset = 0

    def _open(self, row: object) -> None:
        self.detail_offset = 0
        if isinstance(row, Subscription):
            self.subscription = row
            self.catalog.remember_subscription(row.id)
            self.screen = "accounts"
            self.subscription_picker_return_screen = None
            self.query = ""
            self.selected = 0
            self.offset = 0
            self._load_subscription()
            return
        if isinstance(row, StorageAccount):
            self._open_account(row)
            return
        if isinstance(row, AccountQueueStats):
            account = next(
                (
                    item
                    for item in self.accounts
                    if item.name.casefold() == row.account.casefold()
                ),
                None,
            )
            if account is None:
                raise AppError(f"Storage account not found: {row.account}")
            if self.sort_keys.get("stats") == "size":
                self.sort_keys["containers"] = "size"
                self.sort_descending["containers"] = (
                    self.sort_descending.get("stats", True)
                )
            self._open_account(account, return_screen="stats")
            return
        if isinstance(row, ContainerItem):
            if row.is_deleted:
                self._show_item_details(row)
                return
            if self.sort_keys.get("containers") == "size":
                self.sort_keys["blobs"] = "size"
                self.sort_descending["blobs"] = (
                    self.sort_descending.get("containers", False)
                )
            self.container = row.name
            self.screen = "blobs"
            self.prefix = ""
            self.query = ""
            self.selected = 0
            self.offset = 0
            self.current_marker = ""
            self.marker_history = []
            self._load_blob_page()
            return
        if isinstance(row, BlobItem):
            if row.is_prefix:
                self.prefix = row.name
                self.query = ""
                self.selected = 0
                self.offset = 0
                self.current_marker = ""
                self.marker_history = []
                self._load_blob_page()
            else:
                self._show_item_details(row)

    def _open_account(
        self,
        account: StorageAccount,
        return_screen: str | None = None,
    ) -> None:
        self.account = account
        self.containers_return_screen = return_screen
        self.screen = "containers"
        self.query = ""
        self.selected = 0
        self.offset = 0
        self._load_containers()

    def _go_back(self) -> None:
        self.detail_offset = 0
        if self.screen == "stats":
            self.screen = self.previous_screen
            self.query = ""
            self.selected = 0
            self.offset = 0
            return
        if self.screen == "subscriptions":
            if (
                self.subscription is not None
                and self.subscription_picker_return_screen is not None
            ):
                self.screen = self.subscription_picker_return_screen
                self.subscription_picker_return_screen = None
                self.query = ""
                self.selected = 0
                self.offset = 0
                self.status = "Subscription unchanged"
                return
            self.status = "Already at top level; press Ctrl+C to exit"
            return
        if self.screen == "accounts":
            self.status = (
                "Top level for selected subscription; "
                "press S to change subscription"
            )
            return
        elif self.screen == "containers":
            self.screen = self.containers_return_screen or "accounts"
            self.containers_return_screen = None
            self.account = None
        elif self.prefix:
            trimmed = self.prefix.rstrip("/")
            self.prefix = (
                f"{trimmed.rsplit('/', 1)[0]}/"
                if "/" in trimmed
                else ""
            )
            self.current_marker = ""
            self.marker_history = []
            self._load_blob_page()
        else:
            self.screen = "containers"
            self.container = ""
            self.blob_items = []
            self.folder_usage = {}
        self.query = ""
        self.selected = 0
        self.offset = 0

    def _refresh(self) -> None:
        if self.screen == "stats":
            self._load_stats()
        elif self.screen == "subscriptions":
            self.catalog.refresh_subscriptions()
            self.subscriptions = self.catalog.subscriptions
            self.status = f"Loaded {len(self.subscriptions)} subscriptions"
        elif self.screen == "accounts":
            self._load_subscription(force_refresh=True)
        elif self.screen == "containers":
            self._load_containers()
        else:
            self._load_blob_page()

    def _refresh_stats_if_due(self) -> None:
        if self.stats_provider is None:
            return
        now = time.monotonic()
        if now - self.last_stats_refresh < 1:
            return
        try:
            self.stats = self.stats_provider.load()
        except sqlite3.Error:
            return
        self.last_stats_refresh = now

    def _load_stats(self) -> None:
        if self.stats_provider is None:
            self.status = "No SQLite state was configured"
            self.stats = None
            return
        try:
            self.stats = self.stats_provider.load()
        except sqlite3.Error as error:
            self.status = f"Unable to read statistics: {error}"
            return
        self.last_stats_refresh = time.monotonic()
        self.status = (
            f"Statistics refreshed | DB "
            f"{human_size(self.stats.database_size)}"
        )

    def _load_subscription(self, force_refresh: bool = False) -> None:
        assert self.subscription is not None
        self._configure_scan_paths()
        self._busy(
            f"Loading Storage Accounts from {self.subscription.name}..."
        )
        (
            self.accounts,
            self.snapshot_prefixes,
            token_provider,
        ) = self.catalog.load_subscription(
            self.subscription,
            force_refresh=force_refresh,
        )
        self.client = BlobRestClient(
            token_provider,
            timeout_seconds=60,
        )
        self.account = None
        self.container_items = []
        if self.catalog.last_load_from_cache:
            age_hours = self.catalog.last_cache_age_seconds / 3600
            source = (
                f"cache ({age_hours / 24:.1f}d old)"
                if age_hours >= 24
                else f"cache ({age_hours:.1f}h old)"
            )
        else:
            source = "Azure"
        self.status = (
            f"Loaded {len(self.accounts):,} Blob-capable Storage Accounts "
            f"from {source}"
        )

    def _configure_scan_paths(self) -> None:
        assert self.subscription is not None
        paths = resolve_scan_paths(
            self.catalog.settings,
            self.subscription.id,
            state=self.state_path_override,
            log=self.scan_log_path_override,
            working_directory=self.scan_path_working_directory,
        )
        if self.state_path_override is not None and not paths.state.is_file():
            raise AppError(f"State database not found: {paths.state}")
        if (
            self.scan_log_path_override is not None
            and not paths.log.is_file()
        ):
            raise AppError(f"Scan log not found: {paths.log}")
        self.state_path = paths.state if paths.state.is_file() else None
        scan_log = paths.log if paths.log.is_file() else None
        self.stats_provider = (
            StatsProvider(self.state_path, scan_log)
            if self.state_path is not None
            else None
        )
        self.stats = None

    def _load_containers(self) -> None:
        assert self.account is not None
        assert self.client is not None
        self._busy(f"Loading containers from {self.account.name}...")
        try:
            self.container_items = sorted(
                list_containers(
                    self.client,
                    self.account,
                    include_deleted=self.show_deleted,
                ),
                key=lambda item: (
                    item.name.casefold(),
                    item.is_deleted,
                    item.version,
                ),
            )
        except StorageError:
            self.container_items = []
            raise
        self.container_usage = load_usage_state(
            self.state_path,
            self.account.name,
        )
        self.hidden_snapshot_count = sum(
            self._is_aml_snapshot(item.name)
            for item in self.container_items
            if not item.is_deleted
        )
        deleted_count = sum(item.is_deleted for item in self.container_items)
        visible_count = len(self._visible_rows())
        self.status = (
            f"Loaded {len(self.container_items):,} containers; "
            f"{visible_count:,} visible; {deleted_count:,} soft deleted"
        )

    def _load_blob_page(self) -> None:
        assert self.account is not None
        assert self.client is not None
        container_state, self.folder_usage = load_container_folder_usage(
            self.state_path,
            self.account.name,
            self.container,
        )
        if container_state is not None:
            self.container_usage[self.container] = container_state
        self._busy(
            f"Loading {self.account.name}/{self.container}/{self.prefix}..."
        )
        (
            self.blob_items,
            self.next_marker,
            self.service_pages_loaded,
        ) = list_hierarchy_page(
            self.client,
            self.account,
            self.container,
            self.prefix,
            self.current_marker,
            self.page_size,
            include_deleted=self.show_deleted,
        )
        self.selected = 0
        self.offset = 0
        self.status = self._scope_status(
            len(self.blob_items),
            self.service_pages_loaded,
        )

    def _next_page(self) -> None:
        if not self.next_marker:
            self.status = "No next page"
            return
        self.marker_history.append(self.current_marker)
        self.current_marker = self.next_marker
        self._load_blob_page()

    def _previous_page(self) -> None:
        if not self.marker_history:
            self.status = "No previous page"
            return
        self.current_marker = self.marker_history.pop()
        self._load_blob_page()

    def _jump_prefix(self) -> None:
        relative = self._prompt("Prefix relative to container")
        if not relative:
            return
        self.prefix = relative.lstrip("/")
        self.current_marker = ""
        self.marker_history = []
        self.query = ""
        self._load_blob_page()

    def _is_aml_snapshot(self, container: str) -> bool:
        workspace_ids = (
            self.snapshot_prefixes.get(self.account.name.lower(), set())
            if self.account is not None
            else set()
        )
        return is_aml_snapshot(container, workspace_ids)

    def _container_type(self, item: ContainerItem) -> str:
        if item.is_deleted:
            return "deleted"
        return "snapshot" if self._is_aml_snapshot(item.name) else "data"

    def _delete_selected(self, row: object) -> None:
        if self.screen == "containers" and isinstance(row, ContainerItem):
            if row.is_deleted:
                self.status = "Soft-deleted Containers cannot be deleted again"
                return
            self._delete_container(row)
            return
        if self.screen == "blobs" and isinstance(row, BlobItem):
            if row.is_prefix:
                self.status = (
                    "Virtual folders cannot be deleted; select an individual Blob"
                )
                return
            if row.is_deleted:
                self.status = "Soft-deleted Blobs cannot be deleted again"
                return
            self._delete_blob(row)
            return
        self.status = "Delete is available only for Containers and Blobs"

    def _delete_container(self, item: ContainerItem) -> None:
        assert self.account is not None
        assert self.client is not None
        container = item.name
        state = self.container_usage.get(container)
        if state is not None and state.status == "partial":
            self._modal(
                "Delete blocked",
                [
                    f"Container: {container}",
                    "This Container is currently being scanned.",
                    "Wait for or stop the scanner before deleting it.",
                ],
            )
            return
        warning = [
            f"Storage Account: {self.account.name}",
            f"Container: {container}",
            "",
            "Deleting a Container removes every Blob it contains.",
            "Recovery depends on the account's Container soft-delete policy.",
        ]
        if self._is_aml_snapshot(container):
            warning.append(
                "This AML snapshot may be referenced by an existing job."
            )
        warning.extend(
            [
                "",
                "Press any key, then type the full Container name.",
            ]
        )
        self._modal("Delete Container", warning)
        confirmation = self._prompt("Confirm")
        if confirmation != container:
            self.status = "Container deletion cancelled"
            return
        self.client.delete_container(self.account, container)
        self.container_items = [
            candidate
            for candidate in self.container_items
            if candidate != item
        ]
        self.container_usage.pop(container, None)
        self.selected = max(self.selected - 1, 0)
        self.detail_offset = 0
        self.status = f"Deleted Container: {container}"

    def _delete_blob(self, blob: BlobItem) -> None:
        assert self.account is not None
        assert self.client is not None
        container_state = self.container_usage.get(self.container)
        if (
            container_state is not None
            and container_state.status == "partial"
        ):
            self._modal(
                "Delete blocked",
                [
                    f"Container: {self.container}",
                    "This Container is currently being scanned.",
                    "Wait for or stop the scanner before deleting a Blob.",
                ],
            )
            return
        self._modal(
            "Delete Blob",
            [
                f"Storage Account: {self.account.name}",
                f"Container: {self.container}",
                f"Blob: {blob.name}",
                f"Size: {human_size(blob.size_bytes)}",
                f"ETag: {blob.etag or '-'}",
                "",
                "If the Blob changed since this page loaded, deletion fails.",
                "Snapshots are not deleted automatically.",
                "",
                "Press any key, then type DELETE.",
            ],
        )
        if self._prompt("Type DELETE to confirm") != "DELETE":
            self.status = "Blob deletion cancelled"
            return
        self.client.delete_blob(
            self.account,
            self.container,
            blob.name,
            etag=blob.etag,
        )
        self.blob_items = [
            item
            for item in self.blob_items
            if item.is_prefix or item.name != blob.name
        ]
        self.selected = max(self.selected - 1, 0)
        self.detail_offset = 0
        self.status = (
            f"Deleted Blob: {blob.name} | scanner aggregates may be stale"
        )

    def _prompt(self, label: str) -> str:
        assert self.window is not None
        height, width = self.window.getmaxyx()
        prompt = f"{label}: "
        safe_addstr(
            self.window,
            height - 1,
            0,
            " " * max(width - 1, 0),
        )
        safe_addstr(self.window, height - 1, 0, prompt)
        self.window.refresh()
        self.window.timeout(-1)
        curses.echo()
        curses.curs_set(1)
        try:
            value = self.window.getstr(
                height - 1,
                len(prompt),
                max(width - len(prompt) - 2, 1),
            )
        finally:
            curses.noecho()
            curses.curs_set(0)
            self.window.timeout(REFRESH_TIMEOUT_MS)
        return value.decode("utf-8", errors="replace").strip()

    def _busy(self, message: str) -> None:
        self.status = message
        self._draw()

    def _show_help(self) -> None:
        self._modal(
            "Help",
            [
                "Up/Down or k/j  Move selection",
                "Enter           Open account/container/folder or blob details",
                "q / Backspace   Go to parent",
                "/               Filter current list",
                "c               Clear filter",
                "r               Refresh (accounts bypass 10-day cache)",
                "S               Change the selected subscription",
                "o               Cycle the sort column",
                "O               Toggle ascending/descending sort",
                "s               Open live scanner statistics",
                "u               Toggle AML UUID containers (container screen)",
                "x               Toggle soft-deleted Containers/Blobs",
                "n / p           Next / previous page (blob screen)",
                "J               Jump to an exact blob prefix",
                "i               Show full selected item details",
                "[ / ]           Scroll the right-side details",
                "D               Delete selected Blob/Container with confirmation",
                "g / G           First / last item",
                "h / ?           Show this help",
                "Ctrl+C          Exit",
            ],
        )

    def _show_item_details(self, row: object) -> None:
        self._modal("Details", self._detail_lines(row))

    def _modal(self, title: str, lines: Iterable[str]) -> None:
        assert self.window is not None
        all_lines: list[str] = []
        for line in lines:
            all_lines.extend(str(line).splitlines() or [""])
        height, width = self.window.getmaxyx()
        box_width = min(max([len(title) + 4, 60, *(len(x) + 4 for x in all_lines)]), width - 4)
        box_height = min(max(len(all_lines) + 4, 6), height - 2)
        y = max((height - box_height) // 2, 0)
        x = max((width - box_width) // 2, 0)
        modal = curses.newwin(box_height, box_width, y, x)
        modal.keypad(True)
        modal.box()
        safe_addstr(
            modal,
            0,
            2,
            f" {shorten(title, box_width - 6)} ",
            curses.A_BOLD,
        )
        for index, line in enumerate(all_lines[: box_height - 3]):
            safe_addstr(modal, index + 1, 2, shorten(line, box_width - 4))
        safe_addstr(
            modal,
            box_height - 2,
            2,
            "Press any key",
            self._color(1),
        )
        modal.refresh()
        modal.getch()
