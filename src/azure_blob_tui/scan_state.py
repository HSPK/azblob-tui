from __future__ import annotations

import csv
import fcntl
import json
import os
import sqlite3
import tempfile
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .azure import AppError
from .models import StorageAccount


SCHEMA_VERSION = "1"


@dataclass
class FolderDelta:
    blob_count: int = 0
    size_bytes: int = 0


class StateLock:
    def __init__(self, state_path: Path) -> None:
        self.path = Path(f"{state_path}.lock")
        self.handle = None

    def __enter__(self) -> "StateLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("w", encoding="utf-8")
        try:
            fcntl.flock(
                self.handle.fileno(),
                fcntl.LOCK_EX | fcntl.LOCK_NB,
            )
        except BlockingIOError as error:
            self.handle.close()
            raise AppError(
                f"Another scan is already using state: {self.path}"
            ) from error
        self.handle.write(str(os.getpid()))
        self.handle.flush()
        return self

    def __exit__(self, *_: object) -> None:
        if self.handle is not None:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            self.handle.close()


class ScanStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(
            path,
            timeout=30,
            check_same_thread=False,
        )
        self.connection.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        with self.lock:
            self.connection.executescript(
                """
                PRAGMA journal_mode=WAL;
                PRAGMA synchronous=NORMAL;
                PRAGMA busy_timeout=30000;

                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS accounts (
                    name TEXT PRIMARY KEY,
                    resource_group TEXT NOT NULL,
                    resource_id TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS containers (
                    account TEXT NOT NULL,
                    container TEXT NOT NULL,
                    next_marker TEXT NOT NULL DEFAULT '',
                    completed INTEGER NOT NULL DEFAULT 0,
                    page_count INTEGER NOT NULL DEFAULT 0,
                    blob_count INTEGER NOT NULL DEFAULT 0,
                    size_bytes INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (account, container)
                ) WITHOUT ROWID;
                CREATE TABLE IF NOT EXISTS folder_usage (
                    account TEXT NOT NULL,
                    container TEXT NOT NULL,
                    depth INTEGER NOT NULL,
                    folder TEXT NOT NULL,
                    blob_count INTEGER NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    PRIMARY KEY (account, container, depth, folder)
                ) WITHOUT ROWID;
                CREATE INDEX IF NOT EXISTS folder_usage_by_depth
                    ON folder_usage(depth, account, container);
                CREATE TABLE IF NOT EXISTS scan_failures (
                    account TEXT NOT NULL,
                    container TEXT NOT NULL,
                    error TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 1,
                    failed_at TEXT NOT NULL,
                    PRIMARY KEY (account, container)
                ) WITHOUT ROWID;
                """
            )
            columns = {
                str(row["name"])
                for row in self.connection.execute(
                    "PRAGMA table_info(containers)"
                )
            }
            if "active" not in columns:
                self.connection.execute(
                    "ALTER TABLE containers "
                    "ADD COLUMN active INTEGER NOT NULL DEFAULT 0"
                )
            self.connection.execute("UPDATE containers SET active = 0")
            self.connection.commit()

    @staticmethod
    def now() -> str:
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    def close(self) -> None:
        with self.lock:
            self.connection.close()

    def initialize(
        self,
        subscription_id: str,
        max_depth: int,
        accounts: list[StorageAccount],
        excluded_containers: set[str],
        exclude_aml_snapshots: bool,
    ) -> None:
        expected = {
            "schema_version": SCHEMA_VERSION,
            "subscription_id": subscription_id,
            "max_depth": str(max_depth),
            "account_names": json.dumps(
                sorted(account.name for account in accounts),
                separators=(",", ":"),
            ),
            "excluded_containers": json.dumps(
                sorted(excluded_containers),
                separators=(",", ":"),
            ),
            "exclude_aml_snapshots": json.dumps(exclude_aml_snapshots),
        }
        now = self.now()
        with self.lock, self.connection:
            existing = {
                str(row["key"]): str(row["value"])
                for row in self.connection.execute(
                    "SELECT key, value FROM metadata"
                )
            }
            if existing:
                for key, value in expected.items():
                    if existing.get(key) != value:
                        raise AppError(
                            f"State {self.path} is incompatible for {key}: "
                            f"expected {value!r}, found {existing.get(key)!r}"
                        )
            else:
                self.connection.executemany(
                    "INSERT INTO metadata(key, value) VALUES (?, ?)",
                    {
                        **expected,
                        "created_at": now,
                    }.items(),
                )
            self.connection.executemany(
                """
                INSERT INTO accounts(name, resource_group, resource_id)
                VALUES (?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    resource_group = excluded.resource_group,
                    resource_id = excluded.resource_id
                """,
                [
                    (
                        account.name,
                        account.resource_group,
                        account.resource_id,
                    )
                    for account in accounts
                ],
            )
            self._set_metadata_unlocked("status", "running")
            self._set_metadata_unlocked("updated_at", now)

    def register_tasks(
        self,
        tasks: list[tuple[StorageAccount, str]],
    ) -> None:
        now = self.now()
        with self.lock, self.connection:
            self.connection.executemany(
                """
                INSERT OR IGNORE INTO containers(
                    account, container, updated_at
                ) VALUES (?, ?, ?)
                """,
                [
                    (account.name, container, now)
                    for account, container in tasks
                ],
            )

    def pending_tasks(
        self,
        tasks: list[tuple[StorageAccount, str]],
    ) -> list[tuple[StorageAccount, str]]:
        with self.lock:
            completed = {
                (str(row["account"]), str(row["container"]))
                for row in self.connection.execute(
                    "SELECT account, container FROM containers "
                    "WHERE completed = 1"
                )
            }
        return [
            task
            for task in tasks
            if (task[0].name, task[1]) not in completed
        ]

    def counts(self) -> tuple[int, int, int, int, int]:
        with self.lock:
            row = self.connection.execute(
                """
                SELECT
                    COUNT(*) AS total,
                    COALESCE(SUM(completed), 0) AS completed,
                    COALESCE(SUM(blob_count), 0) AS blobs,
                    COALESCE(SUM(size_bytes), 0) AS bytes,
                    COALESCE(SUM(page_count), 0) AS pages
                FROM containers
                """
            ).fetchone()
        return tuple(int(row[key]) for key in (
            "total",
            "completed",
            "blobs",
            "bytes",
            "pages",
        ))

    def marker(self, account: str, container: str) -> str:
        with self.lock:
            row = self.connection.execute(
                "SELECT next_marker FROM containers "
                "WHERE account = ? AND container = ?",
                (account, container),
            ).fetchone()
        if row is None:
            raise AppError(f"Missing scan state for {account}/{container}")
        return str(row["next_marker"])

    def set_active(
        self,
        account: str,
        container: str,
        active: bool,
    ) -> None:
        with self.lock, self.connection:
            self.connection.execute(
                "UPDATE containers SET active = ?, updated_at = ? "
                "WHERE account = ? AND container = ?",
                (int(active), self.now(), account, container),
            )

    def begin_task(self, account: str, container: str) -> bool:
        with self.lock, self.connection:
            was_failed = (
                self.connection.execute(
                    "SELECT 1 FROM scan_failures "
                    "WHERE account = ? AND container = ?",
                    (account, container),
                ).fetchone()
                is not None
            )
            if was_failed:
                self.connection.execute(
                    "DELETE FROM scan_failures "
                    "WHERE account = ? AND container = ?",
                    (account, container),
                )
            self.connection.execute(
                "UPDATE containers SET active = 1, updated_at = ? "
                "WHERE account = ? AND container = ?",
                (self.now(), account, container),
            )
        return was_failed

    def checkpoint(
        self,
        account: str,
        container: str,
        folders: dict[tuple[int, str], FolderDelta],
        next_marker: str,
        page_count: int,
        blob_count: int,
        size_bytes: int,
        completed: bool,
    ) -> None:
        now = self.now()
        with self.lock, self.connection:
            self.connection.executemany(
                """
                INSERT INTO folder_usage(
                    account, container, depth, folder, blob_count, size_bytes
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(account, container, depth, folder) DO UPDATE SET
                    blob_count = blob_count + excluded.blob_count,
                    size_bytes = size_bytes + excluded.size_bytes
                """,
                [
                    (
                        account,
                        container,
                        depth,
                        folder,
                        delta.blob_count,
                        delta.size_bytes,
                    )
                    for (depth, folder), delta in folders.items()
                ],
            )
            self.connection.execute(
                """
                UPDATE containers
                SET next_marker = ?,
                    completed = ?,
                    page_count = page_count + ?,
                    blob_count = blob_count + ?,
                    size_bytes = size_bytes + ?,
                    updated_at = ?
                WHERE account = ? AND container = ?
                """,
                (
                    next_marker,
                    int(completed),
                    page_count,
                    blob_count,
                    size_bytes,
                    now,
                    account,
                    container,
                ),
            )
            self._set_metadata_unlocked("updated_at", now)
            if completed:
                self.connection.execute(
                    "DELETE FROM scan_failures "
                    "WHERE account = ? AND container = ?",
                    (account, container),
                )

    def container_totals(
        self,
        account: str,
        container: str,
    ) -> tuple[int, int]:
        with self.lock:
            row = self.connection.execute(
                "SELECT blob_count, size_bytes FROM containers "
                "WHERE account = ? AND container = ?",
                (account, container),
            ).fetchone()
        return int(row["blob_count"]), int(row["size_bytes"])

    def record_failure(
        self,
        account: str,
        container: str,
        error: str,
    ) -> None:
        with self.lock, self.connection:
            self.connection.execute(
                """
                INSERT INTO scan_failures(
                    account, container, error, failed_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(account, container) DO UPDATE SET
                    error = excluded.error,
                    failed_at = excluded.failed_at,
                    attempts = attempts + 1
                """,
                (account, container, error, self.now()),
            )

    def failure_count(self) -> int:
        with self.lock:
            return int(
                self.connection.execute(
                    "SELECT COUNT(*) FROM scan_failures"
                ).fetchone()[0]
            )

    def set_status(self, status: str) -> None:
        with self.lock, self.connection:
            self._set_metadata_unlocked("status", status)
            self._set_metadata_unlocked("updated_at", self.now())

    def export_depth(self, depth: int, output_path: Path) -> int:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        fields = [
            "storage_account",
            "resource_group",
            "container",
            "folder",
            "folder_depth",
            "blob_count",
            "size_bytes",
            "size_gib",
            "account_percentage",
            "container_percentage",
            "container_complete",
        ]
        temporary_path: Path | None = None
        row_count = 0
        with self.lock:
            cursor = self.connection.execute(
                """
                WITH account_totals AS (
                    SELECT account, SUM(size_bytes) AS total_bytes
                    FROM folder_usage WHERE depth = ? GROUP BY account
                ),
                container_totals AS (
                    SELECT account, container, SUM(size_bytes) AS total_bytes
                    FROM folder_usage
                    WHERE depth = ? GROUP BY account, container
                )
                SELECT
                    f.account,
                    a.resource_group,
                    f.container,
                    f.folder,
                    f.depth,
                    f.blob_count,
                    f.size_bytes,
                    account_totals.total_bytes,
                    container_totals.total_bytes,
                    c.completed
                FROM folder_usage AS f
                JOIN accounts AS a ON a.name = f.account
                JOIN containers AS c
                  ON c.account = f.account AND c.container = f.container
                JOIN account_totals ON account_totals.account = f.account
                JOIN container_totals
                  ON container_totals.account = f.account
                 AND container_totals.container = f.container
                WHERE f.depth = ?
                ORDER BY f.account, f.size_bytes DESC, f.container, f.folder
                """,
                (depth, depth, depth),
            )
            try:
                with tempfile.NamedTemporaryFile(
                    "w",
                    encoding="utf-8",
                    newline="",
                    dir=output_path.parent,
                    prefix=f".{output_path.name}.",
                    delete=False,
                ) as output:
                    temporary_path = Path(output.name)
                    writer = csv.DictWriter(output, fieldnames=fields)
                    writer.writeheader()
                    for row in cursor:
                        size_bytes = int(row[6])
                        account_total = int(row[7])
                        container_total = int(row[8])
                        writer.writerow(
                            {
                                "storage_account": row[0],
                                "resource_group": row[1],
                                "container": row[2],
                                "folder": row[3],
                                "folder_depth": row[4],
                                "blob_count": row[5],
                                "size_bytes": size_bytes,
                                "size_gib": f"{size_bytes / 1024**3:.6f}",
                                "account_percentage": (
                                    f"{size_bytes * 100 / account_total:.6f}"
                                    if account_total
                                    else "0.000000"
                                ),
                                "container_percentage": (
                                    f"{size_bytes * 100 / container_total:.6f}"
                                    if container_total
                                    else "0.000000"
                                ),
                                "container_complete": bool(row[9]),
                            }
                        )
                        row_count += 1
                os.replace(temporary_path, output_path)
            finally:
                if temporary_path and temporary_path.exists():
                    temporary_path.unlink()
        return row_count

    def export_failures(self, output_path: Path) -> int:
        with self.lock:
            rows = self.connection.execute(
                """
                SELECT account, container, attempts, failed_at, error
                FROM scan_failures
                ORDER BY account, container
                """
            ).fetchall()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with output_path.open("w", encoding="utf-8", newline="") as output:
            writer = csv.writer(output)
            writer.writerow(
                ["storage_account", "container", "attempts", "failed_at", "error"]
            )
            writer.writerows(rows)
        return len(rows)

    def _set_metadata_unlocked(self, key: str, value: str) -> None:
        self.connection.execute(
            "INSERT OR REPLACE INTO metadata(key, value) VALUES (?, ?)",
            (key, value),
        )
