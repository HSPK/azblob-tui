import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from azure_blob_tui.azure import AzureCatalog
from azure_blob_tui.models import StorageAccount, Subscription
from azure_blob_tui.settings import UserSettings


class SettingsTests(unittest.TestCase):
    def test_subscription_and_account_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = UserSettings(
                config_dir=root / "config",
                cache_dir=root / "cache",
                cache_ttl_seconds=100,
            )
            settings.save_selected_subscription("sub")
            self.assertEqual("sub", settings.selected_subscription_id())

            account = StorageAccount(
                "sa",
                "rg",
                "loc",
                "/id",
                "https://sa.example/",
                False,
            )
            settings.save_account_cache(
                "sub",
                [account],
                {"sa": {"workspace"}},
                now=1000,
            )
            cache = settings.load_account_cache("sub", now=1050)
            self.assertIsNotNone(cache)
            self.assertEqual([account], cache.accounts)
            self.assertEqual({"workspace"}, cache.snapshot_prefixes["sa"])
            self.assertIsNone(
                settings.load_account_cache("sub", now=1200)
            )

    def test_catalog_uses_cache_until_forced(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = UserSettings(
                config_dir=root / "config",
                cache_dir=root / "cache",
            )
            subscription = Subscription(
                "sub",
                "Subscription",
                "tenant",
                True,
            )
            account = StorageAccount(
                "sa",
                "rg",
                "loc",
                "/id",
                "https://sa.example/",
                False,
            )
            with (
                patch(
                    "azure_blob_tui.azure.cloud_name",
                    return_value="AzureCloud",
                ),
                patch(
                    "azure_blob_tui.azure.list_subscriptions",
                    return_value=[subscription],
                ),
                patch(
                    "azure_blob_tui.azure.list_storage_accounts",
                    return_value=[account],
                ) as list_accounts,
                patch(
                    "azure_blob_tui.azure.discover_snapshot_prefixes",
                    return_value={"sa": {"workspace"}},
                ) as list_prefixes,
            ):
                catalog = AzureCatalog(settings)
                catalog.load_subscription(subscription)
                self.assertFalse(catalog.last_load_from_cache)
                catalog.load_subscription(subscription)
                self.assertTrue(catalog.last_load_from_cache)
                catalog.load_subscription(subscription, force_refresh=True)
                self.assertFalse(catalog.last_load_from_cache)
                self.assertEqual(2, list_accounts.call_count)
                self.assertEqual(2, list_prefixes.call_count)


if __name__ == "__main__":
    unittest.main()
