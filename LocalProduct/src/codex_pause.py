"""Keep Codex tasks running while its Windows interface stays minimized."""
import ctypes
from ctypes import wintypes
import json
import ntpath
import os
from pathlib import Path
import re
import tempfile

from activity_core import exclusive_lock, no_links
from paths import DEPLOY_ROOT
from study_windows import WindowInfo, WindowsDesktop


CODEX_PACKAGE = re.compile(r'openai\.codex_\d+(?:\.\d+){3}_(?:x64|arm64|x86)__2p2nqsd0c76g0\Z')
CODEX_WINDOW_CLASSES = frozenset({'Chrome_WidgetWin_1', 'WinUIDesktopWin32WindowClass'})


class CodexPause:
    def __init__(self, root=None, *, desktop=None):
        self.root = Path(root) if root is not None else DEPLOY_ROOT / 'data' / 'study'
        self.path = self.root / 'codex-pause.json'
        self.desktop = desktop
        self._api_ready = False

    def enabled(self):
        try:
            no_links(self.path, True)
            if self.path.stat().st_size > 2048:
                raise ValueError('Повреждены настройки сворачивания Codex')
            value = json.loads(self.path.read_text(encoding='utf-8-sig'))
        except FileNotFoundError:
            return False
        except (UnicodeError, json.JSONDecodeError) as error:
            raise ValueError('Повреждены настройки сворачивания Codex') from error
        if (not isinstance(value, dict) or set(value) != {'version', 'enabled'} or
                type(value['version']) is not int or value['version'] != 1 or
                type(value['enabled']) is not bool):
            raise ValueError('Повреждены настройки сворачивания Codex')
        return value['enabled']

    def set_enabled(self, enabled):
        if type(enabled) is not bool:
            raise ValueError('Выберите состояние сворачивания Codex')
        no_links(self.root)
        self.root.mkdir(parents=True, exist_ok=True)
        no_links(self.root)
        with exclusive_lock(self.root / 'codex-pause.lock'):
            no_links(self.path, self.path.exists())
            descriptor, temporary = tempfile.mkstemp(prefix='.codex-pause-', suffix='.tmp', dir=self.root)
            try:
                with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
                    json.dump({'version': 1, 'enabled': enabled}, stream, ensure_ascii=False)
                    stream.flush()
                    os.fsync(stream.fileno())
                no_links(self.path, self.path.exists())
                os.replace(temporary, self.path)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)

    @staticmethod
    def _codex_executable(executable):
        if not isinstance(executable, str) or not ntpath.isabs(executable):
            return False
        program_files = os.environ.get('ProgramFiles', '')
        if not program_files or not ntpath.isabs(program_files):
            return False
        path = ntpath.normcase(ntpath.normpath(executable))
        directory, filename = ntpath.split(path)
        package_directory, app_folder = ntpath.split(directory)
        installation_root, package = ntpath.split(package_directory)
        windows_apps = ntpath.normcase(ntpath.normpath(ntpath.join(program_files, 'WindowsApps')))
        return (filename == 'chatgpt.exe' and app_folder == 'app' and
                installation_root == windows_apps and CODEX_PACKAGE.fullmatch(package) is not None)

    def _backend(self):
        if self.desktop is None:
            self.desktop = WindowsDesktop()
        if not self._api_ready:
            functions = (
                (self.desktop.user32.IsIconic, [wintypes.HWND], wintypes.BOOL),
                (self.desktop.user32.GetWindowRect, [wintypes.HWND, ctypes.POINTER(wintypes.RECT)], wintypes.BOOL),
                (self.desktop.user32.ShowWindowAsync, [wintypes.HWND, ctypes.c_int], wintypes.BOOL),
                (self.desktop.user32.GetWindowLongW, [wintypes.HWND, ctypes.c_int], ctypes.c_long),
            )
            for function, arguments, result in functions:
                function.argtypes, function.restype = arguments, result
            self._api_ready = True
        return self.desktop

    def _window(self, handle):
        desktop = self.desktop
        user = desktop.user32
        if (not user.IsWindow(handle) or not user.IsWindowVisible(handle) or
                user.IsIconic(handle) or user.GetWindow(handle, 4) or
                user.GetWindowLongW(handle, -16) & 0x40000000):
            return None
        class_name = ctypes.create_unicode_buffer(256)
        if not user.GetClassNameW(handle, class_name, len(class_name)) or class_name.value not in CODEX_WINDOW_CLASSES:
            return None
        rect = wintypes.RECT()
        if not user.GetWindowRect(handle, ctypes.byref(rect)) or rect.right <= rect.left or rect.bottom <= rect.top:
            return None
        pid = wintypes.DWORD()
        if not user.GetWindowThreadProcessId(handle, ctypes.byref(pid)):
            return None
        if desktop._session(pid.value) != desktop.session_id:
            return None
        process = desktop._process(pid.value)
        if process is None:
            return None
        executable, started = process
        if not started or not self._codex_executable(executable):
            return None
        return WindowInfo(int(handle), pid.value, started, executable, class_name.value, '')

    def reconcile(self, active):
        if type(active) is not bool:
            raise ValueError('Выберите состояние режима уроков')
        enabled = self.enabled()
        result = {'enabled': enabled, 'minimized': 0, 'errors': 0}
        if not active or not enabled:
            return result
        desktop = self._backend()
        if not desktop.is_interactive_desktop():
            return result

        def minimize(handle, parameter):
            try:
                window = self._window(handle)
                if window is None:
                    return True
                current = self._window(handle)
                if (current is not None and current.identity == window.identity and
                        ntpath.normcase(current.executable) == ntpath.normcase(window.executable)):
                    if desktop.user32.ShowWindowAsync(handle, 6):
                        result['minimized'] += 1
                    else:
                        result['errors'] += 1
            except (OSError, ValueError):
                result['errors'] += 1
            return True

        callback = desktop._enum_callback(minimize)
        if not desktop.user32.EnumWindows(callback, 0):
            raise ctypes.WinError(ctypes.get_last_error())
        return result
