import http.client
import sqlite3
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch

from azure_blob_tui.azure import StorageError
from azure_blob_tui.blob import (
    BlobRestClient,
    human_count,
    list_hierarchy_page,
    parse_blob_page,
)
from azure_blob_tui.models import ContainerUsage, StorageAccount
from azure_blob_tui.state import load_folder_usage, load_usage_state


class CoreTests(unittest.TestCase):
    def test_human_count(self):
        self.assertEqual("999", human_count(999))
        self.assertEqual("1.50K", human_count(1500))
        self.assertEqual("2.50M", human_count(2_500_000))
        self.assertEqual("1.42B", human_count(1_420_000_000))

    def test_parse_blob_page(self):
        root = ET.fromstring(
            """
            <EnumerationResults>
              <Blobs>
                <BlobPrefix><Name>a/</Name></BlobPrefix>
                <Blob>
                  <Name>root.bin</Name>
                  <Properties>
                    <Content-Length>42</Content-Length>
                    <BlobType>BlockBlob</BlobType>
                    <AccessTier>Hot</AccessTier>
                  </Properties>
                  <Metadata><owner>team</owner></Metadata>
                </Blob>
              </Blobs>
              <NextMarker>next</NextMarker>
            </EnumerationResults>
            """
        )
        items, marker = parse_blob_page(root)
        self.assertEqual("next", marker)
        self.assertTrue(items[0].is_prefix)
        self.assertEqual(42, items[1].size_bytes)
        self.assertEqual({"owner": "team"}, items[1].metadata)
        self.assertEqual("BlockBlob", items[1].properties["BlobType"])

    def test_delete_is_not_retried_after_ambiguous_response(self):
        class Token:
            def get(self):
                return "token"

        account = StorageAccount(
            "sa",
            "rg",
            "loc",
            "/id",
            "https://sa.example/",
            False,
        )
        client = BlobRestClient(Token(), max_attempts=6)
        with patch(
            "azure_blob_tui.blob.urlopen",
            side_effect=http.client.RemoteDisconnected("closed"),
        ) as request:
            with self.assertRaisesRegex(StorageError, "outcome is unknown"):
                client.delete_blob(account, "container", "file", '"etag"')
        self.assertEqual(1, request.call_count)

    def test_delete_uses_etag(self):
        class Token:
            def get(self):
                return "token"

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def read(self):
                return b""

        account = StorageAccount(
            "sa",
            "rg",
            "loc",
            "/id",
            "https://sa.example/",
            False,
        )
        client = BlobRestClient(Token())
        with patch(
            "azure_blob_tui.blob.urlopen",
            return_value=Response(),
        ) as urlopen:
            client.delete_blob(account, "container", "a/file", '"etag"')
        request = urlopen.call_args.args[0]
        self.assertEqual("DELETE", request.method)
        self.assertEqual('"etag"', request.headers["If-match"])
        self.assertIn("/container/a/file", request.full_url)

    def test_short_service_pages_are_coalesced(self):
        roots = {
            "": ET.fromstring(
                """
                <EnumerationResults>
                  <Blobs><BlobPrefix><Name>a/</Name></BlobPrefix></Blobs>
                  <NextMarker>m1</NextMarker>
                </EnumerationResults>
                """
            ),
            "m1": ET.fromstring(
                """
                <EnumerationResults>
                  <Blobs><BlobPrefix><Name>b/</Name></BlobPrefix></Blobs>
                  <NextMarker>m2</NextMarker>
                </EnumerationResults>
                """
            ),
            "m2": ET.fromstring(
                """
                <EnumerationResults>
                  <Blobs><BlobPrefix><Name>c/</Name></BlobPrefix></Blobs>
                  <NextMarker></NextMarker>
                </EnumerationResults>
                """
            ),
        }

        class Client:
            def get_xml(self, account, container, parameters):
                return roots[str(parameters.get("marker", ""))]

        account = StorageAccount(
            "sa",
            "rg",
            "loc",
            "/id",
            "https://sa.example/",
            False,
        )
        items, marker, pages = list_hierarchy_page(
            Client(),
            account,
            "container",
            "",
            "",
            500,
        )
        self.assertEqual(["a/", "b/", "c/"], [item.name for item in items])
        self.assertEqual("", marker)
        self.assertEqual(3, pages)

    def test_load_usage_state(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite"
            connection = sqlite3.connect(path)
            connection.executescript(
                """
                CREATE TABLE containers (
                    account TEXT,
                    container TEXT,
                    completed INTEGER,
                    page_count INTEGER,
                    blob_count INTEGER,
                    size_bytes INTEGER
                );
                CREATE TABLE folder_usage (
                    account TEXT,
                    container TEXT,
                    depth INTEGER,
                    folder TEXT,
                    blob_count INTEGER,
                    size_bytes INTEGER
                );
                """
            )
            connection.execute(
                "INSERT INTO containers VALUES ('sa','c',1,2,3,4)"
            )
            connection.execute(
                "INSERT INTO folder_usage VALUES ('sa','c',2,'a/b',5,6)"
            )
            connection.commit()
            connection.close()
            self.assertEqual(
                ContainerUsage(True, 2, 3, 4),
                load_usage_state(path, "sa")["c"],
            )
            folder = load_folder_usage(path, "sa", "c")[(2, "a/b")]
            self.assertEqual((2, 5, 6), (
                folder.depth,
                folder.blob_count,
                folder.size_bytes,
            ))


if __name__ == "__main__":
    unittest.main()
