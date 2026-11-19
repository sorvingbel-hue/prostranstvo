"""Local metadata-only storage audit. Importing/constructing never starts a scan.

CLI roots are explicit. Only the report directory is written; scanned files are
never opened for contents, hashed, changed or deleted. Reports contain private
local paths and must not be automatically uploaded. Sizes are logical, not
allocated, physical, reclaimable or a deletion recommendation.
"""
from __future__ import annotations

import argparse
import datetime as dt
import heapq
import json
import math
import os
from pathlib import Path
import stat
import tempfile
import time

REPARSE = 0x400
OFFLINE = 0x1000
RECALL_ON_OPEN = 0x40000
RECALL_ON_DATA_ACCESS = 0x400000
UNSAFE_ATTRIBUTES = REPARSE | OFFLINE | RECALL_ON_OPEN | RECALL_ON_DATA_ACCESS
MAX_REPORT_BYTES = 8 * 1024 * 1024
MAX_AGGREGATES = 50000
MAX_AGGREGATE_ROWS = 1000
MAX_HARDLINK_IDENTITIES = 1000000
MAX_ERROR_SAMPLES = 100
MAX_PATH_CHARS = 4096
MAX_DEPTH = 512

_EXTENSIONS = {
    "video": ".mp4 .mkv .avi .mov .webm .m4v .m2ts .wmv .mpeg .mpg",
    "archive_or_disk_image": ".zip .7z .rar .tar .gz .bz2 .xz .iso .vhd .vhdx",
    "installer_package": ".msi .msp .msix .msixbundle .appx .appxbundle .cab",
    "executable_or_library": ".exe .dll .sys .com",
    "source_or_project_metadata": ".py .js .mjs .cjs .ts .tsx .jsx .c .cpp .h .hpp .cs .java .rs .go .sln .csproj .vcxproj .uproject .godot",
    "document": ".pdf .doc .docx .xls .xlsx .ppt .pptx .odt .txt .md",
    "image_or_audio": ".jpg .jpeg .png .gif .webp .tif .tiff .bmp .mp3 .wav .flac .ogg .m4a",
}
_CATEGORY = {ext: name for name, extensions in _EXTENSIONS.items() for ext in extensions.split()}


def _utc_now():
    return dt.datetime.now(dt.timezone.utc)


def _iso(value):
    if not isinstance(value, dt.datetime):
        raise ValueError("utc_now must return a datetime")
    if value.tzinfo is None:
        raise ValueError("utc_now must return an aware datetime")
    return value.astimezone(dt.timezone.utc).isoformat()


def _timestamp(value):
    try:
        if not math.isfinite(value):
            return None
        return dt.datetime.fromtimestamp(value, dt.timezone.utc).isoformat()
    except (ValueError, OverflowError, OSError, TypeError):
        return None


def _local_absolute(value):
    raw = os.fspath(value)
    if not isinstance(raw, str) or not raw or any(ord(c) < 32 for c in raw):
        raise ValueError("An explicit local absolute path is required")
    if raw.startswith(("\\\\", "//")) or "://" in raw:
        raise ValueError("Network/device paths are not accepted")
    path = Path(raw)
    if not path.is_absolute():
        raise ValueError("An absolute path is required")
    return Path(os.path.abspath(path))  # lexical: never resolve junctions


def _within(path, parent):
    try:
        child_key = os.path.normcase(os.path.abspath(path))
        parent_key = os.path.normcase(os.path.abspath(parent))
        return os.path.commonpath((child_key, parent_key)) == parent_key
    except ValueError:
        return False


def _attributes(meta):
    return int(getattr(meta, "st_file_attributes", 0))


def _unsafe(meta):
    return stat.S_ISLNK(meta.st_mode) or bool(_attributes(meta) & UNSAFE_ATTRIBUTES)


def _configured_cloud_roots():
    roots = []
    for key in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial"):
        value = os.environ.get(key)
        if value:
            try:
                roots.append(_local_absolute(value))
            except ValueError:
                pass
    profile = os.environ.get("USERPROFILE")
    if profile:
        for relative in ("OneDrive", "iCloudPhotos", "Pictures/iCloud Photos"):
            roots.append(_local_absolute(Path(profile) / relative))
    return tuple(dict.fromkeys(roots))


def _configured_cloud(path, roots=None):
    # Windows cloud-compatibility layers can mask reparse flags and tags.
    key = os.path.normcase(str(path)).rstrip("\\/")
    for root in _configured_cloud_roots() if roots is None else roots:
        parent = os.path.normcase(str(root)).rstrip("\\/")
        if key == parent or key.startswith(parent + os.sep):
            return True
    return False


def _native_path(path):
    raw = os.fspath(path)
    if os.name == "nt" and not raw.startswith("\\\\?\\"):
        return "\\\\?\\" + os.path.abspath(raw)
    return raw


def _display_path(path):
    raw = os.fspath(path)
    if raw.startswith("\\\\?\\") and len(raw) > 6 and raw[5] == ":":
        raw = raw[4:]
    return Path(raw)


_ATTRIBUTE_API = None


def _native_attributes(path):
    global _ATTRIBUTE_API
    if _ATTRIBUTE_API is None:
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetFileAttributesW.argtypes = [wintypes.LPCWSTR]
        kernel.GetFileAttributesW.restype = wintypes.DWORD
        _ATTRIBUTE_API = kernel.GetFileAttributesW
    value = _ATTRIBUTE_API(_native_path(path))
    if value == 0xFFFFFFFF:
        import ctypes
        raise ctypes.WinError(ctypes.get_last_error())
    return value


def _native_stat(path, *, follow_symlinks=False):
    if os.name == "nt":
        # Python stat can mask non-name-surrogate cloud reparse flags.
        # Inspect native flags before any potentially recalling stat operation.
        attrs = _native_attributes(path)
        if attrs & UNSAFE_ATTRIBUTES:
            from types import SimpleNamespace
            return SimpleNamespace(st_mode=stat.S_IFDIR if attrs & 0x10 else stat.S_IFREG,
                                   st_file_attributes=attrs)
    return os.stat(_native_path(path), follow_symlinks=follow_symlinks)


def _native_scandir(path):
    return os.scandir(_native_path(path))


def _safe_output(path):
    """Reject links/cloud ancestry and linked report files before writing."""
    if _configured_cloud(path):
        raise ValueError("Output must be outside configured cloud sync roots")
    for item in reversed((path, *path.parents)):
        try:
            meta = _native_stat(item, follow_symlinks=False)
        except FileNotFoundError:
            continue
        if _unsafe(meta):
            raise ValueError("Output must not contain links, reparse or cloud/offline paths")
        if item == path and not stat.S_ISDIR(meta.st_mode):
            raise ValueError("Output must be a directory")


def _safe_report_file(path):
    try:
        meta = _native_stat(path, follow_symlinks=False)
    except FileNotFoundError:
        return
    if _unsafe(meta) or not stat.S_ISREG(meta.st_mode) or meta.st_nlink != 1:
        raise ValueError("Report destination must be an ordinary unlinked file")


def _atomic_json(path, value):
    _safe_output(path.parent)
    _safe_report_file(path)
    payload = json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(",", ":")).encode("utf-8")
    fd, name = tempfile.mkstemp(prefix=".storage-audit-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        _safe_output(path.parent)
        _safe_report_file(path)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)  # only this writer's temporary report, never scanned data


class StorageAudit:
    """One explicit metadata traversal; providers allow tests without native I/O.

    providers may override scandir, stat, monotonic and utc_now. DirEntry.stat is
    used ONLY for enumeration hazard flags. Fresh os.stat(follow_symlinks=False)
    supplies file size/identity/nlink (Windows cached DirEntry values may be 0).
    """

    def __init__(self, roots, output_dir=None, *, top_count=200, aggregate_depth=3,
                 progress_interval=5.0, providers=None):
        if isinstance(roots, (str, bytes, os.PathLike)):
            raise ValueError("roots must be a nonempty sequence of explicit paths")
        values = list(roots)
        if not 1 <= len(values) <= 16:
            raise ValueError("Specify between 1 and 16 roots")
        self.roots = []
        for value in values:
            path = _local_absolute(value)
            if path in self.roots:
                continue
            if any(_within(path, old) or _within(old, path) for old in self.roots):
                raise ValueError("Overlapping roots would double count files")
            self.roots.append(path)
        if output_dir is None:
            base = os.environ.get("LOCALAPPDATA")
            if not base:
                raise ValueError("Specify an explicit local output directory")
            output_dir = Path(base) / "ProstranstvoLocalBeta" / "data" / "storage-audit"
        self.output_dir = _local_absolute(output_dir)
        if self.output_dir.drive.casefold() == "d:":
            raise ValueError("Audit reports must be stored outside D:")
        if os.name == "nt" and (self.output_dir.drive.casefold() != "c:" or any(p.drive.casefold() not in ("c:", "d:") for p in self.roots)):
            raise ValueError("This Windows audit accepts C:/D: roots and a C: report directory only")
        if any(_within(root, self.output_dir) for root in self.roots):
            raise ValueError("Output must not equal or contain a scan root")
        if type(top_count) is not int or not 1 <= top_count <= 200:
            raise ValueError("top_count must be between 1 and 200")
        if type(aggregate_depth) is not int or aggregate_depth not in (2, 3):
            raise ValueError("aggregate_depth must be 2 or 3")
        if isinstance(progress_interval, bool) or not isinstance(progress_interval, (int, float)) or not math.isfinite(progress_interval) or progress_interval < 1:
            raise ValueError("progress_interval must be at least one second")
        self.top_count = top_count
        self.aggregate_depth = aggregate_depth
        self.progress_interval = float(progress_interval)
        providers = providers or {}
        if set(providers) - {"scandir", "stat", "monotonic", "utc_now"}:
            raise ValueError("Unknown provider")
        self._scandir = providers.get("scandir", _native_scandir)
        self._stat = providers.get("stat", _native_stat)
        self._clock = providers.get("monotonic", time.monotonic)
        self._now = providers.get("utc_now", _utc_now)
        self.report_path = self.output_dir / "report.json"
        self.progress_path = self.output_dir / "progress.json"
        self._cloud_roots = _configured_cloud_roots()

    def _reset(self):
        keys = ("files", "directories", "entries_seen", "logical_bytes_raw",
                "logical_bytes_unique_known", "hardlink_duplicate_paths", "hardlink_size_conflicts",
                "unknown_identity_files", "unknown_identity_bytes", "hardlink_index_capped_files",
                "errors", "permission_denied", "skipped_entries", "skipped_reparse",
                "skipped_symlinks", "skipped_cloud_or_offline", "skipped_configured_cloud_roots", "skipped_special",
                "skipped_depth", "excluded_output_entries", "aggregate_buckets_omitted",
                "aggregate_file_contributions_omitted", "path_labels_truncated")
        self.counts = dict.fromkeys(keys, 0)
        self._heap = []
        self._identities = {}
        self._aggregates = {}
        self._root_totals = {}
        self._categories = {}
        self._errors = []
        self._sequence = 0
        self._partial = False
        self._current = None
        self._started = _iso(self._now())
        self._start_clock = self._clock()
        self._last_progress = self._start_clock

    def _label(self, path):
        value = str(path)
        if len(value) > MAX_PATH_CHARS:
            self.counts["path_labels_truncated"] += 1
            return value[:MAX_PATH_CHARS] + "…"
        return value

    def _error(self, path, operation, error):
        self.counts["errors"] += 1
        self.counts["permission_denied"] += int(isinstance(error, PermissionError))
        self._partial = True
        if len(self._errors) < MAX_ERROR_SAMPLES:
            self._errors.append({"path": self._label(path), "operation": operation,
                                 "error_type": type(error).__name__, "errno": getattr(error, "errno", None),
                                 "winerror": getattr(error, "winerror", None)})

    def _skip(self, meta):
        if not _unsafe(meta):
            return False
        self.counts["skipped_entries"] += 1
        self.counts["skipped_symlinks"] += int(stat.S_ISLNK(meta.st_mode))
        self.counts["skipped_reparse"] += int(bool(_attributes(meta) & REPARSE))
        self.counts["skipped_cloud_or_offline"] += int(bool(_attributes(meta) & (OFFLINE | RECALL_ON_OPEN | RECALL_ON_DATA_ACCESS)))
        self._partial = True
        return True

    @staticmethod
    def _bucket(path):
        return {"path": str(path), "files": 0, "logical_bytes_raw": 0, "logical_bytes_unique_known": 0}

    def _unique(self, meta, size):
        dev, ino, links = (getattr(meta, key, 0) for key in ("st_dev", "st_ino", "st_nlink"))
        reliable = all(isinstance(x, int) and x > 0 for x in (dev, ino, links))
        duplicate = False
        if reliable and links == 1:
            return size, reliable, duplicate
        if reliable:
            key = (dev, ino)
            if key in self._identities:
                duplicate = True
                self.counts["hardlink_duplicate_paths"] += 1
                if self._identities[key] != size:
                    self.counts["hardlink_size_conflicts"] += 1
                    self._partial = True
                return 0, reliable, duplicate
            if len(self._identities) < MAX_HARDLINK_IDENTITIES:
                self._identities[key] = size
                return size, reliable, duplicate
            self.counts["hardlink_index_capped_files"] += 1
        self.counts["unknown_identity_files"] += 1
        self.counts["unknown_identity_bytes"] += size
        self._partial = True
        return 0, False, duplicate

    def _file(self, path, meta, root, buckets, aggregates_complete):
        size = max(0, int(meta.st_size))
        unique, identity_known, duplicate = self._unique(meta, size)
        self.counts["files"] += 1
        self.counts["logical_bytes_raw"] += size
        self.counts["logical_bytes_unique_known"] += unique
        for bucket in (self._root_totals[str(root)], *(self._aggregates[key] for key in buckets)):
            bucket["files"] += 1
            bucket["logical_bytes_raw"] += size
            bucket["logical_bytes_unique_known"] += unique
        if not aggregates_complete:
            self.counts["aggregate_file_contributions_omitted"] += 1
        category = _CATEGORY.get(path.suffix.casefold(), "other")
        totals = self._categories.setdefault(category, {"files": 0, "logical_bytes_raw": 0, "logical_bytes_unique_known": 0})
        totals["files"] += 1
        totals["logical_bytes_raw"] += size
        totals["logical_bytes_unique_known"] += unique
        self._sequence += 1
        # Only create exported metadata for candidates that enter the bounded heap.
        if len(self._heap) < self.top_count or size > self._heap[0][0]:
            record = {"path": self._label(path), "logical_bytes": size, "category": category,
                      "hardlink_count": max(0, int(getattr(meta, "st_nlink", 0))),
                      "identity_known": identity_known, "duplicate_hardlink_path": duplicate,
                      "modified_utc": _timestamp(getattr(meta, "st_mtime", None)),
                      "accessed_utc": _timestamp(getattr(meta, "st_atime", None)),
                      "created_utc": _timestamp(getattr(meta, "st_birthtime", None)),
                      "sparse": bool(_attributes(meta) & 0x200), "compressed": bool(_attributes(meta) & 0x800)}
            item = (size, self._sequence, record)
            if len(self._heap) < self.top_count:
                heapq.heappush(self._heap, item)
            else:
                heapq.heapreplace(self._heap, item)

    def _progress(self, status="running", force=False):
        clock = self._clock()
        if not force and clock - self._last_progress < self.progress_interval:
            return
        value = {"version": 1, "status": status, "started_utc": self._started,
                 "updated_utc": _iso(self._now()), "elapsed_seconds": round(max(0, clock - self._start_clock), 3),
                 "current_directory": str(self._current)[:MAX_PATH_CHARS] if self._current else None,
                 "counters": dict(self.counts), "total_entries_unknown": True,
                 "report_path": str(self.report_path)}
        _atomic_json(self.progress_path, value)
        self._last_progress = clock

    def _open_directory(self, path):
        try:
            meta = self._stat(path, follow_symlinks=False)
            if self._skip(meta):
                return None
            if not stat.S_ISDIR(meta.st_mode):
                self.counts["skipped_special"] += 1
                self._partial = True
                return None
            iterator = self._scandir(path)
            self.counts["directories"] += 1
            return iterator
        except OSError as error:
            self._error(path, "scandir_open", error)
            return None

    def _scan_root(self, root):
        self._root_totals[str(root)] = self._bucket(root)
        if _configured_cloud(root, self._cloud_roots):
            self.counts["skipped_entries"] += 1
            self.counts["skipped_cloud_or_offline"] += 1
            self.counts["skipped_configured_cloud_roots"] += 1
            self._partial = True
            return
        # Do not enter an explicitly supplied path through a hazardous ancestor.
        for ancestor in reversed(root.parents):
            try:
                if self._skip(self._stat(ancestor, follow_symlinks=False)):
                    return
            except OSError as error:
                self._error(ancestor, "root_ancestor_stat", error)
                return
        iterator = self._open_directory(root)
        if iterator is None:
            return
        stack = [(root, 0, (), True, iterator)]
        try:
            while stack:
                path, depth, buckets, aggregates_complete, iterator = stack[-1]
                self._current = path
                self._progress()
                try:
                    entry = next(iterator)
                except StopIteration:
                    iterator.close()
                    stack.pop()
                    continue
                except OSError as error:
                    self._error(path, "scandir_next", error)
                    iterator.close()
                    stack.pop()
                    continue
                child = _display_path(entry.path)
                self.counts["entries_seen"] += 1
                if _configured_cloud(child, self._cloud_roots):
                    self.counts["skipped_entries"] += 1
                    self.counts["skipped_cloud_or_offline"] += 1
                    self.counts["skipped_configured_cloud_roots"] += 1
                    self._partial = True
                    continue
                if _within(child, self.output_dir):
                    self.counts["excluded_output_entries"] += 1
                    continue
                try:
                    # Enumeration flags first: do not stat/hydrate placeholders.
                    hint = entry.stat(follow_symlinks=False)
                    if self._skip(hint):
                        continue
                    meta = self._stat(child, follow_symlinks=False)
                    if self._skip(meta):
                        continue
                    if stat.S_ISREG(meta.st_mode):
                        self._file(child, meta, root, buckets, aggregates_complete)
                    elif stat.S_ISDIR(meta.st_mode):
                        if depth >= MAX_DEPTH:
                            self.counts["skipped_depth"] += 1
                            self.counts["skipped_entries"] += 1
                            self._partial = True
                            continue
                        child_buckets, child_complete = buckets, aggregates_complete
                        if depth < self.aggregate_depth:
                            key = str(child)
                            if len(self._aggregates) < MAX_AGGREGATES:
                                self._aggregates[key] = self._bucket(child)
                                child_buckets = (*buckets, key)
                            else:
                                self.counts["aggregate_buckets_omitted"] += 1
                                child_complete = False
                                self._partial = True
                        child_iterator = self._open_directory(child)
                        if child_iterator is not None:
                            stack.append((child, depth + 1, child_buckets, child_complete, child_iterator))
                    else:
                        self.counts["skipped_special"] += 1
                        self.counts["skipped_entries"] += 1
                        self._partial = True
                except OSError as error:
                    self._error(child, "entry_stat", error)
        finally:
            for _, _, _, _, remaining in stack:
                remaining.close()

    def _report(self, interrupted):
        status = "interrupted" if interrupted else "partial" if self._partial else "completed"
        aggregates = heapq.nlargest(MAX_AGGREGATE_ROWS, self._aggregates.values(), key=lambda row: row["logical_bytes_raw"])
        rows = [{**row, "path": self._label(row["path"])} for row in aggregates]
        value = {"version": 1, "status": status, "traversal_finished": not interrupted,
                 "all_contents_visible": False, "started_utc": self._started,
                 "finished_utc": _iso(self._now()), "roots": [str(p) for p in self.roots],
                 "report_path": str(self.report_path), "output_directory_excluded": str(self.output_dir),
                 "configured_cloud_roots_excluded": [str(path) for path in self._cloud_roots],
                 "counters": dict(self.counts),
                 "logical_bytes_unique_upper_bound": self.counts["logical_bytes_unique_known"] + self.counts["unknown_identity_bytes"],
                 "unique_size_complete_for_observed_files": not (self.counts["unknown_identity_files"] or self.counts["hardlink_size_conflicts"]),
                 "root_totals": list(self._root_totals.values()), "folder_aggregates": rows,
                 "categories": self._categories,
                 "largest_files": [item[2] for item in sorted(self._heap, reverse=True)],
                 "errors_sample": list(self._errors),
                 "output_limits": {"max_report_bytes": MAX_REPORT_BYTES, "top_count": self.top_count,
                                   "aggregate_depth": self.aggregate_depth, "aggregate_rows_omitted": len(self._aggregates) - len(rows),
                                   "file_names_omitted_from_top": self.counts["files"] - len(self._heap),
                                   "error_samples_omitted": self.counts["errors"] - len(self._errors), "budget_truncated": False},
                 "notes": ["Logical sizes are not allocated, physical or reclaimable bytes.",
                           "Metadata only: no file contents, hashes, cleanup or deletion verdicts.",
                           "Completed traversal does not prove every name or byte is visible; skipped directories contain an unknown number of entries.",
                           "Reparse/symlink/offline/Recall placeholders are skipped; reason counters may overlap.",
                           "Configured OneDrive and conventional iCloud Photos roots are excluded even if Windows masks cloud reparse flags.",
                           "Other sync providers or relocated roots with masked flags may remain unrecognized; cloud exclusion is not a universal guarantee.",
                           "Folder totals overlap across ancestor levels. Unique bytes are assigned to the first observed hardlink path, not reclaimable folder sizes.",
                           "Extension classification is heuristic. Modified/accessed times do not prove a file is unused.",
                           "Live metadata is not an atomic filesystem snapshot. Concurrent replacement can race metadata checks.",
                           "Progress is emitted between metadata operations; a blocked filesystem operation can delay it.",
                           "Largest-file and aggregate output caps do not stop traversal. Reports contain private local paths."]}
        # Cap serialized output, never the traversal. Fixed-size summary remains.
        for key in ("folder_aggregates", "errors_sample", "largest_files"):
            while len(json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(",", ":")).encode("utf-8")) > MAX_REPORT_BYTES and value[key]:
                del value[key][len(value[key]) // 2:]
                value["output_limits"]["budget_truncated"] = True
            if key == "folder_aggregates":
                value["output_limits"]["aggregate_rows_omitted"] = len(self._aggregates) - len(value[key])
            elif key == "errors_sample":
                value["output_limits"]["error_samples_omitted"] = self.counts["errors"] - len(value[key])
            else:
                value["output_limits"]["file_names_omitted_from_top"] = self.counts["files"] - len(value[key])
        if value["output_limits"]["budget_truncated"] or self.counts["path_labels_truncated"]:
            value["status"] = "interrupted" if interrupted else "partial"
        if len(json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(",", ":")).encode("utf-8")) > MAX_REPORT_BYTES:
            raise ValueError("Report budget too small for the bounded summary")
        return value

    def run(self):
        """Scan explicit roots and atomically save report/progress; no source writes."""
        _safe_output(self.output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        _safe_output(self.output_dir)
        self._reset()
        self._progress(force=True)
        interrupted = False
        try:
            for root in self.roots:
                self._scan_root(root)
        except KeyboardInterrupt:
            interrupted = True
            self._partial = True
        report = self._report(interrupted)
        _atomic_json(self.report_path, report)
        self._progress(status=report["status"], force=True)
        return report


def _below_normal_priority():
    """Only the explicitly invoked CLI's own process; no import-time OS action."""
    if os.name != "nt":
        return "not_applicable"
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetCurrentProcess.argtypes = []
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.SetPriorityClass.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.SetPriorityClass.restype = wintypes.BOOL
    if not kernel.SetPriorityClass(kernel.GetCurrentProcess(), 0x4000):
        raise ctypes.WinError(ctypes.get_last_error())
    return "below_normal"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", action="append", required=True, help="Explicit local absolute root; repeat for C:/ and D:/")
    parser.add_argument("--output", help="Local report directory outside D: (excluded from traversal)")
    parser.add_argument("--top-count", type=int, default=200)
    parser.add_argument("--aggregate-depth", type=int, choices=(2, 3), default=3)
    args = parser.parse_args(argv)
    audit = StorageAudit(args.root, args.output, top_count=args.top_count, aggregate_depth=args.aggregate_depth)
    priority = _below_normal_priority()
    report = audit.run()
    print(json.dumps({"report_path": report["report_path"], "status": report["status"],
                      "traversal_finished": report["traversal_finished"], "priority": priority,
                      "counters": report["counters"]}, ensure_ascii=True))
    return 130 if report["status"] == "interrupted" else 0


if __name__ == "__main__":
    raise SystemExit(main())
