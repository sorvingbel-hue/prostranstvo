"""Local ZIP archives with a verified manifest and a searchable SQLite index."""
from __future__ import annotations

from contextlib import ExitStack, contextmanager
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import uuid
import zipfile
import zlib

from activity_core import exclusive_lock, no_links
from engine import fingerprint, locked_file, mark_delete, no_alternate_streams, native_kernel
from paths import DEPLOY_ROOT

MAX_FILES = 100
MAX_BYTES = 2 * 1024 ** 3
MAX_MANIFEST = 16 * 1024 ** 2
SAMPLE_BYTES = 64 * 1024
TEXT_EXTENSIONS = {'.txt', '.csv', '.json', '.log', '.md', '.py', '.xml', '.yaml', '.yml',
                   '.html', '.css', '.js', '.ts', '.sql', '.ini', '.toml', '.rst', '.srt', '.vtt'}
ARCHIVE_NAME = re.compile(r'archive-([0-9a-f]{32})\.zip\Z')
HEX_HASH = re.compile(r'[0-9a-f]{64}\Z')
RESERVED_NAMES = {'con', 'prn', 'aux', 'nul', *(f'com{i}' for i in range(1, 10)),
                  *(f'lpt{i}' for i in range(1, 10))}


def _utc():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _name(value):
    if (not isinstance(value, str) or not value or value in ('.', '..') or
            any(char in value for char in '/\\:<>"|?*') or any(ord(char) < 32 for char in value) or
            value[-1] in '. ' or value.split('.')[0].casefold() in RESERVED_NAMES):
        raise ValueError('Небезопасное имя файла в архиве')
    return value


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Повторяющееся поле manifest')
        result[key] = value
    return result


def _stamp(path):
    value = fingerprint(path)
    value['device'] = path.stat().st_dev
    return value


def _stream_stamp(stream):
    value = os.fstat(stream.fileno())
    return {'size': value.st_size, 'mtime_ns': value.st_mtime_ns,
            'ino': value.st_ino, 'device': value.st_dev}


def _hash_stream(stream):
    result = hashlib.sha256()
    while True:
        chunk = stream.read(1024 ** 2)
        if not chunk:
            break
        result.update(chunk)
    return result.hexdigest()


def _checked_handle(stream, expected):
    """Check the opened Windows object, not only a path checked before open."""
    value = os.fstat(stream.fileno())
    if not stat.S_ISREG(value.st_mode) or value.st_nlink != 1:
        raise ValueError('Требуется обычный файл без hardlink')
    if os.name != 'nt':
        return
    import ctypes
    import msvcrt
    from ctypes import wintypes
    kernel = native_kernel()
    kernel.GetFinalPathNameByHandleW.argtypes = [wintypes.HANDLE, wintypes.LPWSTR,
                                               wintypes.DWORD, wintypes.DWORD]
    kernel.GetFinalPathNameByHandleW.restype = wintypes.DWORD
    output = ctypes.create_unicode_buffer(32768)
    count = kernel.GetFinalPathNameByHandleW(msvcrt.get_osfhandle(stream.fileno()), output, len(output), 0)
    if not count or count >= len(output):
        raise OSError('Не удалось проверить путь открытого файла')
    actual = output.value
    if actual.startswith('\\\\?\\UNC\\'):
        actual = '\\\\' + actual[8:]
    elif actual.startswith('\\\\?\\'):
        actual = actual[4:]
    if os.path.normcase(actual) != os.path.normcase(str(expected.absolute())):
        raise ValueError('Открытый файл находится по другому пути')


@contextmanager
def _stable_parents(path):
    """Keep parent directories from being renamed or replaced during Windows IO."""
    no_links(path)
    if os.name != 'nt':
        yield
        return
    import ctypes
    kernel = native_kernel()
    handles = []
    try:
        for parent in reversed(path.parents):
            if not parent.exists():
                continue
            handle = kernel.CreateFileW(str(parent), 0x80000000, 3, None, 3, 0x02200000, None)
            if handle == ctypes.c_void_p(-1).value:
                raise ctypes.WinError(ctypes.get_last_error())
            handles.append(handle)
            no_links(parent)
        no_links(path)
        yield
    finally:
        for handle in reversed(handles):
            kernel.CloseHandle(handle)


def _publish(partial, final):
    if os.name == 'nt':
        os.rename(partial, final)  # Windows rename refuses to replace an existing target.
    else:
        os.link(partial, final)
        partial.unlink()


def _remove_owned(path, identity):
    """Rollback only the exact newly created object, never a replacement file."""
    try:
        no_links(path, True)
        with locked_file(path, delete=True) as stream:
            value = _stream_stamp(stream)
            if (value['ino'], value['device']) != identity:
                return
            if os.name == 'nt':
                mark_delete(stream, True)
            else:
                path.unlink()
    except (OSError, ValueError):
        pass


class ArchiveStore:
    def __init__(self, engine, root=None):
        self.engine = engine
        self.root = Path(root).absolute() if root is not None else DEPLOY_ROOT / 'data' / 'archives'
        no_links(self.root)
        self.root.mkdir(parents=True, exist_ok=True)
        no_links(self.root)
        self.objects = self.root / 'objects'
        no_links(self.objects)
        self.objects.mkdir(exist_ok=True)
        no_links(self.objects)
        self.db = self.root / 'index.sqlite'
        with self._connect() as connection:
            connection.executescript('''
CREATE TABLE IF NOT EXISTS archives(
 id TEXT PRIMARY KEY, path TEXT NOT NULL, sha256 TEXT NOT NULL, bytes INTEGER NOT NULL,
 created_utc TEXT NOT NULL, original_bytes INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS entries(
 id TEXT PRIMARY KEY, archive_id TEXT NOT NULL REFERENCES archives(id),
 name TEXT NOT NULL, source TEXT NOT NULL, member TEXT NOT NULL,
 bytes INTEGER NOT NULL, sha256 TEXT NOT NULL, mtime_ns INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS entries_name ON entries(name);
''')

    @contextmanager
    def _connect(self):
        for path in (self.db, Path(str(self.db) + '-journal'), Path(str(self.db) + '-wal'),
                     Path(str(self.db) + '-shm')):
            no_links(path, path.exists())
        connection = sqlite3.connect(self.db, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.create_function('CASEFOLD', 1, lambda value: value.casefold())
        connection.execute('PRAGMA foreign_keys=ON')
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    @contextmanager
    def _source(self, value, expected=None):
        path = self.engine.checked(value)
        if path.is_relative_to(self.root):
            raise ValueError('Служебная папка архивов защищена')
        no_links(path, True)
        no_alternate_streams(path)
        before = _stamp(path)
        if expected is not None and before != expected:
            raise ValueError('Файл изменился после подготовки плана: ' + str(path))
        with _stable_parents(path), locked_file(path) as stream:
            _checked_handle(stream, path)
            no_alternate_streams(path)
            if _stream_stamp(stream) != before:
                raise ValueError('Источник был заменён во время открытия')
            no_links(path, True)
            if _stamp(path) != before:
                raise ValueError('Источник изменился во время открытия')
            yield path, stream, before
            self.engine.checked(path)
            no_links(path, True)
            no_alternate_streams(path)
            if _stream_stamp(stream) != before or _stamp(path) != before:
                raise ValueError('Источник менялся во время архивации: ' + str(path))

    def propose(self, source):
        skipped = 0
        if isinstance(source, (str, Path)):
            folder = self.engine.checked(source, True)
            rows, skipped = self.engine.scan(folder, None, max_files=MAX_FILES)
            paths = [row['path'] for row in rows if Path(row['path']).suffix.casefold() in TEXT_EXTENSIONS]
        elif isinstance(source, list):
            paths = source
        else:
            raise ValueError('Выберите папку или отдельные файлы')
        if not 1 <= len(paths) <= MAX_FILES:
            raise ValueError('Выберите от 1 до 100 подходящих файлов')
        files = []
        seen = set()
        total = 0
        estimate = 0
        for value in paths:
            with self._source(value) as (path, stream, stamp):
                key = os.path.normcase(str(path))
                if key in seen:
                    raise ValueError('Файл повторяется в плане')
                seen.add(key)
                total += stamp['size']
                if total > MAX_BYTES:
                    raise ValueError('Первая версия поддерживает до 2 ГиБ за один архив')
                sample = stream.read(SAMPLE_BYTES)
                result = hashlib.sha256(sample)
                while True:
                    chunk = stream.read(1024 ** 2)
                    if not chunk:
                        break
                    result.update(chunk)
                ratio = len(zlib.compress(sample)) / len(sample) if sample else 1
                estimate += int(stamp['size'] * ratio) + 256
                name = _name(path.name)
                files.append({'source': str(path), 'name': name, 'member': f'files/{len(files):03d}/{name}',
                              'size': stamp['size'], 'stamp': stamp, 'sha256': result.hexdigest()})
        return {'version': 1, 'id': uuid.uuid4().hex, 'created_utc': _utc(), 'files': files,
                'total_bytes': total, 'estimated_archive_bytes': estimate + 4096,
                'skipped': skipped, 'free_bytes': shutil.disk_usage(self.objects).free}

    def select(self, plan, indexes):
        """Keep the reviewed fingerprints when selecting part of a proposal."""
        self._validate_plan(plan)
        if not isinstance(indexes, list) or not indexes or len(set(indexes)) != len(indexes):
            raise ValueError('Выберите отдельные строки плана')
        if any(type(index) is not int or not 0 <= index < len(plan['files']) for index in indexes):
            raise ValueError('Неверная строка плана')
        chosen = []
        for index in indexes:
            row = dict(plan['files'][index])
            row['member'] = f"files/{len(chosen):03d}/{row['name']}"
            chosen.append(row)
        value = dict(plan, id=uuid.uuid4().hex, files=chosen)
        value['total_bytes'] = sum(row['size'] for row in chosen)
        value['estimated_archive_bytes'] = None
        return value

    @staticmethod
    def _validate_plan(plan):
        if not isinstance(plan, dict) or plan.get('version') != 1 or not re.fullmatch(r'[0-9a-f]{32}', str(plan.get('id', ''))):
            raise ValueError('Неверный план архивации')
        files = plan.get('files')
        if not isinstance(files, list) or not 1 <= len(files) <= MAX_FILES:
            raise ValueError('Неверный список файлов')
        total = 0
        sources = set()
        for index, row in enumerate(files):
            if not isinstance(row, dict) or set(row) != {'source', 'name', 'member', 'size', 'stamp', 'sha256'}:
                raise ValueError('Неверная запись плана')
            name = _name(row['name'])
            if row['member'] != f'files/{index:03d}/{name}' or type(row['size']) is not int or row['size'] < 0:
                raise ValueError('Неверное содержимое плана')
            if not isinstance(row['source'], str) or not Path(row['source']).is_absolute():
                raise ValueError('Требуется полный путь источника')
            source = os.path.normcase(row['source'])
            if source in sources:
                raise ValueError('Повтор источника')
            sources.add(source)
            if not isinstance(row['sha256'], str) or not HEX_HASH.fullmatch(row['sha256']):
                raise ValueError('Неверная контрольная сумма')
            stamp = row['stamp']
            if not isinstance(stamp, dict) or set(stamp) != {'size', 'mtime_ns', 'ino', 'device'} or stamp['size'] != row['size']:
                raise ValueError('Неверный отпечаток источника')
            if any(type(value) is not int or value < 0 for value in stamp.values()):
                raise ValueError('Неверный отпечаток источника')
            total += row['size']
        if total > MAX_BYTES or plan.get('total_bytes') != total:
            raise ValueError('Неверный размер плана')

    @staticmethod
    def _manifest(plan):
        return {'format': 'prostranstvo-archive', 'version': 1, 'archive_id': plan['id'],
                'created_utc': plan['created_utc'], 'files': [
                    {key: row[key] for key in ('source', 'name', 'member', 'size', 'sha256')} |
                    {'mtime_ns': row['stamp']['mtime_ns']} for row in plan['files']]}

    @staticmethod
    def _validate_manifest(manifest, archive_id):
        if not isinstance(manifest, dict) or set(manifest) != {'format', 'version', 'archive_id', 'created_utc', 'files'}:
            raise ValueError('Неверный manifest архива')
        if manifest['format'] != 'prostranstvo-archive' or manifest['version'] != 1 or manifest['archive_id'] != archive_id:
            raise ValueError('Неподдерживаемый формат архива')
        try:
            stamp = datetime.datetime.fromisoformat(manifest['created_utc'])
            if stamp.utcoffset() != datetime.timedelta():
                raise ValueError('Требуется время UTC')
        except (TypeError, ValueError):
            raise ValueError('Неверное время архива') from None
        rows = manifest['files']
        if not isinstance(rows, list) or not 1 <= len(rows) <= MAX_FILES:
            raise ValueError('Неверное количество файлов')
        total = 0
        for index, row in enumerate(rows):
            if not isinstance(row, dict) or set(row) != {'source', 'name', 'member', 'size', 'sha256', 'mtime_ns'}:
                raise ValueError('Неверная запись manifest')
            name = _name(row['name'])
            if row['member'] != f'files/{index:03d}/{name}':
                raise ValueError('Небезопасный путь в архиве')
            if not isinstance(row['source'], str) or not row['source'] or '\x00' in row['source']:
                raise ValueError('Неверный путь источника в manifest')
            if type(row['size']) is not int or row['size'] < 0 or type(row['mtime_ns']) is not int or row['mtime_ns'] < 0:
                raise ValueError('Неверный размер файла в manifest')
            if not isinstance(row['sha256'], str) or not HEX_HASH.fullmatch(row['sha256']):
                raise ValueError('Неверная контрольная сумма в manifest')
            total += row['size']
        if total > MAX_BYTES:
            raise ValueError('Архив превышает 2 ГиБ распакованных данных')
        return total

    @contextmanager
    def _verified_archive(self, path, archive_id, expected_hash=None):
        no_links(path, True)
        no_alternate_streams(path)
        with _stable_parents(path), locked_file(path) as stream:
            _checked_handle(stream, path)
            no_alternate_streams(path)
            before = _stream_stamp(stream)
            if before != _stamp(path) or before['size'] > MAX_BYTES + MAX_MANIFEST + 1024 ** 2:
                raise ValueError('Архив был заменён')
            archive_hash = _hash_stream(stream)
            if expected_hash is not None and archive_hash != expected_hash:
                raise ValueError('Контрольная сумма архива изменилась')
            stream.seek(0)
            with zipfile.ZipFile(stream) as archive:
                infos = archive.infolist()
                names = [info.filename for info in infos]
                if len(names) > MAX_FILES + 1 or len(set(name.casefold() for name in names)) != len(names):
                    raise ValueError('Повторяющиеся имена или слишком большой архив')
                if 'manifest.json' not in names:
                    raise ValueError('В архиве отсутствует manifest')
                for info in infos:
                    file_type = stat.S_IFMT(info.external_attr >> 16)
                    if (file_type not in (0, stat.S_IFREG) or info.flag_bits & 1 or
                            info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED) or
                            info.file_size > max(info.compress_size, 1) * 10000):
                        raise ValueError('Неподдерживаемая или небезопасная запись ZIP')
                manifest_info = archive.getinfo('manifest.json')
                if manifest_info.file_size > MAX_MANIFEST:
                    raise ValueError('Слишком большой manifest')
                manifest = json.loads(archive.read('manifest.json'), object_pairs_hook=_unique_object)
                total = self._validate_manifest(manifest, archive_id)
                if set(names) != {'manifest.json', *(row['member'] for row in manifest['files'])}:
                    raise ValueError('Содержимое архива отличается от manifest')
                for row in manifest['files']:
                    info = archive.getinfo(row['member'])
                    if info.file_size != row['size']:
                        raise ValueError('Размер записи отличается от manifest')
                    with archive.open(info) as member:
                        if _hash_stream(member) != row['sha256']:
                            raise ValueError('Повреждён файл в архиве')
                yield archive, manifest, archive_hash, before['size'], total
            no_links(path, True)
            if _stream_stamp(stream) != before or _stamp(path) != before:
                raise ValueError('Архив менялся во время проверки')

    def _index(self, path, manifest, archive_hash, archive_bytes, total):
        archive_id = manifest['archive_id']
        with self._connect() as connection:
            existing = connection.execute('SELECT sha256 FROM archives WHERE id=?', (archive_id,)).fetchone()
            if existing is not None and existing['sha256'] != archive_hash:
                raise ValueError('В каталоге уже есть другой архив с этим ID')
            connection.execute('''INSERT INTO archives VALUES(?,?,?,?,?,?) ON CONFLICT(id)
DO UPDATE SET path=excluded.path,bytes=excluded.bytes''',
                               (archive_id, str(path), archive_hash, archive_bytes, manifest['created_utc'], total))
            connection.execute('DELETE FROM entries WHERE archive_id=?', (archive_id,))
            for index, row in enumerate(manifest['files']):
                connection.execute('INSERT INTO entries VALUES(?,?,?,?,?,?,?,?)',
                                   (f'{archive_id}:{index}', archive_id, row['name'], row['source'], row['member'],
                                    row['size'], row['sha256'], row['mtime_ns']))

    def create(self, plan):
        self._validate_plan(plan)
        archive_id = plan['id']
        final = self.objects / f'archive-{archive_id}.zip'
        partial = self.objects / f'.archive-{archive_id}-{uuid.uuid4().hex}.partial'
        manifest = self._manifest(plan)
        body = json.dumps(manifest, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
        if len(body) > MAX_MANIFEST:
            raise ValueError('План содержит слишком длинные пути')
        with self.engine.lock(), exclusive_lock(self.root / 'archive.lock'), _stable_parents(final):
            no_links(self.objects)
            if final.exists():
                with self._verified_archive(final, archive_id) as (_, actual, archive_hash, size, total):
                    if actual != manifest:
                        raise ValueError('С этим ID уже существует другой архив')
                    self._index(final, actual, archive_hash, size, total)
                    return {'id': archive_id, 'path': str(final), 'bytes': size, 'original_bytes': total, 'sha256': archive_hash}
            if shutil.disk_usage(self.objects).free < plan['total_bytes'] + len(body) + 10 * 1024 ** 2:
                raise ValueError('Недостаточно места для нового архива и проверки')
            partial_identity = None
            try:
                with partial.open('xb') as output:
                    stamp = _stream_stamp(output)
                    partial_identity = (stamp['ino'], stamp['device'])
                    _checked_handle(output, partial)
                    with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED, compresslevel=6, allowZip64=True) as archive:
                        for row in plan['files']:
                            with self._source(row['source'], row['stamp']) as (_, stream, __):
                                result = hashlib.sha256()
                                with archive.open(row['member'], 'w', force_zip64=True) as member:
                                    while True:
                                        chunk = stream.read(1024 ** 2)
                                        if not chunk:
                                            break
                                        result.update(chunk)
                                        member.write(chunk)
                                if result.hexdigest() != row['sha256']:
                                    raise ValueError('Файл изменился после подготовки плана')
                        archive.writestr('manifest.json', body, compress_type=zipfile.ZIP_STORED)
                    output.flush()
                    os.fsync(output.fileno())
                with self._verified_archive(partial, archive_id) as (_, actual, archive_hash, size, total):
                    if actual != manifest:
                        raise ValueError('Ошибка проверки manifest')
                _publish(partial, final)
                self._index(final, manifest, archive_hash, size, total)
                return {'id': archive_id, 'path': str(final), 'bytes': size, 'original_bytes': total, 'sha256': archive_hash}
            finally:
                if partial_identity is not None:
                    _remove_owned(partial, partial_identity)

    def recover(self):
        recovered = 0
        errors = []
        with exclusive_lock(self.root / 'archive.lock'), _stable_parents(self.objects / 'guard'):
            for path in sorted(self.objects.glob('archive-*.zip')):
                match = ARCHIVE_NAME.fullmatch(path.name)
                if match is None:
                    continue
                try:
                    with self._verified_archive(path, match.group(1)) as (_, manifest, archive_hash, size, total):
                        self._index(path, manifest, archive_hash, size, total)
                    recovered += 1
                except (OSError, ValueError, RuntimeError, zipfile.BadZipFile, sqlite3.Error, zlib.error) as error:
                    errors.append({'archive': path.name, 'error': str(error)})
        return {'indexed_archives': recovered, 'errors': errors}

    def search(self, query='', limit=100):
        if not isinstance(query, str) or len(query) > 1000 or type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError('Неверный запрос поиска')
        with self._connect() as connection:
            rows = connection.execute('''SELECT e.*, a.path AS archive_path FROM entries e
JOIN archives a ON a.id=e.archive_id WHERE instr(CASEFOLD(e.name),CASEFOLD(?))>0
 OR instr(CASEFOLD(e.source),CASEFOLD(?))>0 ORDER BY a.created_utc DESC,e.name LIMIT ?''',
                                      (query, query, limit)).fetchall()
        return [dict(row) for row in rows]

    def restore(self, entry_ids, destination):
        if not isinstance(entry_ids, list) or not 1 <= len(entry_ids) <= MAX_FILES or len(set(entry_ids)) != len(entry_ids):
            raise ValueError('Выберите до 100 разных записей архива')
        with self.engine.lock(), exclusive_lock(self.root / 'archive.lock'):
            folder = self.engine.checked(destination, True)
            if not folder.is_dir() or folder.is_relative_to(self.root):
                raise ValueError('Выберите существующую папку восстановления')
            rows = []
            with self._connect() as connection:
                for entry_id in entry_ids:
                    if not isinstance(entry_id, str) or not re.fullmatch(r'[0-9a-f]{32}:[0-9]{1,2}', entry_id):
                        raise ValueError('Неверный ID записи')
                    row = connection.execute('''SELECT e.*,a.sha256 AS archive_sha256 FROM entries e
JOIN archives a ON a.id=e.archive_id WHERE e.id=?''', (entry_id,)).fetchone()
                    if row is None:
                        raise ValueError('Запись отсутствует в каталоге')
                    rows.append(dict(row))
            created = []
            try:
                with ExitStack() as stack:
                    stack.enter_context(_stable_parents(folder / 'guard'))
                    targets = []
                    names = set()
                    opened = {}
                    for row in rows:
                        name = _name(row['name'])
                        target = self.engine.checked(folder / name)
                        if name.casefold() in names or os.path.lexists(target):
                            raise ValueError('Коллизия при восстановлении: ' + name)
                        names.add(name.casefold())
                        targets.append(target)
                        archive_id = row['archive_id']
                        if archive_id not in opened:
                            path = self.objects / f'archive-{archive_id}.zip'
                            opened[archive_id] = stack.enter_context(self._verified_archive(path, archive_id, row['archive_sha256']))
                        _, manifest, _, _, __ = opened[archive_id]
                        index = int(row['id'].split(':')[1])
                        if index >= len(manifest['files']):
                            raise ValueError('Каталог не соответствует manifest')
                        actual = manifest['files'][index]
                        if any(actual[key] != row[column] for key, column in
                               (('name', 'name'), ('source', 'source'), ('member', 'member'), ('size', 'bytes'), ('sha256', 'sha256'))):
                            raise ValueError('Каталог не соответствует manifest')
                    total = sum(row['bytes'] for row in rows)
                    if total > MAX_BYTES or shutil.disk_usage(folder).free < total + 10 * 1024 ** 2:
                        raise ValueError('Недостаточно места для восстановления')
                    for row, target in zip(rows, targets):
                        self.engine.checked(target)
                        partial = self.engine.checked(folder / ('.archive-restore-' + uuid.uuid4().hex + '.partial'))
                        result = hashlib.sha256()
                        count = 0
                        with partial.open('xb') as output:
                            stamp = _stream_stamp(output)
                            identity = (stamp['ino'], stamp['device'])
                            created.append((partial, identity))
                            _checked_handle(output, partial)
                            archive = opened[row['archive_id']][0]
                            with archive.open(row['member']) as member:
                                while True:
                                    chunk = member.read(1024 ** 2)
                                    if not chunk:
                                        break
                                    count += len(chunk)
                                    if count > row['bytes']:
                                        raise ValueError('Запись превышает заявленный размер')
                                    result.update(chunk)
                                    output.write(chunk)
                            output.flush()
                            os.fsync(output.fileno())
                        if count != row['bytes'] or result.hexdigest() != row['sha256']:
                            raise ValueError('Контрольная сумма восстановленного файла не совпала')
                        self.engine.checked(target)
                        no_links(partial, True)
                        _publish(partial, target)
                        created.append((target, identity))
                        self.engine.checked(target)
                        no_links(target, True)
                return [str(path) for path in targets]
            except Exception:
                for path, identity in reversed(created):
                    _remove_owned(path, identity)
                raise
