import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from azure_blob_tui.models import (
    BlobItem,
    ContainerItem,
    ContainerUsage,
    FolderUsage,
    StorageAccount,
    Subscription,
)
from azure_blob_tui.settings import UserSettings
from azure_blob_tui.stats import AccountQueueStats
from azure_blob_tui.ui import BlobBrowser


class FakeCatalog:
    subscriptions = []


class FakeClient:
    def __init__(self):
        self.deleted_blobs = []
        self.deleted_containers = []
        self.restored_blobs = []
        self.restored_containers = []

    def delete_blob(self, account, container, name, etag=""):
        self.deleted_blobs.append((account.name, container, name, etag))

    def delete_container(self, account, container):
        self.deleted_containers.append((account.name, container))

    def restore_blob(
        self,
        account,
        container,
        name,
        deletion_id="",
    ):
        self.restored_blobs.append(
            (account.name, container, name, deletion_id)
        )

    def restore_container(self, account, container, version):
        self.restored_containers.append(
            (account.name, container, version)
        )


class FakePromptWindow:
    def __init__(self):
        self.delays = []

    def getmaxyx(self):
        return 24, 80

    def addstr(self, *_):
        pass

    def refresh(self):
        pass

    def timeout(self, delay):
        self.delays.append(delay)

    def getstr(self, *_):
        return b"long-container-name"


class UiTests(unittest.TestCase):
    def test_container_sorting(self):
        app = BlobBrowser(
            catalog=FakeCatalog(),
            state_path=None,
            stats_provider=None,
            page_size=100,
            show_snapshots=False,
            initial_subscription=None,
            initial_account=None,
        )
        app.screen = "containers"
        app.account = StorageAccount(
            "sa",
            "rg",
            "loc",
            "/id",
            "https://sa.example/",
            False,
        )
        app.container_items = [
            ContainerItem("small"),
            ContainerItem("large"),
        ]
        app.container_usage = {
            "small": ContainerUsage(True, 1, 1, 10),
            "large": ContainerUsage(True, 1, 2, 100),
        }
        app.sort_keys["containers"] = "size"
        app.sort_descending["containers"] = True
        self.assertEqual(
            ["large", "small"],
            [item.name for item in app._visible_rows()],
        )

    def test_sort_marker_and_blob_metadata(self):
        app = BlobBrowser(
            catalog=FakeCatalog(),
            state_path=None,
            stats_provider=None,
            page_size=100,
            show_snapshots=False,
            initial_subscription=None,
            initial_account=None,
        )
        app.screen = "blobs"
        app.account = StorageAccount(
            "sa",
            "rg",
            "loc",
            "/id",
            "https://sa.example/",
            False,
        )
        app.container = "container"
        app.sort_keys["blobs"] = "size"
        app.sort_descending["blobs"] = True
        labels = {column.key: column.label for column in app._columns()}
        self.assertEqual("Size [D]", labels["size"])
        lines = app._detail_lines(
            BlobItem(
                name="file.bin",
                is_prefix=False,
                metadata={"owner": "team"},
            )
        )
        self.assertIn("Metadata:", lines)
        self.assertIn("  owner=team", lines)

    def test_live_account_navigation_inherits_size_sort(self):
        app = BlobBrowser(
            catalog=FakeCatalog(),
            state_path=None,
            stats_provider=None,
            page_size=100,
            show_snapshots=False,
            initial_subscription=None,
            initial_account=None,
        )
        account = StorageAccount(
            "sa",
            "rg",
            "loc",
            "/id",
            "https://sa.example/",
            False,
        )
        app.accounts = [account]
        app.screen = "stats"
        app.sort_keys["stats"] = "size"
        app.sort_descending["stats"] = True
        app._load_containers = lambda: None
        app._open(AccountQueueStats("sa", 2, 1, 0, 1, 10, 100, 3))

        self.assertEqual("containers", app.screen)
        self.assertEqual(account, app.account)
        self.assertEqual("stats", app.containers_return_screen)
        self.assertEqual("size", app.sort_keys["containers"])
        self.assertTrue(app.sort_descending["containers"])
        labels = {column.key: column.label for column in app._columns()}
        self.assertEqual("Size [D]", labels["size"])

        app._load_blob_page = lambda: None
        app._open(ContainerItem("container"))
        self.assertEqual("blobs", app.screen)
        self.assertEqual("size", app.sort_keys["blobs"])
        self.assertTrue(app.sort_descending["blobs"])
        app._go_back()
        app._go_back()
        self.assertEqual("stats", app.screen)

    def test_tui_resolves_subscription_scan_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = UserSettings(
                config_dir=root / "config",
                cache_dir=root / "cache",
                state_dir=root / "state",
            )
            subscription = Subscription(
                "sub",
                "Subscription",
                "tenant",
                True,
            )
            paths = settings.default_scan_paths(subscription.id)
            paths.state.parent.mkdir(parents=True)
            paths.state.touch()
            paths.log.touch()
            catalog = FakeCatalog()
            catalog.settings = settings
            app = BlobBrowser(
                catalog=catalog,
                state_path=None,
                stats_provider=None,
                page_size=100,
                show_snapshots=False,
                initial_subscription=subscription,
                initial_account=None,
                scan_path_working_directory=root / "elsewhere",
            )
            app.subscription = subscription
            app._configure_scan_paths()
            self.assertEqual(paths.state, app.state_path)
            self.assertIsNotNone(app.stats_provider)

    def test_folder_aggregate_and_top_level_back(self):
        app = BlobBrowser(
            catalog=FakeCatalog(),
            state_path=None,
            stats_provider=None,
            page_size=100,
            show_snapshots=False,
            initial_subscription=None,
            initial_account=None,
        )
        app.screen = "blobs"
        app.folder_usage = {
            (2, "a/b"): FolderUsage(2, 1_500_000, 1024),
            (3, "a/b"): FolderUsage(3, 500_000, 512),
        }
        app.container = "container"
        app.container_usage = {
            "container": ContainerUsage(True, 1, 1_500_000, 1024),
        }
        app.prefix = "a/b/"
        folder = BlobItem(name="a/b/", is_prefix=True)
        values = app._row_values(folder)
        self.assertEqual("1.50M", values["blobs"])
        self.assertEqual("1.00 KiB", values["size"])
        status = app._scope_status(2, 1)
        self.assertIn("Total 1.00 KiB / 1.50M Blobs", status)
        self.assertIn("Direct files 512.00 B / 500.0K", status)

        app.screen = "subscriptions"
        app._go_back()
        self.assertIn("Ctrl+C", app.status)

        app.screen = "containers"
        app._handle_key(ord("q"))
        self.assertEqual("accounts", app.screen)
        app.subscription = Subscription(
            "sub",
            "Subscription",
            "tenant",
            True,
        )
        app._handle_key(ord("q"))
        self.assertEqual("accounts", app.screen)
        self.assertIn("press S", app.status)
        app._handle_key(ord("S"))
        self.assertEqual("subscriptions", app.screen)
        app._handle_key(ord("q"))
        self.assertEqual("accounts", app.screen)

    def test_guarded_deletion(self):
        app = BlobBrowser(
            catalog=FakeCatalog(),
            state_path=None,
            stats_provider=None,
            page_size=100,
            show_snapshots=False,
            initial_subscription=None,
            initial_account=None,
        )
        app.client = FakeClient()
        app.account = StorageAccount(
            "sa",
            "rg",
            "loc",
            "/id",
            "https://sa.example/",
            False,
        )
        app.screen = "containers"
        container = ContainerItem("container")
        app.container_items = [container]
        app._modal = lambda *_: None
        app._prompt = lambda *_: "container"
        app._delete_container(container)
        self.assertEqual([("sa", "container")], app.client.deleted_containers)

        app.screen = "blobs"
        app.container = "other"
        blob = BlobItem(
            name="file",
            is_prefix=False,
            etag='"etag"',
        )
        app.blob_items = [blob]
        app._prompt = lambda *_: "DELETE"
        app._delete_blob(blob)
        self.assertEqual(
            [("sa", "other", "file", '"etag"')],
            app.client.deleted_blobs,
        )

    def test_soft_deleted_items_are_read_only_and_identified(self):
        app = BlobBrowser(
            catalog=FakeCatalog(),
            state_path=None,
            stats_provider=None,
            page_size=100,
            show_snapshots=False,
            initial_subscription=None,
            initial_account=None,
        )
        container = ContainerItem(
            "deleted",
            is_deleted=True,
            version="version",
            deleted_time="today",
            remaining_retention_days=6,
        )
        app.account = StorageAccount(
            "sa",
            "rg",
            "loc",
            "/id",
            "https://sa.example/",
            False,
        )
        app.container_items = [container]
        app.screen = "containers"
        app.show_deleted = True
        self.assertEqual("deleted", app._row_values(container)["type"])
        self.assertEqual("6d", app._row_values(container)["retention"])
        self.assertIn("Soft deleted: yes", app._detail_lines(container))
        app._delete_selected(container)
        self.assertIn("cannot be deleted again", app.status)
        app.client = FakeClient()
        app._modal = lambda *_: None
        app._prompt = lambda *_: "deleted"
        app._load_containers = lambda: None
        app._restore_selected(container)
        self.assertEqual(
            [("sa", "deleted", "version")],
            app.client.restored_containers,
        )

        blob = BlobItem(
            "deleted.bin",
            is_prefix=False,
            is_deleted=True,
            deletion_id="deletion",
            remaining_retention_days=5,
        )
        app.screen = "blobs"
        app.container = "container"
        self.assertEqual("DELETED", app._row_values(blob)["kind"])
        self.assertEqual("5d", app._row_values(blob)["retention"])
        app._delete_selected(blob)
        self.assertIn("cannot be deleted again", app.status)
        app._prompt = lambda *_: "RESTORE"
        app._load_blob_page = lambda: None
        app._restore_selected(blob)
        self.assertEqual(
            [("sa", "container", "deleted.bin", "deletion")],
            app.client.restored_blobs,
        )

    def test_prompt_disables_refresh_timeout_while_typing(self):
        app = BlobBrowser(
            catalog=FakeCatalog(),
            state_path=None,
            stats_provider=None,
            page_size=100,
            show_snapshots=False,
            initial_subscription=None,
            initial_account=None,
        )
        window = FakePromptWindow()
        app.window = window
        with (
            patch("azure_blob_tui.ui.curses.echo"),
            patch("azure_blob_tui.ui.curses.noecho"),
            patch("azure_blob_tui.ui.curses.curs_set"),
        ):
            value = app._prompt("Confirm")
        self.assertEqual("long-container-name", value)
        self.assertEqual([-1, 1000], window.delays)

if __name__ == "__main__":
    unittest.main()
