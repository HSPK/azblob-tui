# azblob-tui

`azblob-tui` is a dependency-free terminal browser for Azure Blob Storage.
The installed command is `abt`.

This is an independent community project and is not an official Microsoft
product.

## Install

```bash
uv tool install azblob-tui
```

Alternatively:

```bash
pipx install azblob-tui
```

Authenticate before starting the TUI:

```bash
az login
abt
```

## Scan folder usage

`abt scan` performs a metadata-only scan, aggregates folder depths in one pass,
and writes resumable SQLite checkpoints:

```bash
abt scan \
  --subscription <subscription-id> \
  --all \
  --max-depth 3
```

AML per-snapshot UUID Containers are excluded by default. Reusing the same
`--state` resumes continuation markers, skips completed Containers, and retries
failed or interrupted Containers. Progress is written to `abt-scan.log` by
default and can be displayed live by the TUI. Scan files are stored under
`${XDG_STATE_HOME:-~/.local/state}/azblob-tui/scans/<subscription-id>/`, so
their location does not depend on the shell working directory.

```bash
abt
```

Explicit `--state`, `--output`, and `--log` paths have highest priority.
Otherwise, `abt` uses the paths last saved for the subscription, then recognizes
legacy scan files in the current directory, and finally uses the subscription
default directory.

Features:

- Select an Azure subscription and Storage Account inside the TUI.
- Remember the selected subscription between launches.
- Cache Storage Accounts and AML workspace mappings for 10 days; press `r`
  on the account screen to refresh immediately and `S` to change subscription.
- Hide Azure ML per-snapshot UUID containers by default.
- Toggle soft-deleted Containers and Blobs with `x`; show deletion time,
  remaining retention days, and Container/Blob version identifiers.
- Search accounts, containers, and the current Blob page.
- Browse virtual folders with paginated `List Blobs` requests.
- Coalesce short Azure service pages caused by partition boundaries into one
  logical TUI page, so immediate-child folder totals are complete.
- View Blob size, type, access tier, timestamps, ETag, URL, and metadata.
- Read optional usage/status data from `blob_folder_usage.py` SQLite state.
- Show live queue metrics only in the lower-right panel and provide an
  auto-refreshing per-account statistics screen (`s`).
- Adapt table columns to the terminal width.
- Sort each screen by cycling columns with `o` and toggling direction with `O`.
- Keep selected-item metadata in the upper-right pane and live queue metrics
  in the lower-right pane.
- Show scanner-provided depth 1-3 folder sizes and human-readable Blob counts.
- Use `q` to go back, `h` for full help, and `Ctrl+C` to exit.
- Scroll extended Blob properties and metadata with `[` / `]`.
- Delete an individual Blob or Container with `D` and typed confirmation.
  Partial/in-progress scanner Containers are protected from deletion.
  Soft-deleted items are read-only; restoration is not performed automatically.

Configuration is stored under `${XDG_CONFIG_HOME:-~/.config}/azure-blob-tui`.
Noncredential resource metadata is cached under
`${XDG_CACHE_HOME:-~/.cache}/azure-blob-tui`. Access tokens are never persisted
by this application.

The lower-right queue panel uses the latest `Progress:` record from
`--scan-log` for live processed, active, failed, Blob, page, size, rate, and
elapsed-time values. The per-account statistics table uses SQLite checkpoints,
so it can lag the live log by up to one checkpoint interval.

## Run from source

```bash
PYTHONPATH=src python -m azure_blob_tui
```

Open a known account directly:

```bash
PYTHONPATH=src python -m azure_blob_tui \
  --subscription <subscription-id> \
  --account <storage-account>
```

Show live scanner queue statistics:

```bash
PYTHONPATH=src python -m azure_blob_tui \
  --state ../blob-folder-usage-depth1-3.sqlite \
  --scan-log ../blob-folder-usage-depth1-3-scan.log
```

## Architecture

- `azure.py`: Azure CLI catalog, subscriptions, accounts, workspace mappings,
  and token lifecycle.
- `blob.py`: paginated Blob REST client and response parsing.
- `state.py`: optional scanner-state integration.
- `stats.py`: read-only real-time queue statistics.
- `scan_paths.py`: subscription-aware scan path resolution and legacy fallback.
- `table.py`: terminal-width-aware column fitting and rendering.
- `ui.py`: curses navigation and screens.
- `cli.py`: argument parsing and application assembly.

Keyboard shortcuts are available with `h` or `?`.

## Safety

Browsing is read-only. Deletion is available only through the uppercase `D`
shortcut and requires typed confirmation. Blob deletion uses the displayed
ETag to reject changes made after the page was loaded. Ambiguous network
responses are never retried automatically.

## License

MIT
