"""Cross-feature fixtures: reviewed copy, verified archive and revoked grants."""
import ast
import hashlib
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import engine
from indexed_archives import ArchiveStore
from local_rules import RuleStore


class FileWorkflowTests(unittest.TestCase):
    def setUp(self):
        base = Path(__file__).resolve().parent / 'tests-work'
        base.mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix='file-flow-', dir=base)
        self.root = Path(self.temporary.name)
        self.source, self.destination, self.restored = [self.root / n for n in ('source', 'destination', 'restored')]
        for folder in (self.source, self.destination, self.restored):
            folder.mkdir()
        self.original = self.source / 'invoice-тест.txt'
        self.original.write_text('Проверка документов\n' * 200, encoding='utf-8')
        (self.source / 'ignore.txt').write_text('Не выбран правилом', encoding='utf-8')
        with mock.patch.object(engine, 'ROOTS', [self.source, self.destination, self.restored]), mock.patch.object(engine, 'BASE', self.root / 'code'):
            self.executor = engine.Executor(data=self.root / 'executor', index=self.root / 'no-index.sqlite', blocked=[])
        self.rules = RuleStore(self.executor, self.root / 'rules')
        self.archives = ArchiveStore(self.executor, self.root / 'archives')

    def tearDown(self):
        self.temporary.cleanup()

    def planned_copy(self):
        rule = self.rules.save_rule(dict(name='Тестовые счета', source=str(self.source), destination=str(self.destination), action='copy', name_contains='invoice', extensions=['.txt']))
        preview = self.rules.preview(rule['id'])
        self.assertEqual(preview['count'], 1)
        job = self.rules.enqueue(rule['id'], preview['token'])
        self.assertEqual(self.executor.get(job)['status'], 'needs_approval')
        self.assertFalse((self.destination / self.original.name).exists())
        return job

    def test_reviewed_copy_archive_restore_and_index_recovery(self):
        job = self.planned_copy()
        self.executor.approve(job)
        self.executor.execute(job)
        self.assertEqual(self.executor.get(job)['status'], 'completed')
        copied = self.destination / self.original.name
        self.assertFalse((self.destination / 'ignore.txt').exists())
        plan = self.archives.propose([str(copied)])
        result = self.archives.create(plan)
        self.assertTrue(Path(result['path']).is_file())
        entry = self.archives.search('ТЕСТ')[0]
        restored = self.archives.restore([entry['id']], str(self.restored))
        self.assertEqual(Path(restored[0]).read_bytes(), self.original.read_bytes())
        self.assertTrue(copied.exists())
        self.assertEqual(entry['sha256'], hashlib.sha256(self.original.read_bytes()).hexdigest())
        with self.archives._connect() as connection:
            connection.execute('DELETE FROM entries')
            connection.execute('DELETE FROM archives')
        self.assertEqual(self.archives.search(), [])
        self.assertEqual(self.archives.recover()['indexed_archives'], 1)
        self.assertEqual(len(self.archives.search('invoice')), 1)

    def test_revoking_destination_blocks_already_reviewed_copy(self):
        job = self.planned_copy()
        self.executor.set_allowed_roots([str(self.source)])
        self.executor.approve(job)
        with self.assertRaises(ValueError):
            self.executor.execute(job)
        self.assertEqual(self.executor.get(job)['status'], 'failed')
        self.assertFalse((self.destination / self.original.name).exists())
        self.assertTrue(self.original.exists())


class AppCloseContractTests(unittest.TestCase):
    def test_both_apps_wait_for_rules_and_archives_before_destroying(self):
        product = Path(__file__).resolve().parents[1]
        sources = [product / 'src' / 'app.py']
        personal = product.parent / 'WindowsCompanion' / 'app.py'
        if personal.is_file():
            sources.append(personal)
        for source in sources:
            # Execute only these two method definitions, with no imports, Tk
            # construction, personal data initialization or background workers.
            tree = ast.parse(source.read_text(encoding='utf-8-sig'))
            app = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'App')
            methods = [node for node in app.body if isinstance(node, ast.FunctionDef) and node.name in ('close', 'close_when_idle')]
            namespace = {'messagebox': types.SimpleNamespace(askyesno=mock.Mock(return_value=True))}
            exec(compile(ast.Module(body=methods, type_ignores=[]), str(source), 'exec'), namespace)
            for rules_busy, archive_busy in ((True, False), (False, True)):
                with self.subTest(app=source.parent.name, rules_busy=rules_busy, archive_busy=archive_busy):
                    window = types.SimpleNamespace(
                        rulespanel=types.SimpleNamespace(_busy=rules_busy),
                        archivepanel=types.SimpleNamespace(is_busy=mock.Mock(return_value=archive_busy)),
                        statusvar=mock.Mock(), after=mock.Mock(), engine=mock.Mock(),
                        studypanel=mock.Mock(), stop=mock.Mock(), destroy=mock.Mock(),
                    )
                    window.engine.jobs.return_value = []
                    window.studypanel.request_stop.return_value = True
                    window.close = types.MethodType(namespace['close'], window)
                    window.close_when_idle = types.MethodType(namespace['close_when_idle'], window)
                    window.close()
                    window.after.assert_called_once_with(400, window.close)
                    window.engine.jobs.assert_not_called()
                    window.studypanel.request_stop.assert_not_called()
                    window.stop.set.assert_not_called()
                    window.destroy.assert_not_called()
                    window.after.reset_mock()
                    window.close_when_idle()
                    window.after.assert_called_once_with(500, window.close_when_idle)
                    window.destroy.assert_not_called()
                    window.rulespanel._busy = False
                    window.archivepanel.is_busy.return_value = False
                    window.close_when_idle()
                    window.stop.set.assert_called_once_with()
                    window.destroy.assert_called_once_with()


if __name__ == '__main__':
    unittest.main()
