"""Metadata fixtures/mocked attributes only: no real disk-wide scan or Windows UI."""
import datetime as dt
import io
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import storage_audit as audit


class Entry:
    def __init__(self, path, hint):
        self.path = str(path)
        self.hint = hint

    def stat(self, *, follow_symlinks):
        assert follow_symlinks is False
        if isinstance(self.hint, Exception):
            raise self.hint
        return self.hint


class Entries:
    def __init__(self, entries):
        self.entries = iter(entries)
        self.closed = False

    def __next__(self):
        value = next(self.entries)
        if isinstance(value, Exception):
            raise value
        return value

    def close(self):
        self.closed = True


def metadata(size=0, *, mode=stat.S_IFREG, ino=1, links=1, attrs=0):
    return SimpleNamespace(st_mode=mode, st_size=size, st_dev=9, st_ino=ino,
                           st_nlink=links, st_file_attributes=attrs, st_mtime=1., st_atime=2.)


class StorageAuditTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="storage-audit-fixture-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / "input"
        self.root.mkdir()
        self.output = self.base / "reports"

    def subject(self, **kwargs):
        return audit.StorageAudit([self.root], self.output, **kwargs)

    def test_01_constructor_inert_and_paths_bounded(self):
        subject = self.subject()
        self.assertFalse(self.output.exists())
        self.assertEqual(subject.top_count, 200)
        for roots, output in (([], self.output), ([self.root, self.root / "nested"], self.output),
                              (["relative"], self.output), ([self.root], self.root),
                              ([self.root], "//server/share/report")):
            with self.assertRaises(ValueError):
                audit.StorageAudit(roots, output)
        for key, value in (("top_count", 201), ("aggregate_depth", 5), ("progress_interval", float("nan"))):
            with self.assertRaises(ValueError):
                self.subject(**{key: value})

    def test_02_real_metadata_aggregates_and_readonly_sources(self):
        folder = self.root / "a" / "b" / "c" / "deeper"
        folder.mkdir(parents=True)
        video = folder / "film.mp4"
        video.write_bytes(b"v" * 17)
        package = self.root / "package.msi"
        package.write_bytes(b"i" * 3)
        before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in (video, package)}
        real_open = open

        def guarded_open(path, mode="r", *args, **kwargs):
            self.assertFalse(audit._within(Path(path), self.root), "input contents must never be opened")
            return real_open(path, mode, *args, **kwargs)

        with mock.patch("builtins.open", guarded_open):
            report = self.subject().run()
        self.assertEqual(report["counters"]["files"], 2)
        self.assertEqual(report["counters"]["logical_bytes_raw"], 20)
        self.assertEqual(report["categories"]["video"]["logical_bytes_raw"], 17)
        self.assertEqual(report["categories"]["installer_package"]["files"], 1)
        self.assertEqual(len(report["folder_aggregates"]), 3)
        self.assertEqual(report["largest_files"][0]["modified_utc"][-6:], "+00:00")
        self.assertFalse(report["all_contents_visible"])
        self.assertTrue(report["traversal_finished"])
        for path, expected in before.items():
            self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), expected)
        self.assertEqual(json.loads((self.output / "report.json").read_text())["counters"]["files"], 2)

    def test_03_real_hardlinks_count_once_without_hashes(self):
        first = self.root / "first.bin"
        first.write_bytes(b"hardlink fixture")
        second = self.root / "second.bin"
        try:
            os.link(first, second)
        except OSError as error:
            self.skipTest("Fixture filesystem does not support hardlinks: " + type(error).__name__)
        report = self.subject().run()
        size = len(b"hardlink fixture")
        self.assertEqual(report["counters"]["logical_bytes_raw"], size * 2)
        self.assertEqual(report["counters"]["logical_bytes_unique_known"], size)
        self.assertEqual(report["counters"]["hardlink_duplicate_paths"], 1)
        self.assertTrue(report["unique_size_complete_for_observed_files"])
        self.assertTrue(first.exists() and second.exists())

    def test_04_cached_zero_identity_not_used_for_size_or_hardlinks(self):
        paths = [self.root / "a.bin", self.root / "b.bin"]
        hint = metadata(size=999, ino=0, links=0)
        actual = metadata(size=42, ino=44, links=2)
        calls = []

        def fresh(path, *, follow_symlinks):
            self.assertFalse(follow_symlinks)
            calls.append(Path(path))
            return actual if Path(path) in paths else metadata(mode=stat.S_IFDIR)

        providers = {"stat": fresh, "scandir": lambda _: Entries([Entry(p, hint) for p in paths])}
        report = self.subject(providers=providers).run()
        self.assertTrue(all(path in calls for path in paths))
        self.assertEqual(report["counters"]["logical_bytes_raw"], 84)
        self.assertEqual(report["counters"]["logical_bytes_unique_known"], 42)

    def test_05_hazard_enumeration_flags_skipped_before_fresh_stat(self):
        hints = [metadata(attrs=a) for a in (audit.REPARSE, audit.OFFLINE, audit.RECALL_ON_OPEN, audit.RECALL_ON_DATA_ACCESS)]
        hints.append(metadata(mode=stat.S_IFLNK))
        paths = [self.root / str(n) for n in range(len(hints))]

        def fresh(path, *, follow_symlinks):
            self.assertNotIn(Path(path), paths, "cloud/reparse entry must not be fresh-statted")
            return metadata(mode=stat.S_IFDIR)

        report = self.subject(providers={"stat": fresh, "scandir": lambda _: Entries([Entry(p, h) for p, h in zip(paths, hints)])}).run()
        self.assertEqual(report["status"], "partial")
        self.assertEqual(report["counters"]["skipped_entries"], 5)
        self.assertEqual(report["counters"]["skipped_cloud_or_offline"], 3)
        self.assertEqual(report["counters"]["files"], 0)

    def test_06_errors_continue_other_entries_and_roots(self):
        other = self.base / "other"
        other.mkdir()
        first_iterator = Entries([Entry(self.root / "denied", PermissionError(13, "private text not exported")),
                                  Entry(self.root / "good", metadata()), OSError(5, "iterator failure")])
        second_iterator = Entries([Entry(other / "also-good", metadata())])

        def fresh(path, *, follow_symlinks):
            return metadata(mode=stat.S_IFDIR) if Path(path) not in (self.root / "good", other / "also-good") else metadata(size=2)

        report = audit.StorageAudit([self.root, other], self.output, providers={"stat": fresh, "scandir": lambda p: first_iterator if p == self.root else second_iterator}).run()
        self.assertEqual(report["counters"]["files"], 2)
        self.assertEqual(report["counters"]["errors"], 2)
        self.assertEqual(report["counters"]["permission_denied"], 1)
        self.assertEqual(report["status"], "partial")
        self.assertNotIn("private text", json.dumps(report))
        self.assertTrue(first_iterator.closed and second_iterator.closed)

    def test_07_top200_and_small_report_budget_do_not_stop_scan(self):
        for index in range(230):
            (self.root / ("file-" + str(index) + ".bin")).write_bytes(b"x" * index)
        report = self.subject().run()
        self.assertEqual(report["counters"]["files"], 230)
        self.assertEqual(len(report["largest_files"]), 200)
        self.assertEqual(report["output_limits"]["file_names_omitted_from_top"], 30)
        self.assertEqual(report["largest_files"][0]["logical_bytes"], 229)
        with mock.patch.object(audit, "MAX_REPORT_BYTES", 10000):
            reduced = self.subject().run()
        self.assertEqual(reduced["counters"]["files"], 230)
        self.assertTrue(reduced["output_limits"]["budget_truncated"])
        self.assertLessEqual(self.subject().report_path.stat().st_size, 10000)

    def test_08_identity_and_aggregate_caps_continue_and_report_uncertainty(self):
        for name in ("a", "b", "c"):
            (self.root / name).mkdir()
            (self.root / name / "one").write_bytes(b"x" * 5)
        real_stat = os.stat

        def uncertain(path, *, follow_symlinks):
            value = real_stat(path, follow_symlinks=follow_symlinks)
            return metadata(size=value.st_size, ino=0, links=0) if stat.S_ISREG(value.st_mode) else value

        with mock.patch.object(audit, "MAX_AGGREGATES", 1):
            report = self.subject(providers={"stat": uncertain}).run()
        self.assertEqual(report["counters"]["files"], 3)
        self.assertEqual(report["counters"]["unknown_identity_files"], 3)
        self.assertEqual(report["counters"]["logical_bytes_unique_known"], 0)
        self.assertEqual(report["logical_bytes_unique_upper_bound"], 15)
        self.assertEqual(report["counters"]["aggregate_buckets_omitted"], 2)
        self.assertFalse(report["unique_size_complete_for_observed_files"])
        self.assertEqual(report["status"], "partial")

    def test_09_progress_periodic_and_output_tree_excluded(self):
        output = self.root / "audit-output"
        for index in range(4):
            (self.root / str(index)).write_bytes(b"x")
        ticks = iter(range(0, 1000, 3))
        writes = []
        original = audit._atomic_json

        def record(path, value):
            if path.name == "progress.json":
                writes.append(dict(value))
            original(path, value)

        with mock.patch.object(audit, "_atomic_json", record):
            report = audit.StorageAudit([self.root], output, providers={"monotonic": lambda: next(ticks), "utc_now": lambda: dt.datetime(2026, 11, 18, tzinfo=dt.timezone.utc)}).run()
        self.assertGreaterEqual(len(writes), 3)
        self.assertEqual(writes[0]["status"], "running")
        self.assertEqual(writes[-1]["status"], report["status"])
        self.assertEqual(report["counters"]["files"], 4)
        self.assertEqual(report["counters"]["excluded_output_entries"], 1)
        self.assertTrue(all(row["updated_utc"].endswith("+00:00") for row in writes))

    def test_10_cli_priority_mock_and_interrupt_output_link_failclosed(self):
        fake = {"report_path": "fixture", "status": "completed", "traversal_finished": True, "counters": {}}
        with mock.patch.object(audit.StorageAudit, "run", return_value=fake), mock.patch.object(audit, "_below_normal_priority", return_value="below_normal") as priority, mock.patch("sys.stdout", new=io.StringIO()):
            self.assertEqual(audit.main(["--root", str(self.root), "--output", str(self.output)]), 0)
            priority.assert_called_once_with()
        iterator = Entries([KeyboardInterrupt()])
        with mock.patch.object(Entries, "__next__", side_effect=KeyboardInterrupt):
            report = self.subject(providers={"scandir": lambda _: iterator}).run()
        self.assertEqual(report["status"], "interrupted")
        self.assertFalse(report["traversal_finished"])
        self.assertTrue(iterator.closed)
        victim = self.base / "unrelated"
        victim.write_bytes(b"unchanged")
        linked_output = self.base / "linked-report"
        linked_output.mkdir()
        try:
            os.link(victim, linked_output / "report.json")
        except OSError:
            return
        with self.assertRaises(ValueError):
            audit.StorageAudit([self.root], linked_output).run()
        self.assertEqual(victim.read_bytes(), b"unchanged")


    @unittest.skipUnless(os.name == "nt", "Windows native attribute contracts")
    def test_11_native_reparse_flags_stop_before_python_stat(self):
        with mock.patch.object(audit, "_native_attributes", return_value=audit.REPARSE | 0x10), mock.patch.object(audit.os, "stat") as raw:
            value = audit._native_stat(r"C:\fixture-cloud", follow_symlinks=False)
        raw.assert_not_called()
        self.assertTrue(audit._unsafe(value))

    @unittest.skipUnless(os.name == "nt", "Windows extended path contracts")
    def test_12_extended_paths_used_for_stat_and_scandir_with_normal_display(self):
        path = "C:\\fixture\\" + "long\\" * 70 + "file.txt"
        native = "\\\\?\\" + path
        with mock.patch.object(audit, "_native_attributes", return_value=0), mock.patch.object(audit.os, "stat", return_value=metadata()) as raw:
            audit._native_stat(path, follow_symlinks=False)
        raw.assert_called_once_with(native, follow_symlinks=False)
        with mock.patch.object(audit.os, "scandir") as scan:
            audit._native_scandir(path)
        scan.assert_called_once_with(native)
        self.assertEqual(audit._display_path(native), Path(path))
        self.assertEqual(audit._native_path(native), native)

    @unittest.skipUnless(os.name == "nt", "Windows cloud ancestry contracts")
    def test_13_cloud_ancestor_guard_uses_native_flags_even_if_python_masks_them(self):
        path = Path(r"C:\fixture-cloud\reports")
        cloud = Path(r"C:\fixture-cloud")
        def attrs(value):
            return audit.REPARSE | 0x10 if Path(value) == cloud else 0x10
        with mock.patch.object(audit, "_native_attributes", side_effect=attrs), mock.patch.object(audit.os, "stat", return_value=metadata(mode=stat.S_IFDIR)) as raw:
            with self.assertRaises(ValueError):
                audit._safe_output(path)
        self.assertTrue(all(audit._display_path(call.args[0]) != cloud for call in raw.call_args_list))


    def test_14_configured_sync_root_excluded_when_all_attributes_are_masked(self):
        cloud = self.root / "cloud"
        cloud.mkdir()
        (cloud / "unread.bin").write_bytes(b"x")
        with mock.patch.object(audit, "_configured_cloud_roots", return_value=(cloud,)):
            report = self.subject().run()
            with self.assertRaises(ValueError):
                audit._safe_output(cloud / "reports")
        self.assertEqual(report["counters"]["files"], 0)
        self.assertEqual(report["counters"]["skipped_configured_cloud_roots"], 1)
        self.assertEqual(report["status"], "partial")


if __name__ == "__main__":
    unittest.main()
