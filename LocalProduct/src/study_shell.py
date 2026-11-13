"""Visible notification-area control and the S+D+I panel shortcut.

Only the current states of these three keys are queried. No keystrokes,
window titles or typed text are recorded by this module.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import hashlib
import os
from pathlib import Path
import uuid


class ChordLatch:
    """Fire once, then require all three keys to be released together."""

    def __init__(self):
        self.armed = True

    def update(self, pressed, enabled=True):
        if not enabled:
            self.armed = False
            return False
        if not any(pressed):
            self.armed = True
            return False
        if self.armed and all(pressed):
            self.armed = False
            return True
        return False


def shell_identity(root, account):
    """The Local namespace supplies session isolation; account supplies user isolation."""
    location = os.path.normcase(str(Path(root).absolute()))
    digest = hashlib.sha256((account.casefold() + '\x00' + location).encode('utf-8')).hexdigest()
    return 'Local\\ProstranstvoStudyShell-' + digest


class WindowsShellOwnership:
    """Keep one tray/shortcut owner for this account, session and study data root."""

    def __init__(self, root):
        self.root = root
        self.handle = None
        self.kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        self.kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        self.kernel.CreateMutexW.restype = wintypes.HANDLE
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel.CloseHandle.restype = wintypes.BOOL

    def acquire(self):
        if self.handle is not None:
            return True
        security = ctypes.WinDLL('secur32', use_last_error=True)
        security.GetUserNameExW.argtypes = [ctypes.c_int, wintypes.LPWSTR, ctypes.POINTER(wintypes.ULONG)]
        security.GetUserNameExW.restype = ctypes.c_ubyte
        account = ctypes.create_unicode_buffer(32768)
        size = wintypes.ULONG(len(account))
        if not security.GetUserNameExW(2, account, ctypes.byref(size)):
            raise ctypes.WinError(ctypes.get_last_error())
        name = shell_identity(self.root, account.value)
        ctypes.set_last_error(0)
        handle = self.kernel.CreateMutexW(None, False, name)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        if ctypes.get_last_error() == 183:
            self.kernel.CloseHandle(handle)
            return False
        self.handle = handle
        return True

    def close(self):
        if self.handle is not None:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


class WindowsTray:
    """One identifiable tray icon, receiving callbacks on the Tk GUI thread."""

    def __init__(self, on_show):
        if os.name != 'nt':
            raise OSError('Область уведомлений поддерживается в Windows')
        self.on_show = on_show
        self.window = None
        self.atom = None
        self.available = False
        self.class_name = 'ProstranstvoStudyTray-' + str(os.getpid()) + '-' + uuid.uuid4().hex
        self.user = ctypes.WinDLL('user32', use_last_error=True)
        self.shell = ctypes.WinDLL('shell32', use_last_error=True)
        self.kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        self._configure_api()

    def _configure_api(self):
        result_type = ctypes.c_ssize_t
        callback_type = ctypes.WINFUNCTYPE(result_type, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)

        class WindowClass(ctypes.Structure):
            _fields_ = [('style', wintypes.UINT), ('callback', callback_type),
                        ('class_extra', ctypes.c_int), ('window_extra', ctypes.c_int),
                        ('instance', wintypes.HINSTANCE), ('icon', wintypes.HICON),
                        ('cursor', wintypes.HANDLE), ('background', wintypes.HANDLE),
                        ('menu_name', wintypes.LPCWSTR), ('class_name', wintypes.LPCWSTR)]

        class Guid(ctypes.Structure):
            _fields_ = [('data1', wintypes.DWORD), ('data2', wintypes.WORD),
                        ('data3', wintypes.WORD), ('data4', ctypes.c_ubyte * 8)]

        class NotifyIcon(ctypes.Structure):
            _fields_ = [('size', wintypes.DWORD), ('window', wintypes.HWND),
                        ('identifier', wintypes.UINT), ('flags', wintypes.UINT),
                        ('callback_message', wintypes.UINT), ('icon', wintypes.HICON),
                        ('tip', wintypes.WCHAR * 128), ('state', wintypes.DWORD),
                        ('state_mask', wintypes.DWORD), ('info', wintypes.WCHAR * 256),
                        ('timeout_or_version', wintypes.UINT), ('info_title', wintypes.WCHAR * 64),
                        ('info_flags', wintypes.DWORD), ('guid', Guid), ('balloon_icon', wintypes.HICON)]

        self.WindowClass, self.NotifyIcon = WindowClass, NotifyIcon
        self.callback = callback_type(self._window_message)
        functions = (
            (self.kernel.GetModuleHandleW, [wintypes.LPCWSTR], wintypes.HINSTANCE),
            (self.user.RegisterClassW, [ctypes.POINTER(WindowClass)], wintypes.ATOM),
            (self.user.UnregisterClassW, [wintypes.LPCWSTR, wintypes.HINSTANCE], wintypes.BOOL),
            (self.user.CreateWindowExW, [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR,
             wintypes.DWORD, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
             wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, ctypes.c_void_p], wintypes.HWND),
            (self.user.DestroyWindow, [wintypes.HWND], wintypes.BOOL),
            (self.user.DefWindowProcW, [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM], result_type),
            (self.user.LoadIconW, [wintypes.HINSTANCE, ctypes.c_void_p], wintypes.HICON),
            (self.user.RegisterWindowMessageW, [wintypes.LPCWSTR], wintypes.UINT),
            (self.shell.Shell_NotifyIconW, [wintypes.DWORD, ctypes.POINTER(NotifyIcon)], wintypes.BOOL),
        )
        for function, arguments, result in functions:
            function.argtypes, function.restype = arguments, result
        self.taskbar_created = self.user.RegisterWindowMessageW('TaskbarCreated')
        if not self.taskbar_created:
            raise ctypes.WinError(ctypes.get_last_error())

    def install(self):
        if self.window:
            return
        self.instance = self.kernel.GetModuleHandleW(None)
        definition = self.WindowClass()
        definition.callback = self.callback
        definition.instance = self.instance
        definition.class_name = self.class_name
        self.atom = self.user.RegisterClassW(ctypes.byref(definition))
        if not self.atom:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            # Hidden top-level window receives TaskbarCreated when Explorer restarts.
            self.window = self.user.CreateWindowExW(0x80, self.class_name, 'Пространство — Уроки',
                                                    0, 0, 0, 0, 0, None, None, self.instance, None)
            if not self.window:
                raise ctypes.WinError(ctypes.get_last_error())
            self.data = self.NotifyIcon()
            self.data.size = ctypes.sizeof(self.NotifyIcon)
            self.data.window = self.window
            self.data.identifier = 1
            self.data.flags = 1 | 2 | 4
            self.data.callback_message = 0x8001
            self.data.icon = self.user.LoadIconW(None, ctypes.c_void_p(32512))
            if not self.data.icon:
                raise ctypes.WinError(ctypes.get_last_error())
            self.data.tip = 'Пространство — Уроки · S+D+I открывает панель'
            if not self._add_icon():
                raise OSError('Не удалось показать значок «Пространство — Уроки»')
        except Exception:
            self.close()
            raise

    def _add_icon(self):
        self.available = bool(self.shell.Shell_NotifyIconW(0, ctypes.byref(self.data)))
        return self.available

    def _window_message(self, window, message, first, second):
        if message == 0x8001 and second in (0x0202, 0x0203, 0x0205):
            try:
                self.on_show()
            except Exception:
                pass
            return 0
        if message == self.taskbar_created and self.window and hasattr(self, 'data'):
            self._add_icon()
            return 0
        return self.user.DefWindowProcW(window, message, first, second)

    def close(self):
        if self.window:
            if hasattr(self, 'data'):
                self.shell.Shell_NotifyIconW(2, ctypes.byref(self.data))
            self.user.DestroyWindow(self.window)
            self.window = None
        if self.atom:
            self.user.UnregisterClassW(self.class_name, self.instance)
            self.atom = None
        self.available = False


class StudyShell:
    """Show the panel by tray click or the simultaneous S+D+I shortcut."""

    def __init__(self, root, panel, tray_factory=None, key_state=None, desktop_ready=None,
                 ownership_factory=None):
        self.root = root
        self.panel = panel
        self.tray_factory = tray_factory or WindowsTray
        self.key_state = key_state
        self.desktop_ready = desktop_ready
        self.ownership_factory = ownership_factory or WindowsShellOwnership
        self.ownership = None
        self.tray = None
        self.closed = False
        self.timer = None
        self.chord = ChordLatch()
        self.bindings = []
        self._tray_was_available = False

    def install(self):
        if self.closed:
            return False
        if self.tray is not None:
            return True
        study_root = getattr(getattr(self.panel, 'resume_store', None), 'root', None)
        if study_root is None:
            from paths import DEPLOY_ROOT
            study_root = DEPLOY_ROOT / 'data' / 'study'
        ownership = self.ownership_factory(study_root)
        if not ownership.acquire():
            ownership.close()
            return False
        tray = None
        try:
            if self.key_state is None:
                user = ctypes.WinDLL('user32', use_last_error=True)
                user.GetAsyncKeyState.argtypes = [ctypes.c_int]
                user.GetAsyncKeyState.restype = ctypes.c_short
                self.key_state = user.GetAsyncKeyState
            if self.desktop_ready is None:
                from activity_core import desktop_state
                self.desktop_ready = lambda: bool(desktop_state()[0])
            tray = self.tray_factory(self._request_show)
            tray.install()
        except Exception:
            if tray is not None:
                tray.close()
            ownership.close()
            raise
        self.ownership = ownership
        self.tray = tray
        self._tray_was_available = bool(tray.available)
        self.bindings = [('<Destroy>', self.root.bind('<Destroy>', self._destroyed, add='+')),
                         ('<Unmap>', self.root.bind('<Unmap>', self._unmapped, add='+'))]
        self._poll()
        return True

    def _request_show(self):
        if not self.closed:
            self.root.after_idle(self.show)

    def show(self):
        if self.closed:
            return
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def hide(self):
        if not self.closed and self.tray is not None and self.tray.available:
            self.root.withdraw()

    def _unmapped(self, event):
        if event.widget is self.root and not self.closed and self.root.state() == 'iconic':
            self.root.after_idle(self.hide)

    def _poll(self):
        self.timer = None
        if self.closed:
            return
        try:
            unlocked = self.desktop_ready()
            pressed = tuple(bool(self.key_state(key) & 0x8000) for key in (0x53, 0x44, 0x49)) if unlocked else (False,) * 3
            if self.chord.update(pressed, enabled=unlocked):
                self.show()
            if self.tray is not None:
                available = bool(self.tray.available)
                if self._tray_was_available and not available:
                    self.show()
                self._tray_was_available = available
        except Exception:
            self.chord.update((False,) * 3, enabled=False)
        self.timer = self.root.after(100, self._poll)

    def _destroyed(self, event):
        if event.widget is self.root:
            self.close()

    def close(self):
        if self.closed:
            return
        self.closed = True
        if self.timer is not None:
            try:
                self.root.after_cancel(self.timer)
            except Exception:
                pass
            self.timer = None
        for sequence, identifier in self.bindings:
            try:
                self.root.unbind(sequence, identifier)
            except Exception:
                pass
        self.bindings.clear()
        if self.tray is not None:
            self.tray.close()
            self.tray = None
        if self.ownership is not None:
            self.ownership.close()
            self.ownership = None
