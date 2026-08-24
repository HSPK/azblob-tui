from __future__ import annotations

from pathlib import Path

from .settings import ScanPaths, UserSettings


def resolve_scan_paths(
    settings: UserSettings,
    subscription_id: str,
    *,
    state: Path | None = None,
    output: Path | None = None,
    log: Path | None = None,
    export_depth: int = 1,
    working_directory: Path | None = None,
) -> ScanPaths:
    working_directory = (working_directory or Path.cwd()).resolve()
    base = (
        settings.load_scan_paths(subscription_id)
        or _legacy_scan_paths(working_directory, export_depth)
        or settings.default_scan_paths(subscription_id, export_depth)
    )
    return ScanPaths(
        state=_resolve_override(state, working_directory) or base.state,
        output=_resolve_override(output, working_directory) or base.output,
        log=_resolve_override(log, working_directory) or base.log,
    )


def _resolve_override(path: Path | None, working_directory: Path) -> Path | None:
    if path is None:
        return None
    expanded = path.expanduser()
    if not expanded.is_absolute():
        expanded = working_directory / expanded
    return expanded.resolve()


def _legacy_scan_paths(
    working_directory: Path,
    export_depth: int,
) -> ScanPaths | None:
    candidates = (
        ScanPaths(
            state=working_directory / "abt-scan.sqlite",
            output=working_directory / f"abt-depth{export_depth}.csv",
            log=working_directory / "abt-scan.log",
        ),
        ScanPaths(
            state=working_directory / "blob-folder-usage-depth1-3.sqlite",
            output=working_directory / "blob-folder-usage-depth1-optimized.csv",
            log=working_directory / "blob-folder-usage-depth1-3-scan.log",
        ),
    )
    return next((paths for paths in candidates if paths.state.is_file()), None)
