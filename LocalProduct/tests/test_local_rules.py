import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
"""Rule contracts using isolated files, an isolated queue, and an unshown Tk root."""
import concurrent.futures
import gc
import json
import os
import tempfile
import threading
import time
import unittest
import weakref
from pathlib import Path
from unittest import mock

from engine import BASE, Executor
from local_rules import RuleError, RuleStore


class RuleTests(unittest.TestCase):
    def setUp(self):
        (BASE / "tests-work").mkdir(exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=BASE / "tests-work", prefix="rules-")
        self.root = Path(self.tmp.name)
        self.source = self.root / "source"
        self.destination = self.root / "destination"
        self.source.mkdir()
        self.destination.mkdir()
        self.engine = Executor(self.root / "executor", [self.source, self.destination],
                               self.root / "missing-index.sqlite", [])
        self.store = RuleStore(self.engine, self.root / "rules")

    def tearDown(self):
        self.tmp.cleanup()

    def file(self, name="document.txt", data=b"example"):
        path = self.source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def rule(self, **changes):
        return self.store.save_rule(dict(name="Мои документы", source=str(self.source),
                                         destination=str(self.destination), action="copy", **changes))

    def test_saving_never_enqueues_or_executes(self):
        source = self.file()
        rule = self.rule()
        self.assertEqual(rule["version"], 1)
        self.assertEqual(self.engine.jobs(), [])
        self.assertTrue(source.exists())
        self.assertEqual(list(self.destination.iterdir()), [])
        other = RuleStore(self.engine, self.store.root)
        self.assertEqual(other.list_rules(), [rule])

    def test_strict_schema_rejects_code_and_unknown_actions(self):
        for change in ({"command": "echo run"}, {"schedule": "hourly"},
                       {"action": "delete"}, {"max_files": True}, {"max_files": 101},
                       {"max_total_bytes": 0}, {"enabled": 1}, {"extensions": ["*.txt"]}):
            value = dict(name="Unsafe", source=str(self.source), destination=str(self.destination), action="copy")
            value.update(change)
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.store.save_rule(value)
        self.assertEqual(self.store.list_rules(), [])

    def test_path_guard_and_existing_directory_required(self):
        for changes in ({"source": "relative"}, {"source": str(self.root)},
                        {"destination": str(self.root / "outside")},
                        {"destination": str(self.destination / "missing")},
                        {"destination": str(self.source)}):
            value = dict(name="Paths", source=str(self.source), destination=str(self.destination), action="copy")
            value.update(changes)
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.store.save_rule(value)

    def test_duplicate_names_and_stale_edit_rejected(self):
        rule = self.rule()
        value = dict(rule)
        value.update(name="Новая версия")
        saved = self.store.save_rule(value)
        self.assertEqual(saved["version"], 2)
        with self.assertRaisesRegex(RuleError, "уже изменено"):
            self.store.save_rule(rule)
        value = dict(name=" новая ВЕРСИЯ ", source=str(self.source), destination=str(self.destination), action="copy")
        with self.assertRaisesRegex(RuleError, "именем"):
            self.store.save_rule(value)

    def test_concurrent_edit_has_one_winner_without_lost_update(self):
        rule = self.rule()
        stores = [RuleStore(self.engine, self.store.root) for _ in range(2)]
        barrier = threading.Barrier(2)
        def edit(n):
            barrier.wait()
            try:
                return stores[n].save_rule({**rule, "name": "Редакция " + str(n)})
            except RuleError:
                return None
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(edit, range(2)))
        self.assertEqual(sum(value is not None for value in results), 1)
        self.assertEqual(self.store.list_rules()[0]["version"], 2)

    def test_glob_substring_and_exact_extension_are_conjunctive(self):
        selected = self.file("Report_July.TXT")
        self.file("Report_June.txt")
        self.file("Report_July.txt.png")
        self.file("Other_July.txt")
        rule = self.rule(name_glob="report_*", name_contains="JULY", extensions=[".TXT"])
        preview = self.store.preview(rule["id"])
        self.assertEqual([x["path"] for x in preview["candidates"]], [str(selected)])
        self.assertEqual(preview["total_bytes"], selected.stat().st_size)
        self.assertIn("расширение: .txt", preview["candidates"][0]["reason"])
        self.assertEqual(preview["rule_version"], 1)
        self.assertEqual(self.engine.jobs(), [])

    def test_changed_source_invalidates_preview_without_job(self):
        source = self.file()
        rule = self.rule()
        preview = self.store.preview(rule["id"])
        source.write_bytes(b"changed source, larger")
        with self.assertRaisesRegex(RuleError, "изменилась"):
            self.store.enqueue(rule["id"], preview["token"], rule["version"])
        self.assertEqual(self.engine.jobs(), [])

    def test_new_matching_file_invalidates_exact_selection(self):
        self.file()
        rule = self.rule(extensions=[".txt"])
        preview = self.store.preview(rule["id"])
        self.file("new.txt")
        with self.assertRaisesRegex(RuleError, "изменилась"):
            self.store.enqueue(rule["id"], preview["token"])
        self.assertEqual(self.engine.jobs(), [])

    def test_removed_matching_file_invalidates_exact_selection(self):
        source = self.file()
        rule = self.rule()
        preview = self.store.preview(rule["id"])
        source.unlink()
        with self.assertRaisesRegex(RuleError, "изменилась"):
            self.store.enqueue(rule["id"], preview["token"])
        self.assertEqual(self.engine.jobs(), [])

    def test_irrelevant_added_file_does_not_change_copy_selection(self):
        source = self.file()
        rule = self.rule(extensions=[".txt"])
        preview = self.store.preview(rule["id"])
        self.file("irrelevant.png")
        job = self.store.enqueue(rule["id"], preview["token"])
        self.assertEqual([r["src"] for r in self.engine.get(job)["payload"]["records"]], [str(source)])

    def test_old_rule_version_and_expired_preview_cannot_queue(self):
        self.file()
        rule = self.rule()
        preview = self.store.preview(rule["id"])
        self.store.save_rule({**rule, "name_contains": "document"})
        with self.assertRaisesRegex(RuleError, "Версия"):
            self.store.enqueue(rule["id"], preview["token"])
        preview = self.store.preview(rule["id"])
        with mock.patch("local_rules.time.time", return_value=preview["expires_at"] + 1):
            with self.assertRaisesRegex(RuleError, "устарел"):
                self.store.enqueue(rule["id"], preview["token"])
        self.assertEqual(self.engine.jobs(), [])

    def test_copy_reuses_executor_approval_and_verified_execution(self):
        source = self.file(data=b"valuable fixture data")
        rule = self.rule()
        preview = self.store.preview(rule["id"])
        with mock.patch.object(self.engine, "plan_transfer", wraps=self.engine.plan_transfer) as plan:
            job = self.store.enqueue(rule["id"], preview["token"], rule["version"])
        plan.assert_called_once_with([str(source)], str(self.destination), mode="copy")
        self.assertEqual(self.engine.get(job)["status"], "needs_approval")
        self.assertTrue(source.exists())
        self.assertFalse((self.destination / source.name).exists())
        with self.assertRaises(ValueError):
            self.engine.execute(job)
        self.engine.approve(job)
        self.engine.execute(job)
        self.assertEqual(source.read_bytes(), (self.destination / source.name).read_bytes())

    def test_repeated_token_returns_same_job_after_store_reopen(self):
        self.file()
        rule = self.rule()
        preview = self.store.preview(rule["id"])
        first = self.store.enqueue(rule["id"], preview["token"])
        other = RuleStore(self.engine, self.store.root)
        self.assertEqual(other.enqueue(rule["id"], preview["token"]), first)
        self.assertEqual(len(self.engine.jobs()), 1)

    def test_source_changed_between_revalidation_and_hash_cancels_plan(self):
        source = self.file()
        rule = self.rule()
        preview = self.store.preview(rule["id"])
        original = self.engine.plan_transfer
        def changed(sources, destination, mode):
            source.write_bytes(b"newer content after selection")
            return original(sources, destination, mode)
        with mock.patch.object(self.engine, "plan_transfer", side_effect=changed):
            with self.assertRaisesRegex(RuleError, "план отменён"):
                self.store.enqueue(rule["id"], preview["token"])
        self.assertEqual([job["status"] for job in self.engine.jobs()], ["cancelled"])
        self.assertEqual(list(self.destination.iterdir()), [])

    def test_concurrent_enqueue_creates_one_job(self):
        self.file()
        rule = self.rule()
        preview = self.store.preview(rule["id"])
        barrier = threading.Barrier(2)
        def enqueue(_):
            store = RuleStore(self.engine, self.store.root)
            barrier.wait()
            return store.enqueue(rule["id"], preview["token"])
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            jobs = list(pool.map(enqueue, range(2)))
        self.assertEqual(jobs[0], jobs[1])
        self.assertEqual(len(self.engine.jobs()), 1)

    def test_interrupted_preparation_cannot_repeat_side_effect(self):
        self.file()
        rule = self.rule()
        preview = self.store.preview(rule["id"])
        state = json.loads(self.store.file.read_text(encoding="utf-8"))
        state["previews"][preview["token"]]["status"] = "preparing"
        self.store.file.write_text(json.dumps(state), encoding="utf-8")
        with self.assertRaisesRegex(RuleError, "прервалась"):
            self.store.enqueue(rule["id"], preview["token"])
        self.assertEqual(self.engine.jobs(), [])

    def test_corrupt_registry_and_preview_fail_with_clear_rule_error(self):
        self.file()
        rule = self.rule()
        preview = self.store.preview(rule["id"])
        original = json.loads(self.store.file.read_text(encoding="utf-8"))
        for change in ("boolean-format", "missing-rule-id", "bad-fingerprint", "bad-job"):
            state = json.loads(json.dumps(original))
            record = state["previews"][preview["token"]]
            if change == "boolean-format":
                state["format"] = True
            elif change == "missing-rule-id":
                del record["rule_id"]
            elif change == "bad-fingerprint":
                record["candidates"][0]["size"] = "seven"
            else:
                record.update(status="queued", job_id="not-a-job-id")
            self.store.file.write_text(json.dumps(state), encoding="utf-8")
            with self.subTest(change=change), self.assertRaises(RuleError):
                self.store.list_rules()
        self.assertEqual(self.engine.jobs(), [])

    def test_collisions_at_preview_and_between_preview_and_queue(self):
        source = self.file()
        rule = self.rule()
        preview = self.store.preview(rule["id"])
        target = self.destination / source.name
        target.write_bytes(b"existing fixture")
        with self.assertRaisesRegex(RuleError, "Коллизия"):
            self.store.enqueue(rule["id"], preview["token"])
        with self.assertRaisesRegex(RuleError, "Коллизия"):
            self.store.preview(rule["id"])
        self.assertEqual(target.read_bytes(), b"existing fixture")
        self.assertEqual(self.engine.jobs(), [])

    def test_same_flat_destination_name_rejected(self):
        self.file("one/report.txt")
        self.file("two/REPORT.txt")
        rule = self.rule()
        with self.assertRaisesRegex(RuleError, "Коллизия"):
            self.store.preview(rule["id"])

    def test_limits_fail_closed_without_partial_plan(self):
        self.file("one.txt", b"12345")
        self.file("two.txt", b"67890")
        rule = self.rule(max_files=1)
        with self.assertRaisesRegex(RuleError, "лимит"):
            self.store.preview(rule["id"])
        rule = self.store.save_rule({**rule, "max_files": 2, "max_total_bytes": 9})
        with self.assertRaisesRegex(RuleError, "лимит"):
            self.store.preview(rule["id"])
        rule = self.store.save_rule({**rule, "max_total_bytes": 10})
        with mock.patch("local_rules.SCAN_LIMIT", 1):
            with self.assertRaises(ValueError):
                self.store.preview(rule["id"])
        self.assertEqual(self.engine.jobs(), [])

    def test_no_matches_cannot_enqueue(self):
        self.file("image.png")
        rule = self.rule(extensions=[".txt"])
        preview = self.store.preview(rule["id"])
        self.assertEqual(preview["count"], 0)
        with self.assertRaisesRegex(RuleError, "нет"):
            self.store.enqueue(rule["id"], preview["token"])
        self.assertEqual(self.engine.jobs(), [])

    def test_disabled_rule_and_protected_file_cannot_run(self):
        self.file("setup.exe")
        self.file("secret-password.txt")
        rule = self.rule()
        self.assertEqual(self.store.preview(rule["id"])["count"], 0)
        rule = self.store.save_rule({**rule, "enabled": False})
        with self.assertRaisesRegex(RuleError, "отключено"):
            self.store.preview(rule["id"])

    def test_selected_inventory_executes_only_reviewed_files(self):
        selected = self.file("report.txt")
        self.file("image.png")
        rule = self.store.save_rule(dict(name="Обзор текстов", source=str(self.source),
                                         action="scan", extensions=[".txt"]))
        preview = self.store.preview(rule["id"])
        job = self.store.enqueue(rule["id"], preview["token"])
        self.file("later.txt")
        with mock.patch.object(self.engine, "scan", side_effect=AssertionError("No rescan")):
            result = self.engine.execute(job)["result"]
        self.assertEqual(result["files"], 1)
        self.assertEqual([r["path"] for r in result["largest"]], [str(selected)])
        self.assertEqual(list(self.destination.iterdir()), [])

    def test_selected_inventory_changed_after_queue_fails(self):
        selected = self.file()
        rule = self.store.save_rule(dict(name="Обзор", source=str(self.source), action="scan"))
        preview = self.store.preview(rule["id"])
        job = self.store.enqueue(rule["id"], preview["token"])
        selected.write_bytes(b"changed inventory fixture")
        with self.assertRaises(ValueError):
            self.engine.execute(job)
        self.assertEqual(self.engine.get(job)["status"], "failed")

    @unittest.skipUnless(hasattr(os, "link"), "Hard links unavailable")
    def test_registry_hardlink_rejected(self):
        self.rule()
        os.link(self.store.file, self.root / "linked-registry.json")
        with self.assertRaises(ValueError):
            self.store.list_rules()

    def test_panel_normalizes_saved_form_and_keeps_work_off_tk_thread(self):
        import tkinter as tk
        from rules_ui import RulePanel
        tk_root = tk.Tk()
        tk_root.withdraw()
        main_thread = threading.get_ident()
        thread_ids = []
        original = self.store.preview
        def preview(ident):
            thread_ids.append(threading.get_ident())
            return original(ident)
        self.store.preview = preview
        panel = RulePanel(tk_root, self.engine, store=self.store)
        def idle():
            end = time.monotonic() + 4
            while panel._busy and time.monotonic() < end:
                tk_root.update()
                time.sleep(.005)
            tk_root.update()
            self.assertFalse(panel._busy, panel.status.get())
        try:
            idle()
            self.file("report.txt")
            panel.name.set("Тексты")
            panel.source.set(str(self.source).replace("\\", "/"))
            panel.destination.set(str(self.destination).replace("\\", "/"))
            panel.extensions.set(".TXT")
            panel.save_rule()
            idle()
            self.assertIsNotNone(panel._current, panel.status.get())
            self.assertEqual(panel.extensions.get(), ".txt")
            panel.preview_rule()
            idle()
            self.assertIsNotNone(panel._preview, panel.status.get())
            self.assertEqual(panel._preview["count"], 1)
            self.assertTrue(thread_ids)
            self.assertNotEqual(thread_ids[0], main_thread)
            self.assertEqual(self.engine.jobs(), [])
            panel.queue_rule()
            idle()
            self.assertEqual(len(self.engine.jobs()), 1)
            self.assertEqual(self.engine.jobs()[0]["status"], "needs_approval")
        finally:
            panel.close()
            tk_root.destroy()
            idle = None
            panel = tk_root = None
            gc.collect()

    def test_destroyed_panel_is_not_owned_by_blocked_worker(self):
        import tkinter as tk
        from rules_ui import RulePanel
        entered, release, finished = (threading.Event() for _ in range(3))
        def blocked_model_work():
            entered.set()
            if not release.wait(4):
                raise RuntimeError('fixture timeout')
            gc.collect()  # Worker GC must not own a destroyed Tcl interpreter.
            finished.set()
            return []
        window = tk.Tk()
        window.withdraw()
        panel = RulePanel(window, self.engine, store=self.store)
        thread = None
        retained_panel = None
        try:
            deadline = time.monotonic() + 4
            while panel._busy and time.monotonic() < deadline:
                window.update()
                time.sleep(.005)
            self.assertFalse(panel._busy)
            thread = panel._run('list', blocked_model_work)
            self.assertTrue(entered.wait(2))
            reference = weakref.ref(panel)
            panel.close()
            window.destroy()
            panel = window = None
            gc.collect()
            # Retain a failed old implementation on this thread during cleanup,
            # preventing its final Tcl release from crashing the test worker.
            retained_panel = reference()
            self.assertIsNone(retained_panel, 'The worker retained the Tk panel')
            self.assertFalse(finished.is_set())
        finally:
            release.set()
            if thread is not None:
                thread.join(4)
            if window is not None:
                panel.close()
                window.destroy()
            panel = window = retained_panel = None
            gc.collect()
        self.assertTrue(finished.is_set())
        self.assertFalse(thread.is_alive())


if __name__ == "__main__":
    unittest.main()
