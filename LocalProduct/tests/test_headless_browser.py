"""Mock transport and Node with a fake engine; never start a browser or site."""
import json
import os
from pathlib import Path
import queue
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import headless_browser as browser


class FakeOutput:
    def __init__(self):
        self.lines = queue.Queue()

    def readline(self, size=None):
        return self.lines.get()

    def close(self):
        self.lines.put('')


class FakeProcess:
    def __init__(self, stall=False):
        self.stdout = FakeOutput()
        self.stdin = Mock()
        self.stdin.write.side_effect = self.write
        self.returncode = None
        self.requests = []
        self.stall = stall

    def write(self, line):
        request = json.loads(line)
        self.requests.append(request)
        if 'id' not in request:
            return
        if request['method'] == 'initialize':
            result = {'protocolVersion': '2024-11-05', 'capabilities': {}}
        elif self.stall:
            return
        else:
            name = request['params']['name']
            data = {'tool': name, 'browser_started': name != 'agent_browser_close'}
            result = {'content': [{'type': 'text', 'text': json.dumps(data)}]}
            if name == 'agent_browser_click':
                result['isError'] = True
        self.stdout.lines.put(json.dumps({'jsonrpc': '2.0', 'id': request['id'], 'result': result}))

    def poll(self):
        return self.returncode

    def terminate(self):
        self.returncode = 0
        self.stdout.close()

    kill = terminate

    def wait(self, timeout=None):
        return self.returncode


class HeadlessBrowserTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        module = self.root / 'LocalProduct/src'
        module.mkdir(parents=True)
        worker = module.parent / 'assets/agent-browser/headless_bridge.mjs'
        engine = module.parent / browser.ENGINE_RELATIVE
        self.node = self.root / 'node.exe'
        for file in (worker, engine, self.node):
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text('fixture', encoding='utf-8')
        self.addCleanup(patch.stopall)
        patch.object(browser, 'MODULE_ROOT', module).start()
        patch.object(browser.paths, 'APP_ROOT', module).start()
        patch.object(browser.paths, 'SOURCE_ROOT', module).start()
        patch.dict(os.environ, {'LOCALAPPDATA': str(self.root / 'local')}, clear=True).start()
        patch.object(browser.shutil, 'which', return_value=str(self.node)).start()
        self.processes = []
        def factory(*args, **kwargs):
            process = FakeProcess()
            self.processes.append(process)
            return process
        self.spawn = patch.object(browser.subprocess, 'Popen', side_effect=factory).start()
        self.client = browser.HeadlessBrowser(timeout=.1)
        self.addCleanup(self.client.close)

    def test_status_checks_dependencies_without_process_or_profile_creation(self):
        result = self.client.call('agent_browser_status', {})
        self.assertTrue(result['dependency_ready'])
        self.assertFalse(result['browser_started'])
        self.assertEqual(result['allowed_hosts'], list(browser.ALLOWED_HOSTS))
        self.spawn.assert_not_called()
        self.assertFalse((self.root / 'local').exists())

    def test_handshake_unpacks_json_reuses_process_and_closes_owned_session(self):
        self.assertEqual(self.client.call('agent_browser_open', {'url': 'https://example.com'})['tool'],
                         'agent_browser_open')
        self.client.call('agent_browser_snapshot', {})
        result = self.client.call('agent_browser_click', {'role': 'button', 'name': 'Fixture'})
        self.assertTrue(result['isError'])
        self.assertTrue(self.client.call('agent_browser_status')['browser_started'])
        self.spawn.assert_called_once()
        command = self.spawn.call_args.args[0]
        options = self.spawn.call_args.kwargs
        self.assertEqual(command[0], str(self.node))
        self.assertTrue(command[1].endswith('assets/agent-browser/headless_bridge.mjs') or
                        command[1].endswith('assets\\agent-browser\\headless_bridge.mjs'))
        self.assertFalse(options['shell'])
        self.assertEqual(options['creationflags'], getattr(browser.subprocess, 'CREATE_NO_WINDOW', 0))
        self.assertEqual(options['env']['PROSTRANSTVO_BROWSER_ALLOWED_HOSTS'], ','.join(browser.ALLOWED_HOSTS))
        self.assertEqual(Path(options['env']['PROSTRANSTVO_BROWSER_PROFILE']),
                         self.root / 'local/ProstranstvoAgentBrowser/profiles/edge')
        self.assertEqual(self.processes[0].requests[0]['method'], 'initialize')
        self.client.close()
        self.assertIsNotNone(self.processes[0].returncode)
        self.assertEqual(self.processes[0].requests[-1]['params']['name'], 'agent_browser_close')

    def test_rejects_hosts_schemes_unknown_tools_and_paths_before_start(self):
        for name, args in (
            ('agent_browser_open', {'url': 'https://evil.test'}),
            ('agent_browser_open', {'url': 'https://example.com.evil.test'}),
            ('agent_browser_open', {'url': 'http://example.com'}),
            ('agent_browser_open', {'url': 'https://example.com', 'engine': 'C:/evil.mjs'}),
            ('agent_browser_snapshot', {'script': 'document.cookie'}),
            ('agent_browser_screenshot', {}),
            ('agent_browser_fill', {'label': 'Fixture', 'text': 3}),
        ):
            with self.subTest(name=name, args=args), self.assertRaises(ValueError):
                self.client.call(name, args)
        self.spawn.assert_not_called()

    def test_invalid_engine_override_is_not_ready_and_does_not_fall_back(self):
        os.environ['PROSTRANSTVO_BROWSER_ENGINE'] = 'relative/index.mjs'
        result = self.client.call('agent_browser_status')
        self.assertFalse(result['dependency_ready'])
        self.assertIn('absolute', result['missing_dependencies'][0])
        with self.assertRaises(ValueError):
            self.client.call('agent_browser_open', {'url': 'https://example.com'})
        self.spawn.assert_not_called()

    def test_timeout_reaps_process_and_next_call_uses_fresh_transport(self):
        stalled, fresh = FakeProcess(stall=True), FakeProcess()
        self.spawn.side_effect = [stalled, fresh]
        with self.assertRaises(TimeoutError):
            self.client.call('agent_browser_open', {'url': 'https://example.com'})
        self.assertIsNotNone(stalled.returncode)
        result = self.client.call('agent_browser_open', {'url': 'https://example.com'})
        self.assertEqual(result['tool'], 'agent_browser_open')
        self.assertEqual(self.spawn.call_count, 2)

    def test_known_dead_worker_restarts_before_next_open_without_replaying_old_calls(self):
        self.client.call('agent_browser_open', {'url': 'https://example.com'})
        dead = self.processes[0]
        dead.terminate()
        status = self.client.call('agent_browser_status')
        self.assertFalse(status['browser_started'])
        self.assertEqual(self.spawn.call_count, 1)
        self.client.call('agent_browser_open', {'url': 'https://example.com'})
        self.assertEqual(self.spawn.call_count, 2)
        for process in self.processes:
            opens = [r for r in process.requests if r.get('params', {}).get('name') == 'agent_browser_open']
            self.assertEqual(len(opens), 1)



FAKE_ENGINE = r"""
import fs from 'node:fs';
import {EventEmitter} from 'node:events';
const owners = [], routes = [];
const mode = process.env.PROSTRANSTVO_FAKE_MODE || '';
let nextPage = 0;
function save() {
  fs.writeFileSync(process.env.PROSTRANSTVO_FAKE_PROBE, JSON.stringify({
    contexts: owners.map(c => ({closed:c.closed,profile:c.profile,options:c.options,
      pages:c.all.map(p => ({id:p.id,closed:p.closed,url:p.current,dialogs:p.listenerCount('dialog')}))})),
    routes
  }));
}
class Page extends EventEmitter {
  constructor(owner) { super(); this.owner=owner;this.id=++nextPage;this.closed=false;this.current='about:blank'; }
  isClosed() { return this.closed; }
  url() { return this.current; }
  async close() { if (!this.closed) {this.closed=true;this.emit('close');save();} }
  async goto(url) {
    if (this.closed) throw new Error('closed_page');
    const navigate = async target => {
      let blocked = false;
      await this.owner.gate({
        request:() => ({isNavigationRequest:()=>true,url:()=>target}),
        continue:async()=>{routes.push({url:target,action:'continue'});save();},
        abort:async()=>{blocked=true;routes.push({url:target,action:'abort'});save();}
      });
      if (blocked) throw new Error('blocked_by_client');
    };
    await navigate(url);
    if (url.endsWith('/redirect')) await navigate('https://evil.invalid/leak');
    this.current=url;save();
  }
  async title() { if(this.closed) throw new Error('closed_page');return 'Fixture title'; }
  locator() { return {innerText:async()=>{if(this.closed)throw new Error('closed_page');return 'Fixture body';}}; }
  getByRole(role, {name}) {
    return {count:async()=>1,click:async()=>{
      if(name==='Close page') await this.close();
      if(name==='Crash page') {this.emit('crash');save();}
      if(name==='Close context') await this.owner.close();
      if(name==='Popup') await this.owner.newPage();
    }};
  }
}
class Context extends EventEmitter {
  constructor(profile, options) {
    super();this.profile=profile;this.options=options;this.closed=false;this.all=[];
    if(mode!=='empty') this.all.push(new Page(this));
  }
  pages() { return this.all.filter(p=>!p.closed); }
  async newPage() {
    if(this.closed)throw new Error('closed_context');
    const p=new Page(this);this.all.push(p);this.emit('page',p);save();return p;
  }
  async route(pattern, handler) {
    if(mode==='setup-failure' && owners.length===1)throw new Error('fixture_route_failed');
    this.gate=handler;
  }
  async close() {
    if(!this.closed) {
      this.closed=true;
      for(const p of this.all)await p.close();
      this.emit('close');save();
    }
  }
}
export const chromium = {launchPersistentContext:async(profile,options)=>{
  const owner=new Context(profile,options);owners.push(owner);save();return owner;
}};
"""


class NodeWorkerFakeEngineTests(unittest.TestCase):
    """Execute only Node plus the fixture module above, without Playwright."""
    def setUp(self):
        self.node = browser.shutil.which('node')
        if not self.node:
            self.skipTest('Node unavailable for fake-engine tests')
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.engine = self.root / 'index.mjs'
        self.engine.write_text(FAKE_ENGINE, encoding='utf-8')
        self.worker = Path(__file__).resolve().parents[1] / 'assets/agent-browser/headless_bridge.mjs'

    def run_worker(self, calls, mode=''):
        probe = self.root / 'probe.json'
        env = dict(os.environ, PROSTRANSTVO_BROWSER_ENGINE=str(self.engine),
                   PROSTRANSTVO_BROWSER_PROFILE=str(self.root / 'owned-profile'),
                   PROSTRANSTVO_BROWSER_ALLOWED_HOSTS='example.com',
                   PROSTRANSTVO_FAKE_MODE=mode, PROSTRANSTVO_FAKE_PROBE=str(probe))
        requests = [{'jsonrpc':'2.0','id':1,'method':'initialize','params':{}}]
        requests += [{'jsonrpc':'2.0','id':i+2,'method':'tools/call',
                      'params':{'name':name,'arguments':args}} for i,(name,args) in enumerate(calls)]
        process = browser.subprocess.run(
            [self.node, str(self.worker)], input='\n'.join(map(json.dumps, requests))+'\n',
            capture_output=True, text=True, encoding='utf-8', timeout=8, env=env,
            creationflags=getattr(browser.subprocess, 'CREATE_NO_WINDOW', 0))
        self.assertEqual(process.returncode, 0, process.stderr)
        replies = [json.loads(line) for line in process.stdout.splitlines()]
        self.assertEqual(len(replies), len(requests), process.stdout)
        results = [reply['result'] for reply in replies[1:]]
        values = [json.loads(result['content'][0]['text']) for result in results]
        data = json.loads(probe.read_text(encoding='utf-8'))
        self.assertTrue(all(c['closed'] for c in data['contexts']), 'EOF must close owned contexts')
        return results, values, data

    def test_initial_blank_or_new_page_is_adopted_once_with_headless_options(self):
        for mode in ('initial-blank', 'empty'):
            with self.subTest(mode=mode):
                results, values, data = self.run_worker([
                    ('agent_browser_open', {'url':'https://example.com/'}),
                    ('agent_browser_snapshot', {}),
                    ('agent_browser_close', {})], mode)
                self.assertFalse(any(result.get('isError') for result in results))
                self.assertEqual(values[1]['title'], 'Fixture title')
                self.assertEqual(len(data['contexts']), 1)
                context = data['contexts'][0]
                self.assertTrue(context['options']['headless'])
                self.assertEqual(context['options']['channel'], 'msedge')
                self.assertEqual(context['options']['serviceWorkers'], 'block')
                self.assertEqual(len(context['pages']), 1)
                self.assertEqual(context['pages'][0]['dialogs'], 1)

    def test_closed_or_crashed_main_page_reopens_without_popup_handler_closing_replacement(self):
        for name in ('Close page', 'Crash page'):
            with self.subTest(name=name):
                results, values, data = self.run_worker([
                    ('agent_browser_open', {'url':'https://example.com/'}),
                    ('agent_browser_click', {'role':'button','name':name}),
                    ('agent_browser_open', {'url':'https://example.com/reopened'}),
                    ('agent_browser_snapshot', {})])
                self.assertFalse(any(result.get('isError') for result in results))
                self.assertEqual(values[-1]['url'], 'https://example.com/reopened')
                self.assertEqual(len(data['contexts']), 1)
                self.assertEqual(len(data['contexts'][0]['pages']), 2)
                self.assertTrue(all(p['dialogs']==1 for p in data['contexts'][0]['pages']))

    def test_closed_context_is_not_reported_running_and_open_creates_a_fresh_context(self):
        results, values, data = self.run_worker([
            ('agent_browser_open', {'url':'https://example.com/'}),
            ('agent_browser_click', {'role':'button','name':'Close context'}),
            ('agent_browser_status', {}),
            ('agent_browser_open', {'url':'https://example.com/reopened'})])
        self.assertFalse(any(result.get('isError') for result in results))
        self.assertFalse(values[2]['browser_started'])
        self.assertEqual(len(data['contexts']), 2)

    def test_partial_setup_failure_is_closed_and_next_open_retries_cleanly(self):
        results, values, data = self.run_worker([
            ('agent_browser_open', {'url':'https://example.com/'}),
            ('agent_browser_status', {}),
            ('agent_browser_open', {'url':'https://example.com/retry'})], 'setup-failure')
        self.assertTrue(results[0]['isError'])
        self.assertFalse(values[1]['browser_started'])
        self.assertFalse(results[2].get('isError'))
        self.assertEqual(len(data['contexts']), 2)

    def test_redirect_aborts_before_navigation_and_popup_never_replaces_main_page(self):
        results, values, data = self.run_worker([
            ('agent_browser_open', {'url':'https://example.com/redirect'}),
            ('agent_browser_open', {'url':'https://example.com/'}),
            ('agent_browser_click', {'role':'button','name':'Popup'}),
            ('agent_browser_snapshot', {})])
        self.assertTrue(results[0]['isError'])
        self.assertFalse(any(result.get('isError') for result in results[1:]))
        self.assertEqual(values[-1]['url'], 'https://example.com/')
        bad = [route for route in data['routes'] if 'evil.invalid' in route['url']]
        self.assertEqual(bad, [{'url':'https://evil.invalid/leak','action':'abort'}])
        self.assertTrue(data['contexts'][0]['pages'][1]['closed'])


if __name__ == '__main__':
    unittest.main()
