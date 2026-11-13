"""Persisted, local folder grants. Importing this module creates no files."""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile

from activity_core import exclusive_lock, no_links


MAX_ROOTS = 32
MAX_POLICY_BYTES = 64 * 1024
PROTECTED_PARTS = {
    '.git', '.codex', '.aws', '.ssh', 'node_modules', 'appdata',
    'onedrive', 'iclouddrive', 'icloudphotos', '.minecraft', 'saves',
    'backups', 'windows', 'program files', 'program files (x86)',
    'programdata', '$recycle.bin', 'system volume information', 'recovery',
    'google drive', 'googledrive', 'dropbox', 'nextcloud', 'worlds',
}


def path_key(value):
    """Normalize separators, dot segments and Windows case for comparisons."""
    return os.path.normcase(os.path.abspath(os.fspath(value)))


def contains(root, value):
    root_key, value_key = path_key(root), path_key(value)
    try:
        return os.path.commonpath((root_key, value_key)) == root_key
    except ValueError:
        return False


def local_volume(root):
    if os.name != 'nt':
        return True
    import ctypes
    get_type = ctypes.WinDLL('kernel32', use_last_error=True).GetDriveTypeW
    get_type.argtypes, get_type.restype = [ctypes.c_wchar_p], ctypes.c_uint
    return get_type(str(Path(root).anchor)) in (2, 3)


class FolderPolicy:
    def __init__(self, path, defaults, protected=(), deny_parts=()):
        self.path = Path(path).absolute()
        self.defaults = [Path(os.path.abspath(p)) for p in defaults]
        self.default_keys = {path_key(p) for p in self.defaults}
        self.protected = [Path(path_key(p)) for p in protected]
        self.deny_parts = PROTECTED_PARTS | {p.casefold() for p in deny_parts}
        self.profiles = [Path.home().absolute()]
        self._cached_stamp = None
        self._cached_roots = None
        for root in self.defaults:
            if root.name.casefold() in ('documents', 'downloads', 'desktop', 'pictures', 'music', 'videos'):
                self.profiles.append(root.parent)

    def _root(self, value, require_directory=False):
        if not isinstance(value, (str, os.PathLike)) or not str(value).strip():
            raise ValueError('Нужен абсолютный путь к папке')
        raw = Path(value)
        if not raw.is_absolute():
            raise ValueError('Нужен абсолютный путь к папке')
        root = Path(os.path.abspath(raw))
        if root == Path(root.anchor):
            raise ValueError('Весь диск разрешать нельзя; выберите отдельную папку')
        if os.name == 'nt' and root.drive.upper() not in ('C:', 'D:'):
            raise ValueError('Выберите локальную папку на диске C: или D:')
        if os.name == 'nt' and any(part.endswith((' ', '.')) or any(c in part for c in '<>:"|?*') for part in root.parts[1:]):
            raise ValueError('Неоднозначное имя папки')
        if any(part.casefold() in self.deny_parts or part.casefold().startswith(('onedrive - ', 'onedrive – ', '.env', 'credentials', 'secret', 'token')) for part in root.parts):
            raise ValueError('Системные папки, облака, приложения и сохранения защищены')
        if any(contains(root, profile) for profile in self.profiles):
            raise ValueError('Весь профиль пользователя разрешать нельзя')
        if os.name == 'nt' and root.parent.name.casefold() == 'users':
            raise ValueError('Весь профиль пользователя разрешать нельзя')
        if any(contains(p, root) for p in self.protected):
            raise ValueError('Рабочая или служебная папка приложения защищена')
        # Legacy Documents/Downloads grants retain their narrower per-file guards.
        if path_key(root) not in self.default_keys and any(contains(root, p) for p in self.protected):
            raise ValueError('Папка содержит защищённые данные; выберите вложенную папку')
        if require_directory:
            if not local_volume(root):
                raise ValueError('Выберите папку на локальном диске')
            no_links(root)
            if not root.is_dir():
                raise ValueError('Выбранная папка должна существовать')
            if root.resolve() != root:
                raise ValueError('Неоднозначный путь к папке')
        return root

    def _roots(self, values, require_new=False, previous=()):
        if not isinstance(values, list) or len(values) > MAX_ROOTS:
            raise ValueError('Разрешено не более 32 папок')
        old = {path_key(p) for p in previous}
        result, seen = [], set()
        for value in values:
            root = self._root(value)
            if require_new and path_key(root) not in old:
                root = self._root(value, require_directory=True)
            key = path_key(root)
            if key in seen:
                raise ValueError('Папка уже добавлена')
            seen.add(key)
            result.append(root)
        return result

    def load(self):
        try:
            no_links(self.path, True)
            info = self.path.stat()
            stamp = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
            if stamp == self._cached_stamp:
                return list(self._cached_roots)
            with self.path.open('r', encoding='utf-8-sig') as source:
                text = source.read(MAX_POLICY_BYTES + 1)
        except FileNotFoundError:
            return list(self.defaults)
        if len(text.encode('utf-8')) > MAX_POLICY_BYTES:
            raise ValueError('Файл разрешённых папок слишком большой')
        try:
            value = json.loads(text)
        except (ValueError, UnicodeError) as error:
            raise ValueError('Неверный файл разрешённых папок') from error
        if not isinstance(value, dict) or set(value) != {'version', 'roots'} or type(value['version']) is not int or value['version'] != 1:
            raise ValueError('Неверный формат разрешённых папок')
        if not isinstance(value['roots'], list) or any(not isinstance(p, str) for p in value['roots']):
            raise ValueError('Неверный список разрешённых папок')
        roots = self._roots(value['roots'])
        self._cached_stamp, self._cached_roots = stamp, roots
        return list(roots)

    def save(self, values):
        # The caller also holds Executor.lock, excluding any running transfer.
        no_links(self.path.parent)
        with exclusive_lock(self.path.with_suffix('.lock'), blocking=False):
            roots = self._roots(values, require_new=True, previous=self.load())
            no_links(self.path, self.path.exists())
            descriptor, temporary = tempfile.mkstemp(prefix='.folders-', suffix='.tmp', dir=self.path.parent)
            try:
                with os.fdopen(descriptor, 'w', encoding='utf-8') as target:
                    json.dump({'version': 1, 'roots': [str(p) for p in roots]}, target, ensure_ascii=False, indent=2)
                    target.flush()
                    os.fsync(target.fileno())
                no_links(self.path, self.path.exists())
                os.replace(temporary, self.path)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
        return roots
