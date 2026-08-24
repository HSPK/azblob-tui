from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from .models import StorageAccount


SETTINGS_VERSION = 1
DEFAULT_CACHE_TTL_SECONDS = 10 * 24 * 60 * 60


@dataclass(frozen=True)
class AccountCache:
    accounts: list[StorageAccount]
    snapshot_prefixes: dict[str, set[str]]
    age_seconds: float


@dataclass(frozen=True)
class ScanPaths:
    state: Path
    output: Path
    log: Path


class UserSettings:
    def __init__(
        self,
        config_dir: Path | None = None,
        cache_dir: Path | None = None,
        state_dir: Path | None = None,
        cache_ttl_seconds: int = DEFAULT_CACHE_TTL_SECONDS,
    ) -> None:
        self.config_dir = config_dir or (
            Path(
                os.environ.get(
                    "XDG_CONFIG_HOME",
                    Path.home() / ".config",
                )
            )
            / "azure-blob-tui"
        )
        self.cache_dir = cache_dir or (
            Path(
                os.environ.get(
                    "XDG_CACHE_HOME",
                    Path.home() / ".cache",
                )
            )
            / "azure-blob-tui"
        )
        self.state_dir = state_dir or (
            Path(
                os.environ.get(
                    "XDG_STATE_HOME",
                    Path.home() / ".local" / "state",
                )
            )
            / "azblob-tui"
            / "scans"
        )
        self.cache_ttl_seconds = cache_ttl_seconds

    @property
    def config_path(self) -> Path:
        return self.config_dir / "config.json"

    def selected_subscription_id(self) -> str | None:
        payload = self._read_json(self.config_path)
        if payload is None or payload.get("version") != SETTINGS_VERSION:
            return None
        value = payload.get("selected_subscription_id")
        return str(value) if value else None

    def save_selected_subscription(self, subscription_id: str) -> None:
        payload = self._config_payload()
        payload["selected_subscription_id"] = subscription_id
        self._write_json(self.config_path, payload)

    def default_scan_paths(
        self,
        subscription_id: str,
        export_depth: int = 1,
    ) -> ScanPaths:
        directory = self.state_dir / self._safe_subscription_id(
            subscription_id
        )
        return ScanPaths(
            state=directory / "abt-scan.sqlite",
            output=directory / f"abt-depth{export_depth}.csv",
            log=directory / "abt-scan.log",
        )

    def load_scan_paths(self, subscription_id: str) -> ScanPaths | None:
        payload = self._config_payload()
        all_paths = payload.get("scan_paths")
        if not isinstance(all_paths, dict):
            return None
        raw = all_paths.get(subscription_id)
        if not isinstance(raw, dict):
            return None
        try:
            return ScanPaths(
                state=Path(str(raw["state"])).expanduser().resolve(),
                output=Path(str(raw["output"])).expanduser().resolve(),
                log=Path(str(raw["log"])).expanduser().resolve(),
            )
        except KeyError:
            return None

    def save_scan_paths(
        self,
        subscription_id: str,
        paths: ScanPaths,
    ) -> None:
        payload = self._config_payload()
        raw_paths = payload.get("scan_paths")
        all_paths = dict(raw_paths) if isinstance(raw_paths, dict) else {}
        all_paths[subscription_id] = {
            "state": str(paths.state),
            "output": str(paths.output),
            "log": str(paths.log),
        }
        payload["scan_paths"] = all_paths
        self._write_json(self.config_path, payload)

    def load_account_cache(
        self,
        subscription_id: str,
        now: float | None = None,
    ) -> AccountCache | None:
        payload = self._read_json(self._account_cache_path(subscription_id))
        if payload is None or payload.get("version") != SETTINGS_VERSION:
            return None
        if payload.get("subscription_id") != subscription_id:
            return None
        try:
            created_at = float(payload["created_at"])
            current_time = time.time() if now is None else now
            age_seconds = max(current_time - created_at, 0)
            if age_seconds > self.cache_ttl_seconds:
                return None
            accounts = [
                StorageAccount(**item)
                for item in payload.get("accounts", [])
            ]
            prefixes = {
                str(account): {str(value) for value in values}
                for account, values in payload.get(
                    "snapshot_prefixes",
                    {},
                ).items()
            }
        except (KeyError, TypeError, ValueError):
            return None
        return AccountCache(accounts, prefixes, age_seconds)

    def save_account_cache(
        self,
        subscription_id: str,
        accounts: list[StorageAccount],
        snapshot_prefixes: dict[str, set[str]],
        now: float | None = None,
    ) -> None:
        self._write_json(
            self._account_cache_path(subscription_id),
            {
                "version": SETTINGS_VERSION,
                "subscription_id": subscription_id,
                "created_at": time.time() if now is None else now,
                "accounts": [asdict(account) for account in accounts],
                "snapshot_prefixes": {
                    account: sorted(values)
                    for account, values in snapshot_prefixes.items()
                },
            },
        )

    def _account_cache_path(self, subscription_id: str) -> Path:
        return self.cache_dir / (
            f"accounts-{self._safe_subscription_id(subscription_id)}.json"
        )

    @staticmethod
    def _safe_subscription_id(subscription_id: str) -> str:
        return "".join(
            character
            for character in subscription_id
            if character.isalnum() or character in "-_"
        )

    def _config_payload(self) -> dict[str, object]:
        payload = self._read_json(self.config_path)
        if payload is None or payload.get("version") != SETTINGS_VERSION:
            return {"version": SETTINGS_VERSION}
        return dict(payload)

    @staticmethod
    def _read_json(path: Path) -> dict[str, object] | None:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) else None

    @staticmethod
    def _write_json(path: Path, payload: dict[str, object]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=path.parent,
                prefix=f".{path.name}.",
                delete=False,
            ) as output:
                temporary_path = Path(output.name)
                json.dump(
                    payload,
                    output,
                    indent=2,
                    sort_keys=True,
                )
                output.write("\n")
            os.chmod(temporary_path, 0o600)
            os.replace(temporary_path, path)
        finally:
            if temporary_path and temporary_path.exists():
                temporary_path.unlink()
