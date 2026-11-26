"""Real Tk/stdio on nonce-owned NON-input desktop only; never injects input."""
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest

SOURCE = Path(__file__).resolve().parents[1] / 'src'
sys.path.insert(0, str(SOURCE))


def desktop_names():
    user = ctypes.WinDLL('user32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.GetCurrentThreadId.restype = wintypes.DWORD
    user.GetThreadDesktop.argtypes = [wintypes.DWORD]
    user.GetThreadDesktop.restype = wintypes.HANDLE
    user.OpenInputDesktop.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    user.OpenInputDesktop.restype = wintypes.HANDLE
    user.CloseDesktop.argtypes = [wintypes.HANDLE]
    user.GetUserObjectInformationW.argtypes = [wintypes.HANDLE, ctypes.c_int,
        ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    def name(handle):
        buffer = ctypes.create_unicode_buffer(260)
        needed = wintypes.DWORD()
        if not handle or not user.GetUserObjectInformationW(handle, 2, buffer,
                ctypes.sizeof(buffer), ctypes.byref(needed)):
            raise RuntimeError('Desktop identity unavailable')
        return buffer.value
    handle = user.OpenInputDesktop(0, False, 1)
    try:
        return name(user.GetThreadDesktop(kernel.GetCurrentThreadId())), name(handle)
    finally:
        if handle:
            user.CloseDesktop(handle)


class DesktopUiNativeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if os.name != 'nt':
            raise unittest.SkipTest('Real Windows required')
        thread, incoming = desktop_names()
        if not re.fullmatch(r'CodexQA_[0-9a-f]{32}', thread) or thread == incoming:
            raise unittest.SkipTest('Nonce-owned non-input QA desktop required')
        cls.initial = thread, incoming

    def tearDown(self):
        self.assertEqual(desktop_names(), self.initial)

    def test_actual_panel_stop_and_future_permission_on_temporary_marker(self):
        import tkinter as tk
        from desktop_control_ui import DesktopControlPanel, StopMarker
        with tempfile.TemporaryDirectory() as directory:
            marker = StopMarker(Path(directory) / 'owned' / 'STOP')
            window = tk.Tk()
            panel = None
            try:
                window.geometry('960x650+10+10')
                panel = DesktopControlPanel(window, authorize_parent=lambda _: True, marker=marker)
                panel.pack(fill='both', expand=True)
                window.update_idletasks()
                window.update()
                self.assertTrue(panel.winfo_ismapped())
                self.assertIn('[mcp_servers.prostranstvo_desktop]', panel.configuration.get('1.0', 'end'))
                self.assertTrue(panel.stop_access())
                self.assertEqual(marker.path.read_bytes(), b'STOP\n')
                self.assertFalse(marker.status()['client_connection_verified'])
                self.assertTrue(panel.allow_future())
                self.assertFalse(marker.path.exists())
                panel.close()
                self.assertIsNone(panel._timer)
            finally:
                if panel is not None:
                    panel.close()
                window.destroy()

    def test_actual_indicator_stdio_rejects_non_input_desktop_before_capture(self):
        requests = [
            {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize'},
            {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call', 'params': {'name': 'desktop_status'}},
            {'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call', 'params': {'name': 'desktop_observe'}},
            {'jsonrpc': '2.0', 'id': 4, 'method': 'tools/call', 'params': {'name': 'desktop_stop'}},
            {'jsonrpc': '2.0', 'id': 5, 'method': 'tools/call', 'params': {'name': 'desktop_status'}},
        ]
        with tempfile.TemporaryDirectory() as directory:
            process = subprocess.run([sys.executable, '-B', str(SOURCE / 'desktop_control_mcp.py'),
                '--enable-input', '--session-seconds', '20', '--stop-file', str(Path(directory) / 'STOP')],
                input=''.join(json.dumps(item) + '\n' for item in requests), text=True,
                encoding='utf-8', capture_output=True, timeout=25,
                creationflags=subprocess.CREATE_NO_WINDOW)
        self.assertEqual(process.returncode, 0, process.stderr[-500:])
        replies = [json.loads(line) for line in process.stdout.splitlines()]
        self.assertEqual([item['id'] for item in replies], [1, 2, 3, 4, 5])
        status = json.loads(replies[1]['result']['content'][0]['text'])
        self.assertTrue(status['input_enabled'])
        observation = replies[2]['result']
        self.assertTrue(observation['isError'])
        code = json.loads(observation['content'][0]['text'])['error']
        self.assertEqual(code, 'unlocked_default_desktop_required')
        self.assertFalse(any(part['type'] == 'image' for part in observation['content']))
        final = json.loads(replies[4]['result']['content'][0]['text'])
        self.assertTrue(final['stopped'])
        self.assertFalse(final['input_enabled'])


if __name__ == '__main__':
    unittest.main()
