"""Lazy JSON-line gateway to the owned, headless browser; no downloads or GUI."""
from __future__ import annotations

import atexit
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import threading
import time
from urllib.parse import urlsplit

from activity_core import no_links
from browser_access import validate_https_url
from browser_session import owned_profile_path, profile_lease, manual_login_pending
import paths

ALLOWED_HOSTS = ('example.com', 'www.facebook.com', 'facebook.com')
MODULE_ROOT = Path(__file__).absolute().parent
ENGINE_RELATIVE = Path('build/web-test-tools/playwright-core-1.65.0/package/index.mjs')
ARGUMENTS = {'agent_browser_status': (), 'agent_browser_open': ('url',),
             'agent_browser_snapshot': (), 'agent_browser_click': ('role', 'name'),
             'agent_browser_fill': ('label', 'text'), 'agent_browser_close': ()}


def _file(candidates, expected_name=None):
    for candidate in candidates:
        path = Path(candidate)
        if not path.is_absolute():
            raise ValueError('browser_dependency_path_must_be_absolute')
        if path.exists():
            no_links(path, True)
            if (expected_name and path.name != expected_name) or path.stat().st_size == 0:
                raise ValueError('invalid_browser_dependency_file')
            return str(path)
    raise RuntimeError('browser_dependency_not_found')


def _dependencies():
    node = shutil.which('node')
    if not node:
        raise RuntimeError('node_not_found')
    worker = _file((root / 'assets/agent-browser/headless_bridge.mjs'
                    for root in (MODULE_ROOT, MODULE_ROOT.parent, getattr(paths, 'APP_ROOT', MODULE_ROOT))),
                   'headless_bridge.mjs')
    override = os.environ.get('PROSTRANSTVO_BROWSER_ENGINE')
    engine = _file((Path(override),) if override else (
        MODULE_ROOT.parent / ENGINE_RELATIVE,
        paths.SOURCE_ROOT.parent / 'LocalProduct' / ENGINE_RELATIVE), 'index.mjs')
    return _file((Path(node),)), worker, engine


class HeadlessBrowser:
    def __init__(self, timeout=30):
        self.timeout = timeout
        self._lock = threading.Lock()
        self._process = None
        self._responses = None
        self._id = 0
        self._profile_lease = None

    def _start(self):
        node, worker, engine = _dependencies()
        env = dict(os.environ)
        lease = profile_lease(headless=True)
        lease.__enter__()
        self._profile_lease = lease
        profile = owned_profile_path()
        env.update(PROSTRANSTVO_BROWSER_ENGINE=engine,
                   PROSTRANSTVO_BROWSER_PROFILE=str(profile),
                   PROSTRANSTVO_BROWSER_ALLOWED_HOSTS=','.join(ALLOWED_HOSTS))
        self._process = subprocess.Popen(
            [node, worker], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, encoding='utf-8', bufsize=1,
            shell=False, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0), env=env)
        self._responses = queue.Queue()
        def read(process=self._process, responses=self._responses):
            try:
                while True:
                    line = process.stdout.readline(2_000_001)
                    if not line:
                        break
                    if len(line) > 2_000_000:
                        raise ValueError('browser_response_too_large')
                    responses.put(line)
            except (OSError, ValueError):
                pass
            finally:
                responses.put(None)
                # Node may exit on its idle timer while the host stays alive.
                # Release this gateway's lease without waiting for another RPC.
                # Queue EOF first so an in-flight RPC can leave its lock.
                with self._lock:
                    if self._process is process:
                        try:
                            self._stop()
                        except RuntimeError:
                            # Retain the handle/lease for explicit recovery.
                            pass
        threading.Thread(target=read, daemon=True, name='ProstranstvoBrowserRPC').start()
        self._rpc('initialize', {'protocolVersion': '2024-11-05', 'capabilities': {},
                                'clientInfo': {'name': 'prostranstvo', 'version': '1'}})
        self._send({'jsonrpc': '2.0', 'method': 'notifications/initialized', 'params': {}})

    def _send(self, value):
        self._process.stdin.write(json.dumps(value, ensure_ascii=False) + '\n')
        self._process.stdin.flush()

    def _rpc(self, method, params):
        self._id += 1
        self._send({'jsonrpc': '2.0', 'id': self._id, 'method': method, 'params': params})
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                line = self._responses.get(timeout=max(0, deadline - time.monotonic()))
            except queue.Empty as error:
                raise TimeoutError('headless_browser_timeout') from error
            if line is None:
                raise RuntimeError('headless_browser_worker_closed')
            response = json.loads(line)
            if 'method' in response and 'id' not in response:
                continue
            if response.get('id') != self._id:
                raise RuntimeError('headless_browser_response_id_mismatch')
            if 'error' in response:
                raise RuntimeError('headless_browser_rpc_error: ' + str(response['error']))
            return response['result']

    def call(self, name, args=None):
        args = {} if args is None else args
        if name not in ARGUMENTS or not isinstance(args, dict) or set(args) != set(ARGUMENTS[name]):
            raise ValueError('invalid_headless_browser_tool_arguments')
        if any(not isinstance(value, str) or len(value) > 20_000 for value in args.values()):
            raise ValueError('invalid_headless_browser_tool_arguments')
        if name == 'agent_browser_open':
            url = validate_https_url(args['url'])
            parsed = urlsplit(url)
            if parsed.hostname not in ALLOWED_HOSTS or parsed.port not in (None, 443):
                raise ValueError('headless_browser_host_not_allowed')
            args = {'url': url}
        with self._lock:
            # Recover a worker known to have exited before sending a new call.
            # Never replay a call that may already have reached a live worker.
            if self._process is not None and self._process.poll() is not None:
                self._stop()
            if name == 'agent_browser_close' and self._process is None:
                return {'closed': True, 'browser_started': False}
            pending = False if name == 'agent_browser_close' else manual_login_pending()
            if pending and name not in ('agent_browser_status', 'agent_browser_close'):
                raise RuntimeError('manual_browser_login_in_progress')
            if name == 'agent_browser_status' and self._process is None:
                result = {'dependency_ready': False, 'browser_started': False,
                          'browser_readiness': 'not_verified', 'headless': True,
                          'allowed_hosts': list(ALLOWED_HOSTS), 'manual_login_pending': pending}
                try:
                    _dependencies()
                    result['dependency_ready'] = True
                except (OSError, ValueError, RuntimeError) as error:
                    result['missing_dependencies'] = [str(error)]
                return result
            try:
                if self._process is None:
                    self._start()
                result = self._rpc('tools/call', {'name': name, 'arguments': args})
                content = result.get('content', []) if isinstance(result, dict) else []
                decoded = None
                if (isinstance(result, dict) and not result.get('isError')
                        and len(content) == 1 and content[0].get('type') == 'text'):
                    try:
                        decoded = json.loads(content[0]['text'])
                    except (ValueError, KeyError):
                        pass
                if name == 'agent_browser_close' and (not isinstance(result, dict) or not result.get('isError')):
                    if not isinstance(decoded, dict) or decoded.get('status') != 'owned_browser_closed':
                        return {'isError': True, 'content': [{'type': 'text', 'text':
                                '{"error":"owned_browser_close_not_confirmed"}'}]}
                    self._stop()
                if decoded is not None:
                    if name == 'agent_browser_status' and isinstance(decoded, dict):
                        decoded['manual_login_pending'] = pending
                    return decoded
                return result
            except Exception:
                # A late reply must never be consumed as the next request's answer.
                self._stop()
                raise

    def _stop(self):
        process = self._process
        if process is not None:
            try:
                if process.poll() is None:
                    process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)
            except Exception as error:
                # Do not discard a live process or its profile lease on a
                # failed terminate/kill. Another controller must stay blocked.
                if process.poll() is None:
                    raise RuntimeError('browser_worker_stop_not_confirmed') from error
            if process.poll() is None:
                raise RuntimeError('browser_worker_stop_not_confirmed')
        self._process = None
        try:
            for stream in (process.stdin, process.stdout) if process is not None else ():
                if stream is not None:
                    try:
                        stream.close()
                    except (OSError, ValueError):
                        pass
        finally:
            self._responses = None
            lease, self._profile_lease = self._profile_lease, None
            if lease is not None:
                lease.__exit__(None, None, None)

    def close(self):
        try:
            self.call('agent_browser_close', {})
        except Exception:
            pass
        finally:
            # Service/destructor cleanup is best-effort, not proof that the
            # browser accepted the RPC close request.
            with self._lock:
                try:
                    self._stop()
                except Exception:
                    pass


_browser = HeadlessBrowser()


def call(name, args=None):
    return _browser.call(name, args)


atexit.register(_browser.close)
