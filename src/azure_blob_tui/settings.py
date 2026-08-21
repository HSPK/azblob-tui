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


class UserSettings:
    def __init__(
        self,
        config_dir: Path | None = None,
        cache_dir: Path | None = None,
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
        self._write_json(
            self.config_path,
            {
                "version": SETTINGS_VERSION,
                "selected_subscription_id": subscription_id,
            },
        )

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
        safe_id = "".join(
            character
            for character in subscription_id
            if character.isalnum() or character in "-_"
        )
        return self.cache_dir / f"accounts-{safe_id}.json"

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
