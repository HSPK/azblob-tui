from __future__ import annotations

import argparse
import re
import sys
import threading
import time
import xml.etree.ElementTree as ET
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

from .azure import (
    AppError,
    StorageError,
)
from .blob import BlobRestClient, human_size, list_containers, next_marker
from .filters import is_aml_snapshot
from .models import StorageAccount, Subscription
from .scan_state import FolderDelta, ScanStore, StateLock


@dataclass(frozen=True)
class ScanOptions:
    subscription: Subscription
    accounts: list[StorageAccount]
    workspace_ids: dict[str, set[str]]
    state_path: Path
    output_path: Path
    failure_path: Path
    log_path: Path
    export_depth: int
    max_depth: int
    workers: int
    checkpoint_pages: int
    excluded_containers: set[str]
    exclude_aml_snapshots: bool


def parse_selection(value: str, item_count: int) -> list[int]:
    normalized = re.sub(r"\s+", "", value)
    if normalized.lower() == "all":
        return list(range(item_count))
    if not normalized:
        raise ValueError("Selection cannot be empty.")
    selected: list[int] = []
    seen: set[int] = set()
    for item in normalized.split(","):
        match = re.fullmatch(r"(\d+)-(\d+)", item)
        if match:
            start, end = map(int, match.groups())
        elif item.isdigit():
            start = end = int(item)
        else:
            raise ValueError(f"Invalid selection item: {item}")
        if start < 1 or end > item_count or start > end:
            raise ValueError(
                f"Selection {item!r} is outside 1-{item_count}."
            )
        for number in range(start, end + 1):
            index = number - 1
            if index not in seen:
                selected.append(index)
                seen.add(index)
    return selected


def folder_for_blob(blob_name: str, depth: int) -> str:
    parts = blob_name.split("/")
    if len(parts) == 1:
        return "_root"
    directories = parts[:-1]
    return "/".join(directories[:depth]) if any(directories) else "_root"


def parse_scan_page(
    root: ET.Element,
    max_depth: int,
) -> tuple[dict[tuple[int, str], FolderDelta], int, int]:
    folders: dict[tuple[int, str], FolderDelta] = defaultdict(FolderDelta)
    blob_count = 0
    size_bytes = 0
    for blob in root.findall("./Blobs/Blob"):
        name = blob.findtext("Name", default="")
        length_text = blob.findtext(
            "./Properties/Content-Length",
            default="0",
        )
        try:
            blob_size = int(length_text)
        except ValueError as error:
            raise StorageError(
                f"Blob {name!r} has invalid Content-Length {length_text!r}"
            ) from error
        for depth in range(1, max_depth + 1):
            key = (depth, folder_for_blob(name, depth))
            folders[key].blob_count += 1
            folders[key].size_bytes += blob_size
        blob_count += 1
        size_bytes += blob_size
    return dict(folders), blob_count, size_bytes


class ProgressReporter:
    def __init__(
        self,
        total: int,
        completed: int,
        failed: int,
        blob_count: int,
        page_count: int,
        size_bytes: int,
        log_path: Path,
    ) -> None:
        self.total = total
        self.completed = completed
        self.failed = failed
        self.blob_count = blob_count
        self.initial_blob_count = blob_count
        self.page_count = page_count
        self.size_bytes = size_bytes
        self.active = 0
        self.started_at = time.monotonic()
        self.last_report = 0.0
        self.log_path = log_path
        self.lock = threading.Lock()
        log_path.parent.mkdir(parents=True, exist_ok=True)

    def start(self) -> None:
        with self.lock:
            self._emit()

    def task_started(self, was_failed: bool) -> None:
        with self.lock:
            self.active += 1
            if was_failed:
                self.failed = max(self.failed - 1, 0)

    def page(self, blobs: int, size_bytes: int) -> None:
        with self.lock:
            self.blob_count += blobs
            self.size_bytes += size_bytes
            self.page_count += 1
            if time.monotonic() - self.last_report >= 5:
                self._emit()

    def succeeded(self) -> None:
        with self.lock:
            self.active -= 1
            self.completed += 1
            self._emit()

    def failed_task(self) -> None:
        with self.lock:
            self.active -= 1
            self.failed += 1
            self._emit()

    def finish(self) -> None:
        with self.lock:
            self._emit()

    def _emit(self) -> None:
        processed = self.completed + self.failed
        elapsed = max(time.monotonic() - self.started_at, 0.001)
        percentage = processed * 100 / self.total if self.total else 100
        line = (
            f"Progress: containers {processed:,}/{self.total:,} "
            f"({percentage:5.1f}%, active {self.active:,}, "
            f"failed {self.failed:,}) | blobs {self.blob_count:,} "
            f"| pages {self.page_count:,} | size {human_size(self.size_bytes)} "
            f"| rate "
            f"{(self.blob_count - self.initial_blob_count) / elapsed:,.0f} "
            f"blobs/s "
            f"| elapsed {time.strftime('%H:%M:%S', time.gmtime(elapsed))}"
        )
        print(line, file=sys.stderr, flush=True)
        with self.log_path.open("a", encoding="utf-8") as output:
            output.write(f"{line}\n")
        self.last_report = time.monotonic()


def discover_tasks(
    client: BlobRestClient,
    accounts: list[StorageAccount],
    workspace_ids: dict[str, set[str]],
    excluded_containers: set[str],
    exclude_aml_snapshots: bool,
    workers: int,
) -> tuple[list[tuple[StorageAccount, str]], list[tuple[str, str]]]:
    tasks: list[tuple[StorageAccount, str]] = []
    failures: list[tuple[str, str]] = []
    with ThreadPoolExecutor(max_workers=min(workers, len(accounts))) as executor:
        future_accounts = {
            executor.submit(list_containers, client, account): account
            for account in accounts
        }
        for future in as_completed(future_accounts):
            account = future_accounts[future]
            try:
                names = future.result()
            except StorageError as error:
                failures.append((account.name, str(error)))
                continue
            for name in names:
                if name in excluded_containers:
                    continue
                if exclude_aml_snapshots and is_aml_snapshot(
                    name,
                    workspace_ids.get(account.name.lower(), set()),
                ):
                    continue
                tasks.append((account, name))
    tasks.sort(key=lambda item: (item[0].name, item[1]))
    return tasks, failures


def merge_deltas(
    target: dict[tuple[int, str], FolderDelta],
    source: dict[tuple[int, str], FolderDelta],
) -> None:
    for key, delta in source.items():
        current = target.setdefault(key, FolderDelta())
        current.blob_count += delta.blob_count
        current.size_bytes += delta.size_bytes


def scan_container(
    client: BlobRestClient,
    store: ScanStore,
    progress: ProgressReporter,
    account: StorageAccount,
    container: str,
    max_depth: int,
    checkpoint_pages: int,
) -> None:
    was_failed = store.begin_task(account.name, container)
    progress.task_started(was_failed)
    marker = store.marker(account.name, container)
    pending: dict[tuple[int, str], FolderDelta] = {}
    pending_pages = 0
    pending_blobs = 0
    pending_bytes = 0
    try:
        while True:
            parameters: dict[str, str | int] = {
                "restype": "container",
                "comp": "list",
                "maxresults": 5000,
            }
            if account.hns_enabled:
                parameters["showonly"] = "files"
            if marker:
                parameters["marker"] = marker
            root = client.get_xml(account, container, parameters)
            deltas, blobs, size_bytes = parse_scan_page(root, max_depth)
            merge_deltas(pending, deltas)
            pending_pages += 1
            pending_blobs += blobs
            pending_bytes += size_bytes
            progress.page(blobs, size_bytes)
            next_page = next_marker(root)
            complete = not next_page
            if pending_pages >= checkpoint_pages or complete:
                store.checkpoint(
                    account.name,
                    container,
                    pending,
                    next_page,
                    pending_pages,
                    pending_blobs,
                    pending_bytes,
                    complete,
                )
                pending = {}
                pending_pages = 0
                pending_blobs = 0
                pending_bytes = 0
            marker = next_page
            if complete:
                progress.succeeded()
                return
    except StorageError as error:
        store.record_failure(account.name, container, str(error))
        progress.failed_task()
    finally:
        store.set_active(account.name, container, False)


def run_scan(
    client: BlobRestClient,
    options: ScanOptions,
) -> int:
    with StateLock(options.state_path):
        options.log_path.parent.mkdir(parents=True, exist_ok=True)
        options.log_path.write_text("", encoding="utf-8")
        tasks, account_failures = discover_tasks(
            client,
            options.accounts,
            options.workspace_ids,
            options.excluded_containers,
            options.exclude_aml_snapshots,
            options.workers,
        )
        for account, error in account_failures:
            print(
                f"Unable to list Containers for {account}: {error}",
                file=sys.stderr,
            )
        store = ScanStore(options.state_path)
        try:
            store.initialize(
                options.subscription.id,
                options.max_depth,
                options.accounts,
                options.excluded_containers,
                options.exclude_aml_snapshots,
            )
            store.register_tasks(tasks)
            pending = store.pending_tasks(tasks)
            total, completed, blobs, size_bytes, pages = store.counts()
            progress = ProgressReporter(
                total,
                completed,
                store.failure_count(),
                blobs,
                pages,
                size_bytes,
                options.log_path,
            )
            progress.start()
            with ThreadPoolExecutor(max_workers=options.workers) as executor:
                futures = [
                    executor.submit(
                        scan_container,
                        client,
                        store,
                        progress,
                        account,
                        container,
                        options.max_depth,
                        options.checkpoint_pages,
                    )
                    for account, container in pending
                ]
                for future in as_completed(futures):
                    future.result()
            progress.finish()
            failures = store.failure_count()
            status = "complete" if not failures and not account_failures else "partial"
            store.set_status(status)
            rows = store.export_depth(options.export_depth, options.output_path)
            failure_rows = store.export_failures(options.failure_path)
            print(
                f"State: {options.state_path}\n"
                f"Depth {options.export_depth}: {options.output_path} "
                f"({rows:,} rows)\n"
                f"Failures: {options.failure_path} ({failure_rows:,} rows)"
            )
            return 0 if status == "complete" else 2
        finally:
            store.close()
