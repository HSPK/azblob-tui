from __future__ import annotations

import json
import subprocess
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

from .models import StorageAccount, Subscription
from .settings import UserSettings


STORAGE_RESOURCES = {
    "AzureCloud": "https://storage.azure.com/",
    "AzureChinaCloud": "https://storage.azure.cn/",
    "AzureUSGovernment": "https://storage.azure.us/",
    "AzureGermanCloud": "https://storage.microsoftazure.de/",
}


class AppError(RuntimeError):
    pass


class StorageError(RuntimeError):
    pass


def run_az(arguments: list[str]) -> object:
    command = ["az", *arguments, "--only-show-errors", "--output", "json"]
    completed = subprocess.run(
        command,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if completed.returncode != 0:
        raise AppError(
            completed.stderr.strip() or "Azure CLI command failed."
        )
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise AppError("Azure CLI returned invalid JSON.") from error


def list_subscriptions() -> list[Subscription]:
    raw = run_az(
        [
            "account",
            "list",
            "--query",
            "[].{id:id,name:name,tenantId:tenantId,isDefault:isDefault,state:state}",
        ]
    )
    if not isinstance(raw, list):
        raise AppError("Azure CLI returned an invalid subscription list.")
    subscriptions = [
        Subscription(
            id=str(item.get("id", "")),
            name=str(item.get("name", "")),
            tenant_id=str(item.get("tenantId", "")),
            is_default=bool(item.get("isDefault", False)),
        )
        for item in raw
        if str(item.get("state", "")).lower() == "enabled"
    ]
    subscriptions.sort(key=lambda item: (not item.is_default, item.name.lower()))
    return subscriptions


def find_subscription(
    subscriptions: list[Subscription],
    value: str,
) -> Subscription:
    match = next(
        (
            subscription
            for subscription in subscriptions
            if subscription.id.lower() == value.lower()
            or subscription.name.lower() == value.lower()
        ),
        None,
    )
    if match is None:
        raise AppError(f"Subscription not found: {value}")
    return match


def cloud_name() -> str:
    raw = run_az(["cloud", "show", "--query", "{name:name}"])
    if not isinstance(raw, dict) or not raw.get("name"):
        raise AppError("Unable to determine the active Azure cloud.")
    return str(raw["name"])


def storage_resource(cloud: str) -> str:
    resource = STORAGE_RESOURCES.get(cloud)
    if not resource:
        raise AppError(f"Unsupported Azure cloud: {cloud}")
    return resource


def list_storage_accounts(subscription_id: str) -> list[StorageAccount]:
    raw = run_az(
        [
            "storage",
            "account",
            "list",
            "--subscription",
            subscription_id,
        ]
    )
    if not isinstance(raw, list):
        raise AppError("Azure CLI returned an invalid Storage Account list.")
    accounts: list[StorageAccount] = []
    for item in raw:
        endpoint = (item.get("primaryEndpoints") or {}).get("blob")
        if not endpoint:
            continue
        accounts.append(
            StorageAccount(
                name=str(item.get("name", "")),
                resource_group=str(item.get("resourceGroup", "")),
                location=str(item.get("primaryLocation", "")),
                resource_id=str(item.get("id", "")),
                endpoint=str(endpoint),
                hns_enabled=bool(item.get("isHnsEnabled", False)),
            )
        )
    accounts.sort(key=lambda account: account.name.lower())
    return accounts


def discover_snapshot_prefixes(
    subscription_id: str,
) -> dict[str, set[str]]:
    raw_ids = run_az(
        [
            "resource",
            "list",
            "--subscription",
            subscription_id,
            "--resource-type",
            "Microsoft.MachineLearningServices/workspaces",
            "--query",
            "[].id",
        ]
    )
    if not isinstance(raw_ids, list):
        raise AppError("Azure CLI returned an invalid AML workspace list.")

    def load(resource_id: str) -> object:
        return run_az(
            [
                "resource",
                "show",
                "--subscription",
                subscription_id,
                "--ids",
                resource_id,
                "--query",
                "{workspaceId:properties.workspaceId,"
                "storageAccount:properties.storageAccount}",
            ]
        )

    details: list[object] = []
    if raw_ids:
        with ThreadPoolExecutor(max_workers=min(8, len(raw_ids))) as executor:
            details = list(executor.map(load, [str(item) for item in raw_ids]))

    result: dict[str, set[str]] = defaultdict(set)
    for detail in details:
        if not isinstance(detail, dict):
            continue
        workspace_id = str(detail.get("workspaceId") or "").lower()
        storage_id = str(detail.get("storageAccount") or "")
        if workspace_id and storage_id:
            result[storage_id.rsplit("/", 1)[-1].lower()].add(workspace_id)
    return dict(result)


class TokenProvider:
    def __init__(self, subscription_id: str, resource: str) -> None:
        self.subscription_id = subscription_id
        self.resource = resource
        self._token = ""
        self._refresh_at = 0.0
        self._lock = threading.Lock()

    def get(self) -> str:
        with self._lock:
            if self._token and time.monotonic() < self._refresh_at:
                return self._token
            raw = run_az(
                [
                    "account",
                    "get-access-token",
                    "--subscription",
                    self.subscription_id,
                    "--resource",
                    self.resource,
                ]
            )
            if not isinstance(raw, dict) or not raw.get("accessToken"):
                raise AppError("Azure CLI did not return a Storage token.")
            self._token = str(raw["accessToken"])
            self._refresh_at = time.monotonic() + 45 * 60
            return self._token

    def invalidate(self) -> None:
        with self._lock:
            self._token = ""
            self._refresh_at = 0.0


class AzureCatalog:
    def __init__(self, settings: UserSettings | None = None) -> None:
        self.settings = settings or UserSettings()
        self.cloud = cloud_name()
        self.subscriptions = list_subscriptions()
        self.last_load_from_cache = False
        self.last_cache_age_seconds = 0.0

    def refresh_subscriptions(self) -> None:
        self.subscriptions = list_subscriptions()

    def selected_subscription_id(self) -> str | None:
        return self.settings.selected_subscription_id()

    def remember_subscription(self, subscription_id: str) -> None:
        self.settings.save_selected_subscription(subscription_id)

    def load_subscription(
        self,
        subscription: Subscription,
        force_refresh: bool = False,
    ) -> tuple[
        list[StorageAccount],
        dict[str, set[str]],
        TokenProvider,
    ]:
        cached = (
            None
            if force_refresh
            else self.settings.load_account_cache(subscription.id)
        )
        if cached is not None:
            accounts = cached.accounts
            prefixes = cached.snapshot_prefixes
            self.last_load_from_cache = True
            self.last_cache_age_seconds = cached.age_seconds
        else:
            accounts = list_storage_accounts(subscription.id)
            prefixes = discover_snapshot_prefixes(subscription.id)
            self.settings.save_account_cache(
                subscription.id,
                accounts,
                prefixes,
            )
            self.last_load_from_cache = False
            self.last_cache_age_seconds = 0.0
        token = TokenProvider(subscription.id, storage_resource(self.cloud))
        return accounts, prefixes, token
