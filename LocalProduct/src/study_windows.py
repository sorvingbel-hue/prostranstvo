"""Read Windows application windows and request their ordinary close action.

The backend works only in the calling process's interactive Windows session.
Owned dialogs, disabled main windows and Windows shell components are excluded.
The Roblox fallback is restricted to a revalidated game in its installation path.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass
import json
import ntpath
import os
from pathlib import Path
import re


@dataclass(frozen=True)
class WindowInfo:
    handle: int
    pid: int
    process_started: int
    executable: str
    class_name: str
    title: str

    @property
    def identity(self):
        return self.handle, self.pid, self.process_started, self.class_name


PROTECTED_PROGRAMS = frozenset({
    'codex.exe', 'codex-desktop.exe', 'codex-computer-use.exe', 'chatgpt.exe', 'openai.codex.exe',
    'windowscompanion.exe', 'prostranstvo.exe', 'prostranstvolocalbeta.exe',
    'prostranstvocore.exe', 'prostranstvobroker.exe', 'pashamessage.exe',
    'familymessages.exe', 'activityagent.exe',
    'kittenmessenger.exe', 'nativedesktoppet.exe',
    'dwm.exe', 'winlogon.exe', 'wininit.exe', 'csrss.exe', 'lsass.exe',
    'services.exe', 'svchost.exe', 'sihost.exe', 'taskhostw.exe',
    'shellexperiencehost.exe', 'startmenuexperiencehost.exe',
    'searchhost.exe', 'searchapp.exe', 'applicationframehost.exe',
    'runtimebroker.exe', 'securityhealthsystray.exe',
})
EXPLORER_CLASSES = frozenset({'CabinetWClass', 'ExploreWClass'})
PROTECTED_CLASSES = frozenset({
    'Progman', 'WorkerW', 'Shell_TrayWnd', 'Shell_SecondaryTrayWnd',
    'Windows.UI.Core.CoreWindow', 'ApplicationFrameWindow', '#32770',
})
PLAN_WINDOW_TITLE = 'Пространство · План на сегодня'
CONTROL_TITLES = frozenset({
    'Пространство · Уроки', 'Пространство · Время уроков',
    PLAN_WINDOW_TITLE,
    'Пространство · Помощник компьютера', 'Пространство · Local Beta',
    'Мурчик · Написать папе',
})


def candidate_allowed(window, protected_pids, windows_directory):
    """Pure classification helper, also used when revalidating a close."""
    if window.pid <= 0 or window.pid == os.getpid():
        return False
    if not window.process_started or not window.executable:
        return False
    executable = ntpath.normcase(ntpath.normpath(window.executable))
    program = ntpath.basename(executable)
    if program in PROTECTED_PROGRAMS or window.class_name in PROTECTED_CLASSES:
        return False
    if window.class_name == 'TkTopLevel' and window.title in CONTROL_TITLES:
        return False
    if program == 'explorer.exe':
        expected = ntpath.normcase(ntpath.join(windows_directory, 'explorer.exe'))
        # Explorer can be our launcher ancestor and also own folder windows.
        # Only its verified folder classes may pass the ancestor exclusion.
        return executable == expected and window.class_name in EXPLORER_CLASSES
    if window.pid in protected_pids:
        return False
    windows = ntpath.normcase(ntpath.normpath(windows_directory)).rstrip('\\')
    if executable == windows or executable.startswith(windows + '\\'):
        return False
    return True


class WindowsDesktop:
    """Conservative per-session adapter. Construction does not close windows."""

    def __init__(self, protected_pids=()):
        if os.name != 'nt':
            raise OSError('Режим «Уроки» поддерживает Windows')
        self.user32 = ctypes.WinDLL('user32', use_last_error=True)
        self.kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
        self._configure_api()
        self.session_id = self._session(os.getpid())
        if self.session_id is None or self.session_id == 0:
            raise OSError('Требуется интерактивный сеанс Windows')
        self.protected_pids = set(protected_pids) | self._ancestors() | {os.getpid()}
        buffer = ctypes.create_unicode_buffer(32768)
        length = self.kernel32.GetWindowsDirectoryW(buffer, len(buffer))
        if not length or length >= len(buffer):
            raise ctypes.WinError(ctypes.get_last_error())
        self.windows_directory = buffer.value

    def _configure_api(self):
        self._enum_callback = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        functions = (
            (self.user32.EnumWindows, [self._enum_callback, wintypes.LPARAM], wintypes.BOOL),
            (self.user32.IsWindow, [wintypes.HWND], wintypes.BOOL),
            (self.user32.IsWindowVisible, [wintypes.HWND], wintypes.BOOL),
            (self.user32.IsWindowEnabled, [wintypes.HWND], wintypes.BOOL),
            (self.user32.GetWindow, [wintypes.HWND, wintypes.UINT], wintypes.HWND),
            (self.user32.GetWindowThreadProcessId, [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)], wintypes.DWORD),
            (self.user32.GetClassNameW, [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int], ctypes.c_int),
            (self.user32.GetWindowTextW, [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int], ctypes.c_int),
            (self.user32.PostMessageW, [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM], wintypes.BOOL),
            (self.kernel32.GetWindowsDirectoryW, [wintypes.LPWSTR, wintypes.UINT], wintypes.UINT),
            (self.kernel32.ProcessIdToSessionId, [wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)], wintypes.BOOL),
            (self.kernel32.OpenProcess, [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
            (self.kernel32.CloseHandle, [wintypes.HANDLE], wintypes.BOOL),
            (self.kernel32.QueryFullProcessImageNameW, [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)], wintypes.BOOL),
            (self.kernel32.GetProcessTimes, [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4, wintypes.BOOL),
            (self.kernel32.TerminateProcess, [wintypes.HANDLE, wintypes.UINT], wintypes.BOOL),
        )
        for function, arguments, result in functions:
            function.argtypes = arguments
            function.restype = result

    def _session(self, pid):
        session = wintypes.DWORD()
        if self.kernel32.ProcessIdToSessionId(pid, ctypes.byref(session)):
            return session.value
        return None

    def _ancestors(self):
        """Protect the host terminal and agent parents without a name-wide block."""
        class ProcessEntry(ctypes.Structure):
            _fields_ = [
                ('dwSize', wintypes.DWORD), ('cntUsage', wintypes.DWORD),
                ('th32ProcessID', wintypes.DWORD), ('th32DefaultHeapID', ctypes.c_size_t),
                ('th32ModuleID', wintypes.DWORD), ('cntThreads', wintypes.DWORD),
                ('th32ParentProcessID', wintypes.DWORD), ('pcPriClassBase', wintypes.LONG),
                ('dwFlags', wintypes.DWORD), ('szExeFile', wintypes.WCHAR * 260),
            ]
        snapshot_function = self.kernel32.CreateToolhelp32Snapshot
        snapshot_function.argtypes = [wintypes.DWORD, wintypes.DWORD]
        snapshot_function.restype = wintypes.HANDLE
        first = self.kernel32.Process32FirstW
        next_entry = self.kernel32.Process32NextW
        for function in (first, next_entry):
            function.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry)]
            function.restype = wintypes.BOOL
        snapshot = snapshot_function(0x00000002, 0)
        if snapshot == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        parents = {}
        try:
            entry = ProcessEntry()
            entry.dwSize = ctypes.sizeof(entry)
            more = first(snapshot, ctypes.byref(entry))
            if not more:
                raise ctypes.WinError(ctypes.get_last_error())
            while more:
                parents[entry.th32ProcessID] = entry.th32ParentProcessID
                more = next_entry(snapshot, ctypes.byref(entry))
        finally:
            self.kernel32.CloseHandle(snapshot)
        protected = set()
        pid = os.getpid()
        while pid and pid not in protected:
            protected.add(pid)
            pid = parents.get(pid, 0)
        return protected

    def _process(self, pid):
        if self._session(pid) != self.session_id:
            return None
        process = self.kernel32.OpenProcess(0x1000, False, pid)
        if not process:
            return None
        try:
            return self._process_details(process)
        finally:
            self.kernel32.CloseHandle(process)

    def _process_details(self, process):
        executable = ctypes.create_unicode_buffer(32768)
        size = wintypes.DWORD(len(executable))
        if not self.kernel32.QueryFullProcessImageNameW(process, 0, executable, ctypes.byref(size)):
            return None
        started, ended, kernel, user = (wintypes.FILETIME() for _ in range(4))
        if not self.kernel32.GetProcessTimes(process, ctypes.byref(started), ctypes.byref(ended), ctypes.byref(kernel), ctypes.byref(user)):
            return None
        creation_time = (started.dwHighDateTime << 32) | started.dwLowDateTime
        return executable.value, creation_time

    @staticmethod
    def _roblox_installation(executable):
        local = os.environ.get('LOCALAPPDATA', '')
        if not local or not ntpath.isabs(local):
            return False
        path = ntpath.normcase(ntpath.normpath(executable))
        directory, filename = ntpath.split(path)
        root, version = ntpath.split(directory)
        expected = ntpath.normcase(ntpath.normpath(ntpath.join(local, 'Roblox', 'Versions')))
        return (filename == 'robloxplayerbeta.exe' and root == expected and
                re.fullmatch(r'version-[0-9a-f]{16,64}', version) is not None)

    def _window(self, handle):
        if not self.user32.IsWindow(handle) or not self.user32.IsWindowVisible(handle):
            return None
        if not self.user32.IsWindowEnabled(handle) or self.user32.GetWindow(handle, 4):
            return None
        pid = wintypes.DWORD()
        if not self.user32.GetWindowThreadProcessId(handle, ctypes.byref(pid)):
            return None
        if pid.value == os.getpid():
            return None
        process = self._process(pid.value)
        if process is None:
            return None
        executable, started = process
        class_name = ctypes.create_unicode_buffer(256)
        if not self.user32.GetClassNameW(handle, class_name, len(class_name)):
            return None
        title = ctypes.create_unicode_buffer(1024)
        self.user32.GetWindowTextW(handle, title, len(title))
        window = WindowInfo(int(handle), pid.value, started, executable, class_name.value, title.value)
        if candidate_allowed(window, self.protected_pids, self.windows_directory):
            return window
        return None

    def is_interactive_desktop(self):
        from activity_core import desktop_state
        return bool(desktop_state()[0])

    def list_windows(self):
        if not self.is_interactive_desktop():
            return []
        windows = []
        errors = []
        def collect(handle, parameter):
            try:
                window = self._window(handle)
                if window is not None:
                    windows.append(window)
            except Exception as error:
                errors.append(error)
            return True
        callback = self._enum_callback(collect)
        if not self.user32.EnumWindows(callback, 0):
            raise ctypes.WinError(ctypes.get_last_error())
        if errors:
            raise OSError('Не удалось проверить принадлежность окон') from errors[0]
        return windows

    def close_window(self, window):
        if not isinstance(window, WindowInfo):
            raise TypeError('Требуется проверенное окно Windows')
        if not self.is_interactive_desktop():
            return 'unavailable'
        if not self.user32.IsWindow(window.handle):
            return 'gone'
        current = self._window(window.handle)
        if current is None:
            return 'unavailable'
        same_executable = ntpath.normcase(current.executable) == ntpath.normcase(window.executable)
        if current.identity != window.identity or not same_executable:
            return 'changed'
        if self.user32.PostMessageW(window.handle, 0x0010, 0, 0):
            return 'requested'
        return 'unavailable'

    def terminate_roblox(self, window):
        """End only the exact game after the caller has exhausted close requests."""
        if not isinstance(window, WindowInfo):
            raise TypeError('Требуется проверенное окно Windows')
        if not self.is_interactive_desktop() or not self._roblox_installation(window.executable):
            return 'unavailable'
        if not self.user32.IsWindow(window.handle):
            return 'gone'
        if self._session(window.pid) != self.session_id:
            return 'unavailable'
        current = self._window(window.handle)
        if current is None:
            return 'unavailable'
        if (current.identity != window.identity or
                ntpath.normcase(current.executable) != ntpath.normcase(window.executable)):
            return 'changed'
        # PROCESS_TERMINATE | PROCESS_QUERY_LIMITED_INFORMATION. Keep this handle
        # from identity verification through termination so a reused PID is safe.
        process = self.kernel32.OpenProcess(0x1001, False, window.pid)
        if not process:
            return 'unavailable'
        try:
            details = self._process_details(process)
            if details is None:
                return 'unavailable'
            executable, started = details
            if (started != window.process_started or
                    ntpath.normcase(executable) != ntpath.normcase(window.executable) or
                    not self._roblox_installation(executable)):
                return 'changed'
            if self.kernel32.TerminateProcess(process, 0):
                return 'terminated'
            return 'unavailable'
        finally:
            self.kernel32.CloseHandle(process)


if __name__ == '__main__':
    # Running this file only prints the preview. It never sends close requests.
    from dataclasses import asdict
    print(json.dumps([asdict(window) for window in WindowsDesktop().list_windows()], ensure_ascii=False))
