"""Local stdio MCP entry point. No network server, shell or installation."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import stat
import sys
import threading

from desktop_control import Controller, ControlError, MAX_TEXT_CHARS, WindowsBackend

MAX_LINE_BYTES = 65536
PROTOCOL_VERSION = '2025-03-26'


def schema(properties, required=()):
    return {'type': 'object', 'properties': properties, 'required': list(required),
            'additionalProperties': False}


OBS = {'type': 'string', 'minLength': 1, 'maxLength': 128}
COORD = {'type': 'integer', 'minimum': -65536, 'maximum': 65536}
TOOLS = [
    {'name': 'desktop_status', 'description': 'Local session settings and metadata only; no OS inspection.', 'inputSchema': schema({})},
    {'name': 'desktop_observe', 'description': 'On-demand screenshot as an MCP image plus one-use observation_id. No disk archive. Screen content goes to the connected AI host.', 'inputSchema': schema({})},
    {'name': 'desktop_stop', 'description': 'Revoke input and screenshots until this server is restarted.', 'inputSchema': schema({})},
    {'name': 'desktop_click', 'description': 'Bounded single or double click within the observed foreground window. Requires fresh observation and --enable-input.', 'inputSchema': schema({'observation_id': OBS, 'x': COORD, 'y': COORD, 'button': {'enum': ['left', 'right', 'middle']}, 'count': {'type': 'integer', 'minimum': 1, 'maximum': 2}}, ('observation_id', 'x', 'y', 'button'))},
    {'name': 'desktop_move', 'description': 'Move pointer within the observed foreground window; no button held and no drag. Requires fresh observation and --enable-input.', 'inputSchema': schema({'observation_id': OBS, 'x': COORD, 'y': COORD}, ('observation_id', 'x', 'y'))},
    {'name': 'desktop_drag', 'description': 'Bounded left/right drag, same observed foreground at every interpolated point; 100..2000ms, 2..40 stages. STOP/errors attempt release and revoke. Not transactional; never retry unknown outcomes. Requires fresh observation.', 'inputSchema': schema({'observation_id': OBS, 'x1': COORD, 'y1': COORD, 'x2': COORD, 'y2': COORD, 'button': {'enum': ['left', 'right']}, 'duration_ms': {'type': 'integer', 'minimum': 100, 'maximum': 2000}, 'steps': {'type': 'integer', 'minimum': 2, 'maximum': 40}}, ('observation_id', 'x1', 'y1', 'x2', 'y2', 'button', 'duration_ms', 'steps'))},
    {'name': 'desktop_type', 'description': 'Type up to 2000 Unicode characters. Never use for passwords, OTPs, scripts or host/security approval. Requires fresh observation.', 'inputSchema': schema({'observation_id': OBS, 'text': {'type': 'string', 'minLength': 1, 'maxLength': MAX_TEXT_CHARS}}, ('observation_id', 'text'))},
    {'name': 'desktop_press', 'description': 'Supported key or CTRL/ALT/SHIFT chord. No Windows key or secure attention. Requires fresh observation.', 'inputSchema': schema({'observation_id': OBS, 'chord': {'type': 'string', 'minLength': 1, 'maxLength': 60}}, ('observation_id', 'chord'))},
    {'name': 'desktop_scroll', 'description': 'Scroll 1..20 notches at a point in the observed foreground window. Requires fresh observation.', 'inputSchema': schema({'observation_id': OBS, 'x': COORD, 'y': COORD, 'notches': {'type': 'integer', 'minimum': -20, 'maximum': 20}}, ('observation_id', 'x', 'y', 'notches'))},
    {'name': 'desktop_window', 'description': 'Request activate/minimize/maximize/restore of a previously observed window. Never closes or kills programs. Requires fresh observation.', 'inputSchema': schema({'observation_id': OBS, 'window_id': {'type': 'string', 'minLength': 1, 'maxLength': 100}, 'action': {'enum': ['activate', 'minimize', 'maximize', 'restore']}}, ('observation_id', 'window_id', 'action'))},
]


class LazyWindowsBackend:
    def __init__(self):
        self.value = None
        self.indicator_hwnd = None

    def _get(self):
        if self.value is None:
            self.value = WindowsBackend(indicator_hwnd=self.indicator_hwnd)
        return self.value

    def set_indicator(self, hwnd):
        self.indicator_hwnd = hwnd
        if self.value is not None:
            self.value.indicator_hwnd = hwnd

    def inspect(self):
        return self._get().inspect()

    def screenshot(self):
        return self._get().screenshot()

    def invoke(self, *args, **kwargs):
        return self._get().invoke(*args, **kwargs)


class StopFileGate:
    """Read-only shared revocation marker; this process never removes it."""
    def __init__(self, value):
        self.path = Path(value)

    def blocked(self):
        path = self.path
        if (not path.is_absolute() or str(path).startswith(('\\\\', '//')) or
                any(':' in part for part in path.parts[1:])):
            return True
        try:
            # Inspect root-to-leaf without resolve/following reparse points.
            for item in tuple(reversed(path.parents)) + (path,):
                try:
                    info = item.lstat()
                except FileNotFoundError:
                    continue
                if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
                    return True
                if item != path and not stat.S_ISDIR(info.st_mode):
                    return True
                if item == path:
                    return True  # Any existing marker, file or directory, stops.
            return False
        except (OSError, ValueError):
            return True

    def check(self, controller):
        if self.blocked():
            if not controller.stopped:
                controller.stop()
            return True
        return False


def watch_stop_file(gate, controller, finished):
    while not finished.is_set():
        if gate.check(controller):
            return
        finished.wait(0.1)


def text_content(value):
    return {'type': 'text', 'text': json.dumps(value, ensure_ascii=True, separators=(',', ':'))}


def dispatch(request, controller):
    if type(request) is not dict or request.get('jsonrpc') != '2.0':
        return {'jsonrpc': '2.0', 'id': None, 'error': {'code': -32600, 'message': 'Invalid request'}}
    if 'id' not in request:
        return None  # Notifications NEVER mutate or inspect the real desktop.
    ident = request['id']
    if not ((type(ident) is int and abs(ident) <= 9007199254740991) or
            (type(ident) is str and len(ident) <= 128)):
        return {'jsonrpc': '2.0', 'id': None, 'error': {'code': -32600, 'message': 'Invalid request id'}}
    response = {'jsonrpc': '2.0', 'id': ident}
    method, params = request.get('method'), request.get('params', {})
    if type(params) is not dict:
        response['error'] = {'code': -32602, 'message': 'Invalid params'}
        return response
    if method == 'initialize':
        response['result'] = {'protocolVersion': PROTOCOL_VERSION,
                              'capabilities': {'tools': {}},
                              'serverInfo': {'name': 'prostranstvo-desktop-control-prototype', 'version': '0.1.0'},
                              'instructions': 'Use only within the owner-authorized task. Observe before every mutation. Input affects the local user desktop. Login, 2FA, UAC, host approvals and site rules remain in force. An issued input is not verified completion.'}
    elif method == 'ping':
        response['result'] = {}
    elif method == 'tools/list':
        response['result'] = {'tools': TOOLS}
    elif method == 'tools/call':
        name, args = params.get('name'), params.get('arguments', {})
        try:
            if (set(params) - {'name', 'arguments', '_meta'} or
                    ('_meta' in params and type(params['_meta']) is not dict) or
                    type(name) is not str or type(args) is not dict):
                raise ControlError('invalid_arguments')
            if name in ('desktop_status', 'desktop_stop', 'desktop_observe'):
                if args:
                    raise ControlError('invalid_arguments')
                value = {'desktop_status': controller.status,
                         'desktop_stop': controller.stop,
                         'desktop_observe': controller.observe}[name]()
            elif name in {tool['name'] for tool in TOOLS}:
                value = controller.act(name, args)
            else:
                raise ControlError('unknown_tool')
            if name == 'desktop_observe':
                value = dict(value)
                image = value.pop('image')
                response['result'] = {'content': [text_content(value), image], 'isError': False}
            else:
                response['result'] = {'content': [text_content(value)], 'isError': False}
        except ControlError as error:
            response['result'] = {'content': [text_content({'error': error.code})], 'isError': True}
        except Exception:
            response['result'] = {'content': [text_content({'error': 'operation_failed_outcome_unknown'})], 'isError': True}
    else:
        response['error'] = {'code': -32601, 'message': 'Method not found'}
    return response


def unique_object(items):
    value = {}
    for key, item in items:
        if key in value:
            raise ValueError('duplicate key')
        value[key] = item
    return value


def bounded(value, depth=0, budget=None):
    if budget is None:
        budget = [1000]
    budget[0] -= 1
    if depth > 12 or budget[0] < 0:
        raise ValueError('oversized structure')
    if isinstance(value, str) and len(value) > 8192:
        raise ValueError('oversized string')
    if isinstance(value, dict):
        for key, item in value.items():
            bounded(key, depth+1, budget)
            bounded(item, depth+1, budget)
    elif isinstance(value, list):
        for item in value:
            bounded(item, depth+1, budget)


def serve(controller, incoming, outgoing):
    """Binary streams, one bounded UTF-8 JSON object per line, no diagnostics stdout."""
    def write(value):
        outgoing.write(json.dumps(value, ensure_ascii=True, separators=(',', ':')).encode('utf-8')+b'\n')
        outgoing.flush()
    while True:
        try:
            line = incoming.readline(MAX_LINE_BYTES+1)
        except Exception:
            return
        if not line:
            return
        if len(line) > MAX_LINE_BYTES:
            write({'jsonrpc': '2.0', 'id': None, 'error': {'code': -32700, 'message': 'Request too large; connection closed'}})
            return
        try:
            value = json.loads(line.decode('utf-8', 'strict'), object_pairs_hook=unique_object,
                               parse_constant=lambda _: (_ for _ in ()).throw(ValueError('constant')))
            bounded(value)
        except (ValueError, UnicodeError, RecursionError):
            write({'jsonrpc': '2.0', 'id': None, 'error': {'code': -32700, 'message': 'Invalid bounded JSON'}})
            continue
        response = dispatch(value, controller)
        if response is not None:
            write(response)


def metadata_stderr(event):
    # Deliberately not request contents, text, titles, screenshots or IDs.
    try:
        sys.stderr.write(json.dumps(event, ensure_ascii=True, separators=(',', ':'))+'\n')
        sys.stderr.flush()
    except Exception:
        pass  # An unavailable log sink must not expose request data via traceback.


def run_with_indicator(controller):
    """Visible input-enabled session; server starts only after this window maps.

    Not launched during synthetic tests. Closing it revokes the session; the
    stdio connection may remain open for status but cannot re-enable itself.
    """
    # DPI awareness and unelevated-token validation precede every HWND.
    controller.backend._get()
    import tkinter as tk
    window = tk.Tk()
    window.title('Пространство — управление экраном')
    window.attributes('-topmost', True)
    window.resizable(False, False)
    label = tk.Label(window, text='ИИ может управлять этим рабочим столом\nНе пользуйтесь мышью и клавиатурой одновременно.', padx=16, pady=12)
    label.pack()
    def stop():
        controller.stop()
        label.configure(text='Управление и снимки остановлены.\nДля нового сеанса нужен перезапуск MCP.')
        button.configure(state='disabled')
    button = tk.Button(window, text='СТОП — отключить управление', command=stop, padx=12, pady=8)
    button.pack(padx=12, pady=(0, 12))
    window.protocol('WM_DELETE_WINDOW', stop)
    window.update_idletasks()
    window.update()
    if not window.winfo_ismapped():
        controller.stop()
        window.destroy()
        raise ControlError('visible_indicator_required')
    # winfo_id is the Tk client HWND. Visibility/IsIconic of its top-level
    # owner are checked again in native inspection, not just by this UI timer.
    controller.backend.set_indicator(int(window.winfo_id()))
    finished = threading.Event()
    def worker():
        try:
            serve(controller, sys.stdin.buffer, sys.stdout.buffer)
        finally:
            controller.stop()
            finished.set()
    threading.Thread(target=worker, name='local-desktop-stdio', daemon=True).start()
    def poll():
        if finished.is_set():
            window.destroy()
            return
        if window.state() != 'normal' and not controller.stopped:
            stop()
        if controller.stopped and str(button.cget('state')) != 'disabled':
            label.configure(text='Управление и снимки остановлены.\nДля нового сеанса нужен перезапуск MCP.')
            button.configure(state='disabled')
        if controller.clock() >= controller.deadline and not controller.stopped:
            stop()
        window.after(200, poll)
    window.after(200, poll)
    try:
        window.mainloop()
    finally:
        controller.stop()


def main(argv=None):
    parser = argparse.ArgumentParser(description='Opt-in stdio desktop control. No service or network listener.')
    parser.add_argument('--enable-input', action='store_true', help='Explicitly allow input for this bounded visible local session')
    parser.add_argument('--session-seconds', type=int, default=1800)
    parser.add_argument('--stop-file', help='Absolute local no-links STOP marker. Existence/errors revoke; removal never resumes this process.')
    args = parser.parse_args(argv)
    if not 1 <= args.session_seconds <= 3600:
        parser.error('session-seconds must be 1..3600')
    controller = Controller(LazyWindowsBackend(), enabled=args.enable_input,
                            session_seconds=args.session_seconds, audit=metadata_stderr)
    finished = threading.Event()
    try:
        if args.stop_file:
            gate = StopFileGate(args.stop_file)
            if gate.check(controller):
                raise ControlError('stop_marker_present_or_unsafe')
            threading.Thread(target=watch_stop_file, args=(gate, controller, finished),
                             name='local-desktop-stop-marker', daemon=True).start()
        if args.enable_input:
            if os.name != 'nt':
                raise ControlError('windows_required')
            run_with_indicator(controller)
        else:
            serve(controller, sys.stdin.buffer, sys.stdout.buffer)
    except Exception:
        controller.stop()
        sys.stderr.write('Desktop control failed closed; no input session available.\n')
        return 1
    finally:
        finished.set()
        controller.stop()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
