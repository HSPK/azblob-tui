# Changelog

## 0.4.0 - 2026-08-26

- Restore selected soft-deleted Containers and Blobs with `R` and typed
  confirmation.
- Support version-specific Container restore and HNS path restore using the
  Azure-provided deletion ID.
- Keep restore requests non-retrying when the network result is ambiguous.

## 0.3.0 - 2026-08-25

- Toggle soft-deleted Containers and Blobs with `x`.
- Show deletion time, remaining retention days, and version identifiers.
- Keep soft-deleted items read-only while preserving existing deletion safety.

## 0.2.4 - 2026-08-25

- Restore the TUI refresh timeout without relying on the unsupported curses
  `window.getdelay()` method.

## 0.2.3 - 2026-08-24

- Disable the one-second TUI refresh timeout while typing confirmation or
  prefix input, then restore it afterward.

## 0.2.2 - 2026-08-24

- Store scan state, exports, and logs in a subscription-specific XDG state
  directory instead of depending on the shell working directory.
- Share path resolution between `abt` and `abt scan`, with explicit arguments,
  saved subscription paths, and legacy working-directory files supported in
  that order.
- Persist each subscription's active scan paths so the TUI automatically finds
  live statistics and folder-size data.

## 0.2.1 - 2026-08-24

- Open Storage Accounts directly from live scanner statistics.
- Preserve Size sorting when entering Containers and nested Blob directories.
- Rename the Container `Scanned Size` column to `Size`.

## 0.2.0 - 2026-08-21

- Integrate metadata-only, multi-depth, checkpointed scanning as `abt scan`.
- Persist scan failures and safely retry incomplete Containers by reusing the
  same SQLite state.

## 0.1.0 - 2026-08-21

- Browse Azure subscriptions, Storage Accounts, Containers, virtual folders,
  and Blobs in a curses TUI.
- Hide Azure ML per-snapshot UUID Containers by default.
- Show extended Blob properties, metadata, URLs, access tiers, and ETags.
- Integrate depth 1-3 folder aggregates and live scanner queue statistics.
- Cache Storage Account discovery for 10 days and remember the selected
  subscription.
- Support adaptive table layouts, sorting, filtering, pagination, and guarded
  Blob/Container deletion.
