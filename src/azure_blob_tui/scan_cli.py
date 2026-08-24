from __future__ import annotations

import argparse
from pathlib import Path

from .azure import AppError, AzureCatalog, find_subscription
from .blob import BlobRestClient
from .models import StorageAccount, Subscription
from .scan_paths import resolve_scan_paths
from .scanner import ScanOptions, parse_selection, run_scan


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="abt scan",
        description=(
            "Scan Blob metadata once, aggregate virtual-folder depths into "
            "SQLite, and resume or retry incomplete Containers safely."
        ),
    )
    parser.add_argument("--subscription", help="Subscription name or ID.")
    account_group = parser.add_mutually_exclusive_group()
    account_group.add_argument(
        "--all",
        action="store_true",
        help="Scan every Blob-capable Storage Account in the subscription.",
    )
    account_group.add_argument(
        "--accounts",
        help="Comma-separated Storage Account names.",
    )
    parser.add_argument(
        "--depth",
        type=int,
        default=1,
        help="Depth to export after the scan (default: 1).",
    )
    parser.add_argument(
        "--max-depth",
        type=int,
        default=3,
        help="Aggregate every depth from 1 through this value (default: 3).",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=16,
        help="Concurrent Container workers (default: 16).",
    )
    parser.add_argument(
        "--checkpoint-pages",
        type=int,
        default=10,
        help="Commit progress after this many pages per Container (default: 10).",
    )
    parser.add_argument(
        "--state",
        type=Path,
        help="SQLite checkpoint path; overrides the subscription default.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Export CSV path; overrides the subscription default.",
    )
    parser.add_argument(
        "--log",
        type=Path,
        help="Progress log path; overrides the subscription default.",
    )
    parser.add_argument(
        "--exclude-container",
        action="append",
        default=[],
        metavar="NAME",
        help="Exclude a Container; repeat for multiple values.",
    )
    parser.add_argument(
        "--include-aml-snapshots",
        action="store_true",
        help="Include AML per-snapshot UUID Containers (excluded by default).",
    )
    parser.add_argument(
        "--refresh-accounts",
        action="store_true",
        help="Bypass the 10-day Storage Account cache.",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip the confirmation prompt.",
    )
    return parser


def resolve_subscription(
    catalog: AzureCatalog,
    value: str | None,
) -> Subscription:
    selected = value or catalog.selected_subscription_id()
    if selected:
        try:
            subscription = find_subscription(catalog.subscriptions, selected)
            catalog.remember_subscription(subscription.id)
            return subscription
        except AppError:
            if value:
                raise
    print("Azure subscriptions:")
    for index, subscription in enumerate(catalog.subscriptions, 1):
        default = " [default]" if subscription.is_default else ""
        print(f"{index:>3}. {subscription.name} ({subscription.id}){default}")
    raw = input("Select subscription: ").strip()
    try:
        index = int(raw) - 1
        subscription = catalog.subscriptions[index]
    except (ValueError, IndexError) as error:
        raise AppError("Invalid subscription selection.") from error
    catalog.remember_subscription(subscription.id)
    return subscription


def select_accounts(
    accounts: list[StorageAccount],
    select_all: bool,
    names: str | None,
) -> list[StorageAccount]:
    if select_all:
        return accounts
    if names:
        requested = {
            value.strip().lower()
            for value in names.split(",")
            if value.strip()
        }
        by_name = {account.name.lower(): account for account in accounts}
        missing = sorted(requested - by_name.keys())
        if missing:
            raise AppError(
                f"Storage Account not found: {', '.join(missing)}"
            )
        return [by_name[name] for name in sorted(requested)]

    print("Storage Accounts:")
    for index, account in enumerate(accounts, 1):
        print(
            f"{index:>3}. {account.name:<26} "
            f"{account.resource_group:<24} {account.location}"
        )
    raw = input("Select accounts (for example 1-3,5 or all): ").strip()
    try:
        indexes = parse_selection(raw, len(accounts))
    except ValueError as error:
        raise AppError(str(error)) from error
    return [accounts[index] for index in indexes]


def run(arguments: list[str] | None = None) -> int:
    args = build_parser().parse_args(arguments)
    if args.depth < 1 or args.max_depth < args.depth:
        raise AppError("--max-depth must be greater than or equal to --depth.")
    if args.max_depth > 32:
        raise AppError("--max-depth cannot exceed 32.")
    if not 1 <= args.workers <= 64:
        raise AppError("--workers must be between 1 and 64.")
    if not 1 <= args.checkpoint_pages <= 1000:
        raise AppError("--checkpoint-pages must be between 1 and 1000.")

    catalog = AzureCatalog()
    subscription = resolve_subscription(catalog, args.subscription)
    accounts, workspace_ids, token = catalog.load_subscription(
        subscription,
        force_refresh=args.refresh_accounts,
    )
    selected_accounts = select_accounts(
        accounts,
        args.all,
        args.accounts,
    )
    if not selected_accounts:
        raise AppError("No Storage Accounts selected.")

    paths = resolve_scan_paths(
        catalog.settings,
        subscription.id,
        state=args.state,
        output=args.output,
        log=args.log,
        export_depth=args.depth,
    )
    failure_path = paths.output.with_name(f"{paths.output.stem}.errors.csv")
    options = ScanOptions(
        subscription=subscription,
        accounts=selected_accounts,
        workspace_ids=workspace_ids,
        state_path=paths.state,
        output_path=paths.output,
        failure_path=failure_path.expanduser().resolve(),
        log_path=paths.log,
        export_depth=args.depth,
        max_depth=args.max_depth,
        workers=args.workers,
        checkpoint_pages=args.checkpoint_pages,
        excluded_containers=set(args.exclude_container),
        exclude_aml_snapshots=not args.include_aml_snapshots,
    )
    print(
        f"Subscription: {subscription.name}\n"
        f"Storage Accounts: {len(selected_accounts)}\n"
        f"Depths: 1-{args.max_depth}; export depth {args.depth}\n"
        f"AML snapshots: "
        f"{'included' if args.include_aml_snapshots else 'excluded'}\n"
        f"State: {options.state_path}\n"
        f"Output: {options.output_path}\n"
        f"Log: {options.log_path}\n"
        "Reusing the same state resumes checkpoints and retries incomplete "
        "Containers."
    )
    if not args.yes:
        if input("Start scan? [y/N]: ").strip().lower() not in {"y", "yes"}:
            print("Cancelled.")
            return 0
    for path in (
        options.state_path,
        options.output_path,
        options.failure_path,
        options.log_path,
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
    catalog.settings.save_scan_paths(subscription.id, paths)
    client = BlobRestClient(token, timeout_seconds=60)
    result = run_scan(client, options)
    with options.log_path.open("a", encoding="utf-8") as output:
        output.write(f"scan-exit={result}\n")
    return result
