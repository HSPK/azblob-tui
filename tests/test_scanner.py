import csv
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from azure_blob_tui.models import StorageAccount, Subscription
from azure_blob_tui.scanner import ScanOptions, parse_selection, run_scan


class FakeClient:
    def get_xml(self, account, path, parameters):
        if path == "":
            return ET.fromstring(
                """
                <EnumerationResults>
                  <Containers><Container><Name>container</Name></Container></Containers>
                  <NextMarker></NextMarker>
                </EnumerationResults>
                """
            )
        marker = parameters.get("marker", "")
        if not marker:
            return ET.fromstring(
                """
                <EnumerationResults>
                  <Blobs>
                    <Blob>
                      <Name>a/b/file.bin</Name>
                      <Properties><Content-Length>10</Content-Length></Properties>
                    </Blob>
                  </Blobs>
                  <NextMarker>next</NextMarker>
                </EnumerationResults>
                """
            )
        return ET.fromstring(
            """
            <EnumerationResults>
              <Blobs>
                <Blob>
                  <Name>root.bin</Name>
                  <Properties><Content-Length>5</Content-Length></Properties>
                </Blob>
              </Blobs>
              <NextMarker></NextMarker>
            </EnumerationResults>
            """
        )


class ScannerTests(unittest.TestCase):
    def test_parse_selection(self):
        self.assertEqual([0, 1, 2, 4], parse_selection("1-3,5,2", 5))

    def test_checkpoint_scan_and_export(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            account = StorageAccount(
                "sa",
                "rg",
                "loc",
                "/id",
                "https://sa.example/",
                False,
            )
            options = ScanOptions(
                subscription=Subscription("sub", "Sub", "tenant", True),
                accounts=[account],
                workspace_ids={},
                state_path=root / "state.sqlite",
                output_path=root / "depth1.csv",
                failure_path=root / "errors.csv",
                log_path=root / "scan.log",
                export_depth=1,
                max_depth=3,
                workers=2,
                checkpoint_pages=1,
                excluded_containers=set(),
                exclude_aml_snapshots=True,
            )
            self.assertEqual(0, run_scan(FakeClient(), options))
            with options.output_path.open(newline="", encoding="utf-8") as source:
                rows = list(csv.DictReader(source))
            self.assertEqual(
                {"a": 10, "_root": 5},
                {row["folder"]: int(row["size_bytes"]) for row in rows},
            )
            self.assertIn("Progress: containers 1/1", options.log_path.read_text())


if __name__ == "__main__":
    unittest.main()
