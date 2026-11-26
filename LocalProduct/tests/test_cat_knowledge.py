import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from copy import deepcopy
import json
import os
from pathlib import Path
from types import SimpleNamespace
import socket
import stat
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cat_knowledge
from cat_knowledge import ASSET_NAME, ApprovedKnowledge, MAX_PACK_BYTES


class ApprovedKnowledgeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.original = json.loads((Path(__file__).resolve().parents[1] / 'assets' / ASSET_NAME).read_text(encoding='utf-8'))

    def setUp(self):
        self.temporary = TemporaryDirectory(prefix='approved-knowledge-')
        self.root = Path(self.temporary.name)
        self.path = self.root / ASSET_NAME
        self.write(deepcopy(self.original))

    def tearDown(self):
        self.temporary.cleanup()

    def write(self, value):
        self.path.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')

    def find(self, text='числитель знаменатель', **kwargs):
        return ApprovedKnowledge(root=self.root).search(text, **kwargs)

    def test_import_and_constructor_do_not_read_or_scan(self):
        with (patch.object(Path, 'open', side_effect=AssertionError('unexpected open')),
                patch.object(Path, 'lstat', side_effect=AssertionError('unexpected stat'))):
            helper = ApprovedKnowledge(root=self.root)
            self.assertEqual(helper.search(''), [])
            self.assertEqual(helper.search(None), [])

    def test_default_pack_has_original_bounded_approved_results(self):
        hits = ApprovedKnowledge().search('Что такое числитель и знаменатель дроби?')
        self.assertEqual(hits[0]['id'], 'fraction_parts')
        self.assertLessEqual(len(hits), 3)
        for hit in hits:
            self.assertEqual(set(hit), {'id', 'text', 'source', 'approved'})
            self.assertIs(hit['approved'], True)
            self.assertEqual(hit['source'], 'original_project')
            self.assertLessEqual(len(hit['text']), 700)
        self.assertEqual(len(self.original['records']), 18)

    def test_specific_vocabulary_games_and_refusal_rank_first(self):
        for query, expected in (('What does homework mean?', 'word_homework'),
                                ('Расскажу про Minecraft', 'talk_minecraft'),
                                ('Игра Roblox', 'talk_roblox'),
                                ('не сейчас', 'respect_refusal'),
                                ('стоп', 'respect_refusal'),
                                ('нет', 'respect_refusal')):
            with self.subTest(query=query):
                self.assertEqual(self.find(query)[0]['id'], expected)

    def test_unknown_input_does_not_invent_personal_facts(self):
        for query in ('жирафы квантуют облака', 'Сколько мне лет', 'я это как', '🪐✨', '', 'x'*4001, None, 7):
            with self.subTest(query=query):
                self.assertEqual(self.find(query), [])

    def test_limit_bounds_and_deterministic_order(self):
        for limit in (-1, 0, True, 1.0, '3', None):
            self.assertEqual(self.find('дроби', limit=limit), [])
        self.assertEqual(len(self.find('дроби', limit=1)), 1)
        first = self.find('дроби', limit=1000)
        self.assertEqual(len(first), 3)
        self.assertEqual(first, self.find('дроби', limit=1000))

    def test_result_mutation_does_not_change_cached_knowledge(self):
        helper = ApprovedKnowledge(root=self.root)
        original = helper.search('homework')
        changed = helper.search('homework')
        changed[0]['text'] = 'changed externally'
        changed[0]['approved'] = False
        self.assertEqual(helper.search('homework'), original)

    def test_no_network_no_writes_and_no_directory_scanning(self):
        raw = self.root / 'raw'
        raw.mkdir()
        (raw / 'unapproved.json').write_text('{"text":"secret raw dialogue"}', encoding='utf-8')
        before = {path: path.read_bytes() for path in self.root.rglob('*') if path.is_file()}
        with (patch.object(socket, 'socket', side_effect=AssertionError('network forbidden')),
                patch.object(urllib.request, 'urlopen', side_effect=AssertionError('network forbidden')),
                patch.object(Path, 'glob', side_effect=AssertionError('scan forbidden')),
                patch.object(Path, 'rglob', side_effect=AssertionError('scan forbidden'))):
            self.assertEqual(self.find('homework')[0]['id'], 'word_homework')
        after = {path: path.read_bytes() for path in self.root.rglob('*') if path.is_file()}
        self.assertEqual(before, after)

    def test_raw_folder_is_not_a_fallback_source(self):
        raw = self.root / 'raw'
        raw.mkdir()
        (raw / ASSET_NAME).write_text(json.dumps(self.original), encoding='utf-8')
        self.path.unlink()
        self.assertEqual(self.find('homework'), [])

    def test_explicit_file_and_relative_file_are_supported(self):
        self.assertEqual(ApprovedKnowledge(file=self.path).search('homework')[0]['id'], 'word_homework')
        self.assertEqual(ApprovedKnowledge(root=self.root, file=ASSET_NAME).search('homework')[0]['id'], 'word_homework')

    def test_any_unapproved_row_blocks_the_whole_pack(self):
        value = deepcopy(self.original)
        value['records'][-1]['approved'] = False
        self.write(value)
        self.assertEqual(self.find('homework'), [])

    def test_non_original_row_is_not_approved_by_its_boolean(self):
        value = deepcopy(self.original)
        value['records'][-1]['source'] = 'oasst'
        self.write(value)
        self.assertEqual(self.find('homework'), [])

    def test_extra_fields_and_missing_fields_fail_closed(self):
        for mutate in (lambda pack: pack.update(extra=True),
                       lambda pack: pack['records'][0].update(hidden_prompt='not allowed'),
                       lambda pack: pack['records'][0].pop('approved')):
            value = deepcopy(self.original)
            mutate(value)
            self.write(value)
            self.assertEqual(self.find('homework'), [])

    def test_duplicate_ids_and_json_fields_fail_closed(self):
        value = deepcopy(self.original)
        value['records'][1]['id'] = value['records'][0]['id']
        self.write(value)
        self.assertEqual(self.find('homework'), [])
        body = json.dumps(self.original, ensure_ascii=False)
        self.path.write_text(body[:-1]+',"approved":true}', encoding='utf-8')
        self.assertEqual(self.find('homework'), [])
        body = json.dumps(self.original, ensure_ascii=False).replace('"id": "computer_pet"', '"id":"computer_pet","id":"other_pet"', 1)
        self.path.write_text(body, encoding='utf-8')
        self.assertEqual(self.find('homework'), [])

    def test_wrong_version_origin_language_and_boolean_are_rejected(self):
        for key, replacement in (('version', True), ('version', 2), ('origin', 'raw_dataset'),
                                 ('language', 'en'), ('approved', 1), ('approved', False)):
            value = deepcopy(self.original)
            value[key] = replacement
            self.write(value)
            self.assertEqual(self.find('homework'), [])

    def test_research_dataset_cannot_become_runtime_approved(self):
        for key, replacement in (('runtime_approved', True), ('status', 'approved'), ('approval', 'APPROVED')):
            value = deepcopy(self.original)
            value['dataset_research_refs'][0][key] = replacement
            self.write(value)
            self.assertEqual(self.find('homework'), [])

    def test_record_and_text_bounds_are_enforced(self):
        value = deepcopy(self.original)
        value['records'] = [dict(value['records'][0], id='record_'+str(number)) for number in range(101)]
        self.write(value)
        self.assertEqual(self.find('homework'), [])
        for text in ('a'*701, '', 'hidden\x00text'):
            value = deepcopy(self.original)
            value['records'][0]['text'] = text
            self.write(value)
            self.assertEqual(self.find('homework'), [])

    def test_corrupt_utf8_json_nonfinite_and_large_file_fail_closed(self):
        for body in (b'\xff', b'{', b'{"version":NaN}', b'x'*(MAX_PACK_BYTES+1)):
            self.path.write_bytes(body)
            self.assertEqual(self.find('homework'), [])

    def test_hardlinked_asset_is_rejected(self):
        alias = self.root / 'alias.json'
        try:
            os.link(self.path, alias)
        except OSError:
            self.skipTest('Filesystem does not support this hardlink fixture')
        self.assertEqual(self.find('homework'), [])

    def test_ancestor_reparse_flag_is_rejected_before_open(self):
        real_lstat = Path.lstat
        def simulated(path, *args, **kwargs):
            if path == self.root:
                return SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=0x400)
            return real_lstat(path, *args, **kwargs)
        with patch.object(Path, 'lstat', simulated):
            self.assertEqual(self.find('homework'), [])

    def test_asset_changed_while_reading_is_rejected(self):
        real_open = Path.open
        target = self.path
        class ChangingReader:
            def __init__(self, actual): self.actual = actual
            def __enter__(self): return self
            def __exit__(self, *args): return self.actual.__exit__(*args)
            def fileno(self): return self.actual.fileno()
            def read(self, count):
                body = self.actual.read(count)
                with real_open(target, 'ab') as writer: writer.write(b' ')
                return body
        def open_fixture(path, mode='r', *args, **kwargs):
            actual = real_open(path, mode, *args, **kwargs)
            return ChangingReader(actual) if path == target and mode == 'rb' else actual
        with patch.object(Path, 'open', open_fixture):
            self.assertEqual(self.find('homework'), [])

    def test_validated_snapshot_does_not_auto_import_edits(self):
        helper = ApprovedKnowledge(root=self.root)
        original = helper.search('homework')
        self.path.write_text('{"raw":"unapproved"}', encoding='utf-8')
        self.assertEqual(helper.search('homework'), original)
        self.assertEqual(self.find('homework'), [])


if __name__ == '__main__':
    unittest.main()
