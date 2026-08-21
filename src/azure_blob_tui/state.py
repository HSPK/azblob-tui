from __future__ import annotations

import sqlite3
from pathlib import Path

from .models import ContainerUsage, FolderUsage


def load_usage_state(
    state_path: Path | None,
    account_name: str,
) -> dict[str, ContainerUsage]:
    if state_path is None or not state_path.is_file():
        return {}
    try:
        connection = sqlite3.connect(
            f"{state_path.resolve().as_uri()}?mode=ro",
            uri=True,
            timeout=5,
        )
        rows = connection.execute(
            """
            SELECT container, completed, page_count, blob_count, size_bytes
            FROM containers
            WHERE account = ?
            """,
            (account_name,),
        ).fetchall()
        connection.close()
    except sqlite3.Error:
        return {}
    return {
        str(row[0]): ContainerUsage(
            completed=bool(row[1]),
            page_count=int(row[2]),
            blob_count=int(row[3]),
            size_bytes=int(row[4]),
        )
        for row in rows
    }


def load_folder_usage(
    state_path: Path | None,
    account_name: str,
    container_name: str,
) -> dict[tuple[int, str], FolderUsage]:
    _, folders = load_container_folder_usage(
        state_path,
        account_name,
        container_name,
    )
    return folders


def load_container_folder_usage(
    state_path: Path | None,
    account_name: str,
    container_name: str,
) -> tuple[
    ContainerUsage | None,
    dict[tuple[int, str], FolderUsage],
]:
    if state_path is None or not state_path.is_file():
        return None, {}
    try:
        connection = sqlite3.connect(
            f"{state_path.resolve().as_uri()}?mode=ro",
            uri=True,
            timeout=5,
        )
        container_row = connection.execute(
            """
            SELECT completed, page_count, blob_count, size_bytes
            FROM containers
            WHERE account = ? AND container = ?
            """,
            (account_name, container_name),
        ).fetchone()
        folder_rows = connection.execute(
            """
            SELECT depth, folder, blob_count, size_bytes
            FROM folder_usage
            WHERE account = ? AND container = ?
            """,
            (account_name, container_name),
        ).fetchall()
        connection.close()
    except sqlite3.Error:
        return None, {}
    container = (
        ContainerUsage(
            completed=bool(container_row[0]),
            page_count=int(container_row[1]),
            blob_count=int(container_row[2]),
            size_bytes=int(container_row[3]),
        )
        if container_row is not None
        else None
    )
    folders = {
        (int(row[0]), str(row[1])): FolderUsage(
            depth=int(row[0]),
            blob_count=int(row[2]),
            size_bytes=int(row[3]),
        )
        for row in folder_rows
    }
    return container, folders
