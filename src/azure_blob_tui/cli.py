from __future__ import annotations

import argparse
import curses
import shutil
import sys
from pathlib import Path

from .azure import AppError, AzureCatalog, StorageError, find_subscription
from .stats import StatsProvider
from .ui import BlobBrowser
from .scan_cli import run as run_scan


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Browse Azure Blob Storage in a terminal. Select a subscription "
            "and account interactively; AML UUID snapshots are hidden by default."
        ),
        epilog="Scanner: abt scan --help",
    )
    parser.add_argument(
        "--subscription",
        help="Subscription name or ID to open immediately.",
    )
    parser.add_argument(
        "--account",
        help="Storage Account to open immediately.",
    )
    parser.add_argument(
        "--show-snapshots",
        action="store_true",
        help="Show AML UUID snapshot containers initially.",
    )
    parser.add_argument(
        "--state",
        type=Path,
        help="Optional blob_folder_usage.py SQLite state for size/status columns.",
    )
    parser.add_argument(
        "--page-size",
        type=int,
        default=500,
        help="Blob items per page, 1-5000 (default: 500).",
    )
    parser.add_argument(
        "--scan-log",
        type=Path,
        help="Optional scanner log used for live active/failed/rate statistics.",
    )
    return parser


def run(arguments: list[str] | None = None) -> int:
    command_arguments = (
        list(arguments)
        if arguments is not None
        else sys.argv[1:]
    )
    if command_arguments and command_arguments[0] == "scan":
        return run_scan(command_arguments[1:])
    args = build_parser().parse_args(command_arguments)
    if not shutil.which("az"):
        raise AppError("Azure CLI was not found in PATH.")
    if not 1 <= args.page_size <= 5000:
        raise AppError("--page-size must be between 1 and 5000.")

    catalog = AzureCatalog()
    if not catalog.subscriptions:
        raise AppError("No enabled Azure subscriptions were found.")

    initial_subscription = None
    saved_subscription_id = catalog.selected_subscription_id()
    if args.subscription:
        initial_subscription = find_subscription(
            catalog.subscriptions,
            args.subscription,
        )
        catalog.remember_subscription(initial_subscription.id)
    elif saved_subscription_id:
        try:
            initial_subscription = find_subscription(
                catalog.subscriptions,
                saved_subscription_id,
            )
        except AppError:
            initial_subscription = None
    elif args.account:
        initial_subscription = next(
            (
                subscription
                for subscription in catalog.subscriptions
                if subscription.is_default
            ),
            catalog.subscriptions[0],
        )
        catalog.remember_subscription(initial_subscription.id)

    state_path = args.state
    if state_path is None:
        default_state = Path("blob-folder-usage-depth1-3.sqlite")
        state_path = default_state if default_state.is_file() else None
    elif not state_path.is_file():
        raise AppError(f"State database not found: {state_path}")

    scan_log_path = args.scan_log
    if scan_log_path is None:
        default_log = Path("blob-folder-usage-depth1-3-scan.log")
        scan_log_path = default_log if default_log.is_file() else None
    elif not scan_log_path.is_file():
        raise AppError(f"Scan log not found: {scan_log_path}")
    stats_provider = (
        StatsProvider(state_path, scan_log_path)
        if state_path is not None
        else None
    )

    app = BlobBrowser(
        catalog=catalog,
        state_path=state_path,
        stats_provider=stats_provider,
        page_size=args.page_size,
        show_snapshots=args.show_snapshots,
        initial_subscription=initial_subscription,
        initial_account=args.account,
    )
    curses.wrapper(app.run)
    return 0


def main() -> int:
    try:
        return run()
    except KeyboardInterrupt:
        return 130
    except (AppError, StorageError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
