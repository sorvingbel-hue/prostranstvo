"""Opt-in local desktop control; importing this file never touches Windows.

This prototype is not a privilege boundary. It uses only the unlocked input
desktop of its current Windows user. No shell, cookies, clipboard or network.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import base64
import ctypes as C
from ctypes import wintypes as W
import ntpath
import os
import secrets
import struct
import threading
import time
import zlib

MAX_PIXELS = 16_000_000
MAX_PNG_BYTES = 12 * 1024 * 1024
MAX_TEXT_CHARS = 2000
OBSERVATION_SECONDS = 20
MAX_SESSION_SECONDS = 3600

# These processes are intentionally outside this prototype's input surface.
PROTECTED_PROGRAMS = frozenset({
    'consent.exe', 'credentialuibroker.exe', 'logonui.exe', 'lockapp.exe',
    'winlogon.exe', 'wininit.exe', 'lsass.exe', 'securityhealthsystray.exe',
    'sechealthui.exe', 'msmpeng.exe', 'mpcmdrun.exe', 'taskmgr.exe',
    'keepass.exe', 'keepassxc.exe', '1password.exe', 'bitwarden.exe',
    'lastpass.exe', 'dashlane.exe', 'powershell.exe', 'pwsh.exe', 'cmd.exe',
    'windowsterminal.exe', 'openconsole.exe', 'conhost.exe', 'mintty.exe',
    'bash.exe', 'wsl.exe', 'codex.exe', 'codex-desktop.exe', 'chatgpt.exe',
})
PROTECTED_SCREEN_PROGRAMS = frozenset({
    'consent.exe', 'credentialuibroker.exe', 'logonui.exe', 'lockapp.exe',
    'winlogon.exe', 'keepass.exe', 'keepassxc.exe', '1password.exe',
    'bitwarden.exe', 'lastpass.exe', 'dashlane.exe',
})


class ControlError(RuntimeError):
    """Only fixed public codes cross MCP; native exception details stay local."""
    def __init__(self, code):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class Window:
    hwnd: int
    pid: int
    started: int
    executable: str
    class_name: str
    title: str = ''

    @property
    def identity(self):
        return self.hwnd, self.pid, self.started, self.executable.casefold(), self.class_name

    @property
    def window_id(self):
        return f'{self.hwnd:x}:{self.pid}:{self.started}'

    def public(self):
        return {'window_id': self.window_id, 'program': ntpath.basename(self.executable),
                'title': self.title[:240], 'class_name': self.class_name[:120]}


@dataclass(frozen=True)
class State:
    session_id: int
    station: str
    thread_desktop: str
    input_desktop: str
    bounds: tuple[int, int, int, int]
    foreground: Window | None
    windows: tuple[Window, ...] = ()
    connected: bool = True
    protected_auth_present: bool = False

    @property
    def signature(self):
        return (self.session_id, self.connected, self.protected_auth_present, self.station.casefold(), self.thread_desktop.casefold(),
                self.input_desktop.casefold(), self.bounds,
                self.foreground.identity if self.foreground else None)


def require_desktop(state, *, input_action=False):
    if not isinstance(state, State) or state.session_id <= 0:
        raise ControlError('interactive_session_required')
    if state.connected is not True:
        raise ControlError('connected_session_required')
    if (state.station.casefold() != 'winsta0' or
            state.thread_desktop.casefold() != 'default' or
            state.input_desktop.casefold() != 'default'):
        raise ControlError('unlocked_default_desktop_required')
    if (len(state.bounds) != 4 or any(type(x) is not int for x in state.bounds) or
            not 1 <= state.bounds[2] <= 32768 or not 1 <= state.bounds[3] <= 32768 or
            state.bounds[2] * state.bounds[3] > MAX_PIXELS):
        raise ControlError('unsupported_screen_bounds')
    if state.foreground is None or not state.foreground.started or not state.foreground.executable:
        raise ControlError('foreground_identity_unavailable')
    if state.protected_auth_present or any(ntpath.basename(w.executable).casefold() in PROTECTED_SCREEN_PROGRAMS
           for w in (state.foreground,) + state.windows):
        raise ControlError('protected_authentication_window_present')
    if input_action and (state.foreground.pid == os.getpid() or
                         ntpath.basename(state.foreground.executable).casefold() in PROTECTED_PROGRAMS):
        raise ControlError('protected_foreground')


def coordinate(value):
    if type(value) is not int:
        raise ControlError('invalid_coordinate')
    return value


def validate_point(bounds, x, y):
    x, y = coordinate(x), coordinate(y)
    left, top, width, height = bounds
    if not left <= x < left + width or not top <= y < top + height:
        raise ControlError('coordinate_outside_observation')
    return x, y


KEYS = {'ENTER': 0x0D, 'TAB': 0x09, 'ESCAPE': 0x1B, 'SPACE': 0x20,
        'BACKSPACE': 0x08, 'DELETE': 0x2E, 'INSERT': 0x2D, 'HOME': 0x24,
        'END': 0x23, 'PAGEUP': 0x21, 'PAGEDOWN': 0x22, 'LEFT': 0x25,
        'UP': 0x26, 'RIGHT': 0x27, 'DOWN': 0x28}
KEYS.update({chr(n): n for n in range(ord('A'), ord('Z') + 1)})
KEYS.update({str(n): ord(str(n)) for n in range(10)})
KEYS.update({f'F{n}': 0x6F + n for n in range(1, 13)})
MODIFIERS = {'CTRL': 0x11, 'ALT': 0x12, 'SHIFT': 0x10}


def chord(value):
    if not isinstance(value, str) or len(value) > 60:
        raise ControlError('unsupported_key_chord')
    parts = value.upper().split('+')
    if (not 1 <= len(parts) <= 4 or len(set(parts)) != len(parts) or
            parts[-1] not in KEYS or any(x not in MODIFIERS for x in parts[:-1])):
        raise ControlError('unsupported_key_chord')
    if set(parts[:-1]) >= {'CTRL', 'ALT'} and parts[-1] == 'DELETE':
        raise ControlError('secure_attention_not_supported')
    return [MODIFIERS[x] for x in parts[:-1]], KEYS[parts[-1]]


def synchronized(method):
    def invoke(self, *args, **kwargs):
        with self.lock:
            return method(self, *args, **kwargs)
    return invoke


class Controller:
    """Single-use observations. Backend is injected, so tests need no OS calls."""
    def __init__(self, backend, *, enabled=False, session_seconds=1800,
                 clock=time.monotonic, audit=None, nonce=secrets.token_urlsafe):
        if type(enabled) is not bool or type(session_seconds) is not int or not 1 <= session_seconds <= MAX_SESSION_SECONDS:
            raise ValueError('Invalid local session options')
        self.backend, self.enabled, self.clock = backend, enabled, clock
        self.lock = threading.RLock()
        self.audit = audit or (lambda event: None)
        self.nonce = nonce
        self.deadline = clock() + session_seconds
        self.stopped = False
        self.revoked = threading.Event()
        self.observation = None
        self.events = deque(maxlen=50)

    def _record(self, operation, outcome):
        event = {'operation': operation, 'outcome': outcome, 'at_unix': time.time()}
        self.events.append(event)
        try:
            self.audit(dict(event))  # Deliberately no arguments, text or image.
        except Exception:
            pass  # A failed audit sink cannot change a native action's result.

    @synchronized
    def status(self):
        return {'product': 'prostranstvo-desktop-control-prototype', 'version': '0.1.0',
                'input_enabled': self.enabled and not self.stopped and self.clock() < self.deadline,
                'stopped': self.stopped, 'remaining_seconds': max(0, int(self.deadline - self.clock())),
                'observation_valid_seconds': OBSERVATION_SECONDS, 'stdio_only': True,
                'privilege': 'current Windows user; no elevation', 'last_events': list(self.events)}

    def stop(self):
        # Revocation cannot wait behind capture/inspection or a GUI lock.
        self.revoked.set()
        self.enabled = False
        self.stopped = True
        self.observation = None
        self._record('desktop_stop', 'stopped')
        return {'status': 'stopped', 'restart_required_to_reenable': True}

    @synchronized
    def observe(self):
        self.observation = None
        if self.stopped or self.clock() >= self.deadline:
            raise ControlError('session_stopped_or_expired')
        before = self.backend.inspect()
        require_desktop(before)
        image = self.backend.screenshot()
        after = self.backend.inspect()
        require_desktop(after)
        if before.signature != after.signature:
            raise ControlError('desktop_changed_during_observation')
        if not isinstance(image, bytes) or not 24 <= len(image) <= MAX_PNG_BYTES or image[:8] != b'\x89PNG\r\n\x1a\n':
            raise ControlError('invalid_or_oversized_screenshot')
        width, height = struct.unpack('>II', image[16:24])
        if (width, height) != before.bounds[2:]:
            raise ControlError('screenshot_bounds_mismatch')
        # Capturing can take time; neither return an image nor mint a token if
        # the local session expired while the backend was working.
        if self.revoked.is_set() or self.clock() >= self.deadline:
            raise ControlError('session_stopped_or_expired')
        ident = self.nonce(24)
        self.observation = (ident, self.clock(), after)
        self._record('desktop_observe', 'observed')
        return {'observation_id': ident, 'valid_for_seconds': OBSERVATION_SECONDS,
                'bounds': dict(zip(('left', 'top', 'width', 'height'), after.bounds)),
                'foreground': after.foreground.public(),
                'windows': [w.public() for w in after.windows[:100]],
                'image': {'type': 'image', 'mimeType': 'image/png',
                          'data': base64.b64encode(image).decode('ascii')}}

    def _authorize(self, observation_id, *, input_action=True):
        if self.stopped or not self.enabled or self.clock() >= self.deadline:
            raise ControlError('input_disabled_stopped_or_expired')
        old = self.observation
        self.observation = None  # One attempt consumes the observation, even on failure.
        if (not old or not isinstance(observation_id, str) or
                not observation_id.isascii() or len(observation_id) > 128 or
                not secrets.compare_digest(old[0], observation_id)):
            raise ControlError('fresh_observation_required')
        if not 0 <= self.clock() - old[1] < OBSERVATION_SECONDS:
            raise ControlError('observation_expired')
        self._action_deadline = old[1] + OBSERVATION_SECONDS
        current = self.backend.inspect()
        require_desktop(current, input_action=input_action)
        if current.signature != old[2].signature:
            raise ControlError('desktop_or_focus_changed')
        return old[2], current

    @synchronized
    def act(self, operation, arguments):
        if type(arguments) is not dict:
            raise ControlError('invalid_arguments')
        expected = {'desktop_click': {'observation_id', 'x', 'y', 'button'},
                    'desktop_move': {'observation_id', 'x', 'y'},
                    'desktop_drag': {'observation_id', 'x1', 'y1', 'x2', 'y2', 'button', 'duration_ms', 'steps'},
                    'desktop_type': {'observation_id', 'text'},
                    'desktop_press': {'observation_id', 'chord'},
                    'desktop_scroll': {'observation_id', 'x', 'y', 'notches'},
                    'desktop_window': {'observation_id', 'window_id', 'action'}}
        allowed = expected.get(operation, set()) | ({'count'} if operation == 'desktop_click' else set())
        if operation not in expected or not expected[operation] <= set(arguments) or set(arguments) - allowed:
            raise ControlError('invalid_arguments')
        try:
            observed, current = self._authorize(arguments['observation_id'], input_action=operation != 'desktop_window')
            values = {k: v for k, v in arguments.items() if k != 'observation_id'}
            if operation in ('desktop_click', 'desktop_scroll', 'desktop_move'):
                validate_point(observed.bounds, values['x'], values['y'])
            if operation == 'desktop_drag':
                validate_point(observed.bounds, values['x1'], values['y1'])
                validate_point(observed.bounds, values['x2'], values['y2'])
                if values['button'] not in ('left', 'right'):
                    raise ControlError('invalid_drag_button')
                if type(values['duration_ms']) is not int or not 100 <= values['duration_ms'] <= 2000:
                    raise ControlError('invalid_drag_duration')
                if type(values['steps']) is not int or not 2 <= values['steps'] <= 40:
                    raise ControlError('invalid_drag_steps')
            if operation == 'desktop_click' and values['button'] not in ('left', 'right', 'middle'):
                raise ControlError('invalid_mouse_button')
            if operation == 'desktop_click':
                values.setdefault('count', 1)
                if type(values['count']) is not int or values['count'] not in (1, 2):
                    raise ControlError('invalid_click_count')
            if operation == 'desktop_scroll' and (type(values['notches']) is not int or not 1 <= abs(values['notches']) <= 20):
                raise ControlError('invalid_scroll_amount')
            if operation == 'desktop_type':
                text = values['text']
                if (not isinstance(text, str) or not 1 <= len(text) <= MAX_TEXT_CHARS or
                        any(ord(x) < 32 and x not in '\n\t' for x in text)):
                    raise ControlError('invalid_text')
                try: text.encode('utf-16-le', 'strict')
                except UnicodeError: raise ControlError('invalid_text') from None
            if operation == 'desktop_press':
                chord(values['chord'])
            if operation == 'desktop_window':
                if values['action'] not in ('activate', 'minimize', 'maximize', 'restore'):
                    raise ControlError('unsupported_window_action')
                saved = next((w for w in observed.windows if w.window_id == values['window_id']), None)
                live = next((w for w in current.windows if w.window_id == values['window_id']), None)
                if not saved or not live or saved.identity != live.identity:
                    raise ControlError('window_identity_changed')
                if live.pid == os.getpid() or ntpath.basename(live.executable).casefold() in PROTECTED_PROGRAMS:
                    raise ControlError('protected_target')
                values['window'] = live
                values.pop('window_id')
            # Inspection can block; a token valid before inspection may no
            # longer be valid now. Native backend repeats the time gate.
            valid_until = min(self.deadline, self._action_deadline)
            if self.revoked.is_set() or self.clock() >= valid_until:
                raise ControlError('observation_or_session_expired')
            self.backend.invoke(operation, values, expected=current,
                                valid_until=valid_until, clock=self.clock,
                                is_stopped=self.revoked.is_set)
            self._record(operation, 'requested')
            return {'status': 'input_or_window_request_issued', 'requires_new_observation': True,
                    'site_action_or_delivery_verified': False}
        except ControlError as error:
            if operation == 'desktop_drag' or error.code == 'partial_input_outcome_unknown_do_not_retry':
                self.stop()
            self._record(operation, error.code)
            raise
        except Exception:
            if operation == 'desktop_drag':
                self.stop()
            self._record(operation, 'native_operation_failed')
            raise ControlError('native_operation_failed_outcome_unknown') from None


class MOUSEINPUT(C.Structure):
    _fields_ = [('dx', W.LONG), ('dy', W.LONG), ('mouseData', W.DWORD),
                ('dwFlags', W.DWORD), ('time', W.DWORD), ('dwExtraInfo', C.c_size_t)]


class KEYBDINPUT(C.Structure):
    _fields_ = [('wVk', W.WORD), ('wScan', W.WORD), ('dwFlags', W.DWORD),
                ('time', W.DWORD), ('dwExtraInfo', C.c_size_t)]


class HARDWAREINPUT(C.Structure):
    _fields_ = [('uMsg', W.DWORD), ('wParamL', W.WORD), ('wParamH', W.WORD)]


class INPUTUNION(C.Union):
    _fields_ = [('mi', MOUSEINPUT), ('ki', KEYBDINPUT), ('hi', HARDWAREINPUT)]


class INPUT(C.Structure):
    _anonymous_ = ('u',)
    _fields_ = [('type', W.DWORD), ('u', INPUTUNION)]


class WindowsBackend:
    """Lazy, current-user Win32 adapter. Construction performs no input/capture."""
    def __init__(self, *, indicator_hwnd=None):
        if os.name != 'nt':
            raise ControlError('windows_required')
        if C.sizeof(INPUT) != (40 if C.sizeof(C.c_void_p) == 8 else 28):
            raise ControlError('unsupported_input_abi')
        self.u = C.WinDLL('user32', use_last_error=True)
        self.k = C.WinDLL('kernel32', use_last_error=True)
        self.g = C.WinDLL('gdi32', use_last_error=True)
        self.wts = C.WinDLL('wtsapi32', use_last_error=True)
        self.a = C.WinDLL('advapi32', use_last_error=True)
        self.indicator_hwnd = indicator_hwnd
        self.sleep = time.sleep
        self._configure()
        self._require_unelevated()
        # Only this newly launched process. Never changes Windows scaling.
        self.u.SetProcessDPIAware()
        if not self.u.IsProcessDPIAware():
            raise ControlError('dpi_awareness_required')

    def _configure(self):
        self.enum_type = C.WINFUNCTYPE(W.BOOL, W.HWND, W.LPARAM)
        declarations = [
            (self.u, 'SetProcessDPIAware', [], W.BOOL),
            (self.u, 'IsProcessDPIAware', [], W.BOOL),
            (self.u, 'GetSystemMetrics', [C.c_int], C.c_int),
            (self.u, 'GetForegroundWindow', [], W.HWND),
            (self.u, 'GetThreadDesktop', [W.DWORD], W.HANDLE),
            (self.u, 'GetProcessWindowStation', [], W.HANDLE),
            (self.u, 'OpenInputDesktop', [W.DWORD, W.BOOL, W.DWORD], W.HANDLE),
            (self.u, 'CloseDesktop', [W.HANDLE], W.BOOL),
            (self.u, 'GetUserObjectInformationW', [W.HANDLE, C.c_int, C.c_void_p, W.DWORD, C.POINTER(W.DWORD)], W.BOOL),
            (self.u, 'EnumWindows', [self.enum_type, W.LPARAM], W.BOOL),
            (self.u, 'IsWindowVisible', [W.HWND], W.BOOL),
            (self.u, 'IsIconic', [W.HWND], W.BOOL),
            (self.u, 'GetWindowThreadProcessId', [W.HWND, C.POINTER(W.DWORD)], W.DWORD),
            (self.u, 'GetClassNameW', [W.HWND, W.LPWSTR, C.c_int], C.c_int),
            (self.u, 'GetWindowTextW', [W.HWND, W.LPWSTR, C.c_int], C.c_int),
            (self.u, 'SendInput', [W.UINT, C.POINTER(INPUT), C.c_int], W.UINT),
            (self.u, 'GetAsyncKeyState', [C.c_int], C.c_short),
            (self.u, 'GetCursorPos', [C.POINTER(W.POINT)], W.BOOL),
            (self.u, 'WindowFromPoint', [W.POINT], W.HWND),
            (self.u, 'GetAncestor', [W.HWND, W.UINT], W.HWND),
            (self.u, 'SetForegroundWindow', [W.HWND], W.BOOL),
            (self.u, 'ShowWindowAsync', [W.HWND, C.c_int], W.BOOL),
            (self.k, 'GetCurrentThreadId', [], W.DWORD),
            (self.k, 'GetCurrentProcess', [], W.HANDLE),
            (self.k, 'ProcessIdToSessionId', [W.DWORD, C.POINTER(W.DWORD)], W.BOOL),
            (self.k, 'OpenProcess', [W.DWORD, W.BOOL, W.DWORD], W.HANDLE),
            (self.k, 'CloseHandle', [W.HANDLE], W.BOOL),
            (self.k, 'QueryFullProcessImageNameW', [W.HANDLE, W.DWORD, W.LPWSTR, C.POINTER(W.DWORD)], W.BOOL),
            (self.k, 'GetProcessTimes', [W.HANDLE] + [C.POINTER(W.FILETIME)] * 4, W.BOOL),
            (self.a, 'OpenProcessToken', [W.HANDLE, W.DWORD, C.POINTER(W.HANDLE)], W.BOOL),
            (self.a, 'GetTokenInformation', [W.HANDLE, C.c_int, C.c_void_p, W.DWORD, C.POINTER(W.DWORD)], W.BOOL),
            (self.wts, 'WTSQuerySessionInformationW', [W.HANDLE, W.DWORD, C.c_int, C.POINTER(C.c_void_p), C.POINTER(W.DWORD)], W.BOOL),
            (self.wts, 'WTSFreeMemory', [C.c_void_p], None),
            (self.u, 'GetDC', [W.HWND], W.HDC),
            (self.u, 'ReleaseDC', [W.HWND, W.HDC], C.c_int),
            (self.g, 'CreateCompatibleDC', [W.HDC], W.HDC),
            (self.g, 'CreateCompatibleBitmap', [W.HDC, C.c_int, C.c_int], W.HBITMAP),
            (self.g, 'SelectObject', [W.HDC, W.HANDLE], W.HANDLE),
            (self.g, 'DeleteObject', [W.HANDLE], W.BOOL),
            (self.g, 'DeleteDC', [W.HDC], W.BOOL),
            (self.g, 'BitBlt', [W.HDC, C.c_int, C.c_int, C.c_int, C.c_int, W.HDC, C.c_int, C.c_int, W.DWORD], W.BOOL),
            (self.g, 'GetDIBits', [W.HDC, W.HBITMAP, W.UINT, W.UINT, C.c_void_p, C.c_void_p, W.UINT], C.c_int),
        ]
        for dll, name, args, result in declarations:
            method = getattr(dll, name)
            method.argtypes, method.restype = args, result

    def _require_unelevated(self):
        token = W.HANDLE()
        if not self.a.OpenProcessToken(self.k.GetCurrentProcess(), 8, C.byref(token)):
            raise ControlError('token_identity_unavailable')
        try:
            elevated, needed = W.DWORD(), W.DWORD()
            if not self.a.GetTokenInformation(token, 20, C.byref(elevated), C.sizeof(elevated), C.byref(needed)):
                raise ControlError('token_identity_unavailable')
            if needed.value != C.sizeof(elevated) or elevated.value:
                raise ControlError('unelevated_user_required')
        finally:
            self.k.CloseHandle(token)

    def _connected(self, session):
        data, count = C.c_void_p(), W.DWORD()
        if not self.wts.WTSQuerySessionInformationW(None, session, 8, C.byref(data), C.byref(count)):
            raise ControlError('connection_state_unavailable')
        try:
            if not data or count.value != C.sizeof(C.c_int):
                raise ControlError('connection_state_unavailable')
            return C.cast(data, C.POINTER(C.c_int)).contents.value == 0  # WTSActive
        finally:
            if data: self.wts.WTSFreeMemory(data)

    def _name(self, handle):
        if not handle:
            raise ControlError('desktop_identity_unavailable')
        needed = W.DWORD()
        self.u.GetUserObjectInformationW(handle, 2, None, 0, C.byref(needed))
        if not 2 <= needed.value <= 1024:
            raise ControlError('desktop_identity_unavailable')
        buffer = C.create_unicode_buffer(needed.value // 2 + 1)
        if not self.u.GetUserObjectInformationW(handle, 2, buffer, C.sizeof(buffer), C.byref(needed)):
            raise ControlError('desktop_identity_unavailable')
        return buffer.value

    def _session(self, pid):
        ident = W.DWORD()
        if not self.k.ProcessIdToSessionId(pid, C.byref(ident)):
            raise ControlError('process_session_unavailable')
        return ident.value

    def _window(self, hwnd, session):
        if not hwnd or not self.u.IsWindowVisible(hwnd):
            return None
        pid = W.DWORD()
        self.u.GetWindowThreadProcessId(hwnd, C.byref(pid))
        if not pid.value or self._session(pid.value) != session:
            return None
        handle = self.k.OpenProcess(0x1000, False, pid.value)
        if not handle:
            return None
        try:
            path = C.create_unicode_buffer(32768)
            size = W.DWORD(len(path))
            if not self.k.QueryFullProcessImageNameW(handle, 0, path, C.byref(size)):
                return None
            created, ended, kernel, user = (W.FILETIME() for _ in range(4))
            if not self.k.GetProcessTimes(handle, C.byref(created), C.byref(ended), C.byref(kernel), C.byref(user)):
                return None
            klass, title = C.create_unicode_buffer(256), C.create_unicode_buffer(241)
            if not self.u.GetClassNameW(hwnd, klass, len(klass)):
                return None
            # Own-process GetWindowTextW synchronously sends WM_GETTEXT and
            # can deadlock Tk against a worker inspection. Identity is safe;
            # keep its title blank and refuse it as an input/window target.
            if pid.value != os.getpid():
                self.u.GetWindowTextW(hwnd, title, len(title))
            return Window(int(hwnd), pid.value, (created.dwHighDateTime << 32) | created.dwLowDateTime,
                          path.value, klass.value, title.value)
        finally:
            self.k.CloseHandle(handle)

    def inspect(self, *, cleanup=False):
        # Revocation/indicator loss must block new work, but must not itself
        # prevent an already-held synthetic button from being released.
        if not cleanup and self.indicator_hwnd is not None:
            indicator = self.u.GetAncestor(self.indicator_hwnd, 2) or self.indicator_hwnd
            if not self.u.IsWindowVisible(indicator) or self.u.IsIconic(indicator):
                raise ControlError('visible_indicator_required')
        session = self._session(os.getpid())
        connected = self._connected(session)
        station = self._name(self.u.GetProcessWindowStation())
        thread = self._name(self.u.GetThreadDesktop(self.k.GetCurrentThreadId()))
        incoming = self.u.OpenInputDesktop(0, False, 0x0001)
        try: input_name = self._name(incoming)
        finally:
            if incoming: self.u.CloseDesktop(incoming)
        bounds = tuple(self.u.GetSystemMetrics(i) for i in (76, 77, 78, 79))
        state = State(session, station, thread, input_name, bounds, None, connected=connected)
        # Do not enumerate/read windows until secure/other desktops are rejected.
        if not connected or session <= 0 or station.casefold() != 'winsta0' or thread.casefold() != 'default' or input_name.casefold() != 'default':
            return state
        foreground = self._window(self.u.GetForegroundWindow(), session)
        windows = []
        callback_error = []
        protected_auth = []
        def receive(hwnd, _):
            try:
                window = self._window(hwnd, session)
                if window and ntpath.basename(window.executable).casefold() in PROTECTED_SCREEN_PROGRAMS:
                    protected_auth.append(True)
                if window and len(windows) < 100: windows.append(window)
            except (ControlError, OSError):
                pass  # A changing/inaccessible background window is not a target.
            except Exception:
                callback_error.append(True)
            return True
        callback = self.enum_type(receive)
        C.set_last_error(0)
        ok = self.u.EnumWindows(callback, 0)
        if callback_error or not ok:
            raise ControlError('window_enumeration_failed')
        return State(session, station, thread, input_name, bounds, foreground, tuple(windows), connected, bool(protected_auth))

    def _same(self, expected, valid_until=None, clock=time.monotonic, is_stopped=lambda: False, *, input_action=True, held_mouse=None):
        if is_stopped():
            raise ControlError('session_stopped_or_expired')
        current = self.inspect()
        require_desktop(current, input_action=input_action)
        if current.signature != expected.signature:
            raise ControlError('desktop_or_focus_changed')
        if any(self.u.GetAsyncKeyState(key) & 0x8000 for key in (0x10, 0x11, 0x12, 0x5B, 0x5C)):
            raise ControlError('physical_modifier_pressed')
        if any(key != held_mouse and self.u.GetAsyncKeyState(key) & 0x8000 for key in (1, 2, 4, 5, 6)):
            raise ControlError('physical_mouse_button_pressed')
        if is_stopped() or (valid_until is not None and clock() >= valid_until):
            raise ControlError('observation_or_session_expired')
        return current

    @staticmethod
    def _mouse(dx=0, dy=0, flags=0, data=0):
        return INPUT(type=0, mi=MOUSEINPUT(dx, dy, data & 0xFFFFFFFF, flags, 0, 0))

    @staticmethod
    def _key(vk=0, scan=0, flags=0):
        return INPUT(type=1, ki=KEYBDINPUT(vk, scan, flags, 0, 0))

    def _send(self, events, expected, valid_until, clock, is_stopped):
        self._same(expected, valid_until, clock, is_stopped)
        items = (INPUT * len(events))(*events)
        accepted = self.u.SendInput(len(events), items, C.sizeof(INPUT))
        if accepted != len(events):
            self._release_partial(events[:max(0, min(accepted, len(events)))], expected)
            # A partial send can already have had an effect. Never auto-retry it.
            raise ControlError('partial_input_outcome_unknown_do_not_retry')

    def _release_partial(self, prefix, expected):
        """Best-effort key/button-up cleanup only; never retry original input."""
        held = {}
        for event in prefix:
            if event.type == 1:
                key = ('key', event.ki.wVk, event.ki.wScan, event.ki.dwFlags & ~2)
                if event.ki.dwFlags & 2:
                    held.pop(key, None)
                else:
                    held[key] = self._key(event.ki.wVk, event.ki.wScan, event.ki.dwFlags | 2)
            elif event.type == 0:
                for down, up in ((2, 4), (8, 16), (32, 64)):
                    key = ('mouse', down)
                    if event.mi.dwFlags & down: held[key] = self._mouse(flags=up)
                    if event.mi.dwFlags & up: held.pop(key, None)
        if not held:
            return
        try:
            current = self.inspect(cleanup=True)
            require_desktop(current, input_action=True)
            if current.signature != expected.signature:
                return
            releases = list(reversed(list(held.values())))
            items = (INPUT * len(releases))(*releases)
            self.u.SendInput(len(releases), items, C.sizeof(INPUT))
        except Exception:
            pass  # Outcome remains unknown. Local user may need to release keys.

    def invoke(self, operation, values, *, expected, valid_until, clock=time.monotonic, is_stopped=lambda: False):
        current = self._same(expected, valid_until, clock, is_stopped, input_action=operation != 'desktop_window')
        if operation == 'desktop_drag':
            self._drag(values, expected, valid_until, clock, is_stopped)
        elif operation in ('desktop_click', 'desktop_scroll', 'desktop_move'):
            x, y = validate_point(current.bounds, values['x'], values['y'])
            hit = self.u.WindowFromPoint(W.POINT(x, y))
            if (not hit or self.u.GetAncestor(hit, 3) !=
                    self.u.GetAncestor(current.foreground.hwnd, 3)):
                raise ControlError('point_not_in_observed_foreground')
            left, top, width, height = current.bounds
            events = [self._mouse((x-left)*65535//max(1,width-1), (y-top)*65535//max(1,height-1), 0xC001)]
            if operation == 'desktop_click':
                down, up = {'left': (2, 4), 'right': (8, 16), 'middle': (32, 64)}[values['button']]
                events += [self._mouse(flags=down), self._mouse(flags=up)] * values['count']
            elif operation == 'desktop_scroll':
                events.append(self._mouse(flags=0x0800, data=values['notches']*120))
            self._send(events, expected, valid_until, clock, is_stopped)
        elif operation == 'desktop_press':
            modifiers, key = chord(values['chord'])
            self._send([self._key(vk=v) for v in modifiers+[key]] +
                       [self._key(vk=v, flags=2) for v in [key]+list(reversed(modifiers))], expected, valid_until, clock, is_stopped)
        elif operation == 'desktop_type':
            events = []
            for char in values['text']:
                if char in '\n\t':
                    vk = 0x0D if char == '\n' else 0x09
                    events += [self._key(vk=vk), self._key(vk=vk, flags=2)]
                else:
                    units = char.encode('utf-16-le')
                    for i in range(0, len(units), 2):
                        unit = int.from_bytes(units[i:i+2], 'little')
                        events += [self._key(scan=unit, flags=4), self._key(scan=unit, flags=6)]
            self._send(events, expected, valid_until, clock, is_stopped)
        elif operation == 'desktop_window':
            saved = values['window']
            actual = self._window(saved.hwnd, current.session_id)
            if not actual or actual.identity != saved.identity:
                raise ControlError('window_identity_changed')
            if actual.pid == os.getpid() or ntpath.basename(actual.executable).casefold() in PROTECTED_PROGRAMS:
                raise ControlError('protected_target')
            self._same(expected, valid_until, clock, is_stopped, input_action=False)
            if values['action'] == 'activate':
                if not self.u.SetForegroundWindow(actual.hwnd):
                    raise ControlError('foreground_request_refused')
            elif not self.u.ShowWindowAsync(actual.hwnd, {'minimize': 6, 'maximize': 3, 'restore': 9}[values['action']]):
                raise ControlError('window_request_refused')
        else:
            raise ControlError('unsupported_operation')

    def _drag_point(self, expected, x, y, valid_until, clock, is_stopped, held_mouse=None):
        current = self._same(expected, valid_until, clock, is_stopped, held_mouse=held_mouse)
        validate_point(current.bounds, x, y)
        hit = self.u.WindowFromPoint(W.POINT(x, y))
        if not hit or self.u.GetAncestor(hit, 3) != self.u.GetAncestor(current.foreground.hwnd, 3):
            raise ControlError('drag_point_not_in_observed_foreground')
        return current

    def _drag_wait(self, until, valid_until, clock, is_stopped):
        while True:
            now = clock()
            if is_stopped() or now >= valid_until:
                raise ControlError('drag_stopped_or_expired')
            remaining = until - now
            if remaining <= 0:
                return
            # Sleep is injectable for fake tests. A physical user can always
            # interrupt; this function never captures or blocks their input.
            self.sleep(min(0.02, remaining))

    def _drag_release(self, up, expected):
        """One best-effort release on the same normal desktop/focus only.

        Never release into a protected/foreign window after a focus change.
        Windows can refuse SendInput: no code can guarantee cleanup then.
        """
        try:
            current = self.inspect(cleanup=True)
            require_desktop(current, input_action=True)
            if current.signature != expected.signature:
                return False
            # A non-activating foreign overlay can appear without changing
            # foreground. Do not drop into it when issuing the final key-up.
            cursor = W.POINT()
            if not self.u.GetCursorPos(C.byref(cursor)):
                return False
            hit = self.u.WindowFromPoint(cursor)
            if not hit or self.u.GetAncestor(hit, 3) != self.u.GetAncestor(current.foreground.hwnd, 3):
                return False
            event = (INPUT * 1)(self._mouse(flags=up))
            return self.u.SendInput(1, event, C.sizeof(INPUT)) == 1
        except Exception:
            return False

    def _drag(self, values, expected, valid_until, clock, is_stopped):
        # The controller validates these first; repeat in the native adapter
        # because it is also an integration boundary.
        x1, y1 = validate_point(expected.bounds, values['x1'], values['y1'])
        x2, y2 = validate_point(expected.bounds, values['x2'], values['y2'])
        button, duration, steps = values['button'], values['duration_ms'], values['steps']
        if (button not in ('left', 'right') or type(duration) is not int or not 100 <= duration <= 2000 or
                type(steps) is not int or not 2 <= steps <= 40):
            raise ControlError('invalid_drag_arguments')
        down, up, vk = {'left': (2, 4, 1), 'right': (8, 16, 2)}[button]
        left, top, width, height = expected.bounds
        def move(x, y):
            return self._mouse((x-left)*65535//max(1,width-1), (y-top)*65535//max(1,height-1), 0xC001)
        self._drag_point(expected, x1, y1, valid_until, clock, is_stopped)
        self._send([move(x1,y1)], expected, valid_until, clock, is_stopped)
        held = False
        try:
            self._drag_point(expected, x1, y1, valid_until, clock, is_stopped)
            # Mark possibly held before issuing: even an exception/partial
            # result cannot skip the finally release attempt.
            held = True
            packet = (INPUT * 1)(self._mouse(flags=down))
            if self.u.SendInput(1, packet, C.sizeof(INPUT)) != 1:
                raise ControlError('partial_drag_input_outcome_unknown_do_not_retry')
            started = clock()
            # Requested trajectory duration plus bounded scheduling overhead;
            # slow inspection may delay cleanup, but never authorizes another
            # move after this deadline. Cannot cancel a blocked Win32 call.
            drag_until = min(valid_until, started + duration/1000 + 0.25)
            for step in range(1, steps+1):
                self._drag_wait(started + duration/1000*step/steps, drag_until, clock, is_stopped)
                x = round(x1 + (x2-x1)*step/steps)
                y = round(y1 + (y2-y1)*step/steps)
                self._drag_point(expected, x, y, drag_until, clock, is_stopped, held_mouse=vk)
                packet = (INPUT * 1)(move(x,y))
                if self.u.SendInput(1, packet, C.sizeof(INPUT)) != 1:
                    raise ControlError('partial_drag_input_outcome_unknown_do_not_retry')
            # Recheck the endpoint before a normal drop; STOP/error still
            # reaches finally and attempts only release, never the drag again.
            self._drag_point(expected, x2, y2, drag_until, clock, is_stopped, held_mouse=vk)
        finally:
            if held and not self._drag_release(up, expected):
                raise ControlError('drag_release_failed_outcome_unknown') from None

    def screenshot(self):
        before = self.inspect()
        require_desktop(before)
        left, top, width, height = before.bounds
        source = memory = bitmap = previous = None
        try:
            source = self.u.GetDC(None)
            if not source: raise ControlError('capture_failed')
            memory = self.g.CreateCompatibleDC(source)
            bitmap = self.g.CreateCompatibleBitmap(source, width, height)
            if not memory or not bitmap: raise ControlError('capture_failed')
            previous = self.g.SelectObject(memory, bitmap)
            if not previous or previous == C.c_void_p(-1).value: raise ControlError('capture_failed')
            if not self.g.BitBlt(memory, 0, 0, width, height, source, left, top, 0x40CC0020):
                raise ControlError('capture_failed')
            self.g.SelectObject(memory, previous)
            previous = None
            class Header(C.Structure):
                _fields_ = [('size', W.DWORD), ('width', W.LONG), ('height', W.LONG),
                            ('planes', W.WORD), ('bits', W.WORD), ('compression', W.DWORD),
                            ('sizeimage', W.DWORD), ('xppm', W.LONG), ('yppm', W.LONG),
                            ('used', W.DWORD), ('important', W.DWORD)]
            header = Header(C.sizeof(Header), width, -height, 1, 32, 0, 0, 0, 0, 0, 0)
            pixels = C.create_string_buffer(width*height*4)
            if self.g.GetDIBits(memory, bitmap, 0, height, pixels, C.byref(header), 0) != height:
                raise ControlError('capture_failed')
            after = self.inspect()
            require_desktop(after)
            if before.signature != after.signature:
                raise ControlError('desktop_changed_during_capture')
            raw = pixels.raw
            encoded = bytearray((width*3+1)*height)
            for row in range(height):
                block = raw[row*width*4:(row+1)*width*4]
                rgb = bytearray(width*3)
                rgb[0::3], rgb[1::3], rgb[2::3] = block[2::4], block[1::4], block[0::4]
                offset = row*(width*3+1)
                encoded[offset+1:offset+1+width*3] = rgb
            def chunk(kind, data):
                return struct.pack('>I', len(data))+kind+data+struct.pack('>I', zlib.crc32(kind+data)&0xFFFFFFFF)
            png = (b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('>IIBBBBB',width,height,8,2,0,0,0))+
                   chunk(b'IDAT', zlib.compress(encoded,3))+chunk(b'IEND',b''))
            if len(png) > MAX_PNG_BYTES: raise ControlError('screenshot_too_large')
            return png
        finally:
            if previous and memory: self.g.SelectObject(memory, previous)
            if bitmap: self.g.DeleteObject(bitmap)
            if memory: self.g.DeleteDC(memory)
            if source: self.u.ReleaseDC(None, source)
