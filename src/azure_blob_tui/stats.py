from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class AccountQueueStats:
    account: str
    total: int
    completed: int
    partial: int
    pending_or_failed: int
    blob_count: int
    size_bytes: int
    page_count: int


@dataclass(frozen=True)
class QueueStats:
    total: int
    completed: int
    partial: int
    pending_or_failed: int
    blob_count: int
    size_bytes: int
    page_count: int
    accounts: tuple[AccountQueueStats, ...]
    database_size: int
    scan_status: str
    checkpoint_updated_at: str
    reported_processed: int | None = None
    reported_total: int | None = None
    reported_active: int | None = None
    reported_failed: int | None = None
    reported_blob_count: int | None = None
    reported_page_count: int | None = None
    reported_size: str = ""
    reported_rate: str = ""
    reported_elapsed: str = ""

    def short_status(self) -> str:
        if self.reported_total is not None:
            return (
                f"queue {self.reported_processed}/{self.reported_total} "
                f"active={self.reported_active} failed={self.reported_failed}"
            )
        return (
            f"queue done={self.completed}/{self.total} "
            f"partial={self.partial} pending={self.pending_or_failed}"
        )


PROGRESS_PATTERN = re.compile(
    r"^Progress: containers (?P<processed>[\d,]+)/(?P<total>[\d,]+) "
    r"\([^)]*active (?P<active>[\d,]+), failed (?P<failed>[\d,]+)\) "
    r"\| blobs (?P<blobs>[\d,]+) "
    r"\| pages (?P<pages>[\d,]+) "
    r"\| size (?P<size>[^|]+) "
    r"\| rate (?P<rate>[^|]+) \| elapsed (?P<elapsed>\S+)$",
    re.MULTILINE,
)


class StatsProvider:
    def __init__(
        self,
        state_path: Path,
        scan_log_path: Path | None = None,
    ) -> None:
        self.state_path = state_path
        self.scan_log_path = scan_log_path

    def load(self) -> QueueStats:
        connection = sqlite3.connect(
            f"{self.state_path.resolve().as_uri()}?mode=ro",
            uri=True,
            timeout=5,
        )
        connection.row_factory = sqlite3.Row
        account_rows = connection.execute(
            """
            SELECT
                account,
                COUNT(*) AS total,
                SUM(CASE WHEN completed = 1 THEN 1 ELSE 0 END) AS completed,
                SUM(
                    CASE
                        WHEN completed = 0 AND page_count > 0 THEN 1
                        ELSE 0
                    END
                ) AS partial,
                SUM(
                    CASE
                        WHEN completed = 0 AND page_count = 0 THEN 1
                        ELSE 0
                    END
                ) AS pending_or_failed,
                COALESCE(SUM(blob_count), 0) AS blob_count,
                COALESCE(SUM(size_bytes), 0) AS size_bytes,
                COALESCE(SUM(page_count), 0) AS page_count
            FROM containers
            GROUP BY account
            ORDER BY account
            """
        ).fetchall()
        metadata = {
            str(row["key"]): str(row["value"])
            for row in connection.execute(
                "SELECT key, value FROM metadata"
            )
        }
        connection.close()

        accounts = tuple(
            AccountQueueStats(
                account=str(row["account"]),
                total=int(row["total"]),
                completed=int(row["completed"]),
                partial=int(row["partial"]),
                pending_or_failed=int(row["pending_or_failed"]),
                blob_count=int(row["blob_count"]),
                size_bytes=int(row["size_bytes"]),
                page_count=int(row["page_count"]),
            )
            for row in account_rows
        )
        progress = self._read_progress()
        return QueueStats(
            total=sum(item.total for item in accounts),
            completed=sum(item.completed for item in accounts),
            partial=sum(item.partial for item in accounts),
            pending_or_failed=sum(
                item.pending_or_failed for item in accounts
            ),
            blob_count=sum(item.blob_count for item in accounts),
            size_bytes=sum(item.size_bytes for item in accounts),
            page_count=sum(item.page_count for item in accounts),
            accounts=accounts,
            database_size=self._database_size(),
            scan_status=metadata.get("status", "unknown"),
            checkpoint_updated_at=metadata.get("updated_at", ""),
            **progress,
        )

    def _database_size(self) -> int:
        return sum(
            path.stat().st_size
            for path in (
                self.state_path,
                Path(f"{self.state_path}-wal"),
                Path(f"{self.state_path}-shm"),
            )
            if path.exists()
        )

    def _read_progress(self) -> dict[str, object]:
        if self.scan_log_path is None or not self.scan_log_path.is_file():
            return {}
        try:
            text = self.scan_log_path.read_text(
                encoding="utf-8",
                errors="replace",
            )
        except OSError:
            return {}
        matches = list(PROGRESS_PATTERN.finditer(text))
        if not matches:
            return {}
        match = matches[-1]
        return {
            "reported_processed": int(
                match.group("processed").replace(",", "")
            ),
            "reported_total": int(match.group("total").replace(",", "")),
            "reported_active": int(
                match.group("active").replace(",", "")
            ),
            "reported_failed": int(
                match.group("failed").replace(",", "")
            ),
            "reported_blob_count": int(
                match.group("blobs").replace(",", "")
            ),
            "reported_page_count": int(
                match.group("pages").replace(",", "")
            ),
            "reported_size": match.group("size").strip(),
            "reported_rate": match.group("rate").strip(),
            "reported_elapsed": match.group("elapsed"),
        }
