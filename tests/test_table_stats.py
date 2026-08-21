import sqlite3
import tempfile
import unittest
from pathlib import Path

from azure_blob_tui.stats import StatsProvider
from azure_blob_tui.table import Column, fit_columns, render_row


class TableStatsTests(unittest.TestCase):
    def test_table_hides_low_priority_columns(self):
        columns = [
            Column("name", "Name", 10, 20, flex=1),
            Column("size", "Size", 8, 10, align="right", hide_priority=1),
            Column("time", "Time", 12, 20, hide_priority=10),
        ]
        fitted = fit_columns(columns, 25)
        self.assertEqual(["name", "size"], [x.column.key for x in fitted])
        self.assertLessEqual(len(render_row(fitted, {"name": "x", "size": 1})), 25)

    def test_table_respects_max_width(self):
        columns = [
            Column("name", "Name", 10, 20, flex=1, max_width=30),
            Column("size", "Size", 8, 8),
        ]
        fitted = fit_columns(columns, 100)
        self.assertEqual(30, fitted[0].width)

    def test_stats_provider(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state.sqlite"
            log = Path(directory) / "scan.log"
            connection = sqlite3.connect(state)
            connection.executescript(
                """
                CREATE TABLE metadata (key TEXT, value TEXT);
                CREATE TABLE containers (
                    account TEXT,
                    completed INTEGER,
                    page_count INTEGER,
                    blob_count INTEGER,
                    size_bytes INTEGER
                );
                INSERT INTO metadata VALUES ('status', 'running');
                INSERT INTO containers VALUES ('sa', 1, 2, 3, 4);
                INSERT INTO containers VALUES ('sa', 0, 1, 5, 6);
                INSERT INTO containers VALUES ('sb', 0, 0, 0, 0);
                """
            )
            connection.commit()
            connection.close()
            log.write_text(
                "Progress: containers 2/3 ( 66.7%, active 1, failed 1) "
                "| blobs 8 | pages 3 | size 10.00 B "
                "| rate 2 blobs/s | elapsed 00:00:04\n"
            )
            stats = StatsProvider(state, log).load()
            self.assertEqual((3, 1, 1, 1), (
                stats.total,
                stats.completed,
                stats.partial,
                stats.pending_or_failed,
            ))
            self.assertEqual(1, stats.reported_active)
            self.assertEqual(8, stats.reported_blob_count)
            self.assertEqual(3, stats.reported_page_count)
            self.assertEqual("10.00 B", stats.reported_size)


if __name__ == "__main__":
    unittest.main()
