import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
"""Local HTTP security and editor lifecycle fixtures; never opens a browser."""
from contextlib import redirect_stdout
import hashlib
import http.client
import io
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest import mock
from pet_tricks import FileLock, PetTrickStore
from pet_tricks_server import EditorServer, main


class TrickHTTPTests(unittest.TestCase):
    def setUp(self):
        base = Path(__file__).parent / 'tests-work'; base.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix='tricks-http-', dir=base)
        self.root = Path(self.temp.name)
        assets = self.root / 'assets'
        (assets/'tricks').mkdir(parents=True); (assets/'blockly'/'msg').mkdir(parents=True)
        for name, body in (('index.html', '<!doctype html><title>Fixture</title>'), ('editor.js', '/*fixture*/'), ('editor.css', '/*fixture*/')):
            (assets/'tricks'/name).write_text(body, encoding='utf-8')
        manifest = {'version':1, 'package':'blockly', 'package_version':'13.4.0','license':'Apache-2.0','files':{}}
        for name, body in (('blockly_compressed.js', b'/*pinned fixture*/'), ('msg/ru.js', b'/*RU fixture*/'), ('LICENSE', b'fixture license')):
            (assets/'blockly'/name).write_bytes(body)
            manifest['files'][name]={'size':len(body),'sha256':hashlib.sha256(body).hexdigest()}
        (assets/'blockly'/'asset-manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
        self.store = PetTrickStore(self.root/'state')
        self.server = EditorServer(self.store, assets)
        self.thread = threading.Thread(target=self.server.serve, daemon=True)
        self.thread.start()
    def tearDown(self):
        if self.thread.is_alive(): self.server.stop_async(); self.thread.join(timeout=4)
        self.assertFalse(self.thread.is_alive())
        self.temp.cleanup()
    def request(self, method, path, value=None, *, headers=None, authenticated=True, origin=True):
        values = {}
        if authenticated: values['Authorization']='Bearer '+self.server.token
        if origin: values['Origin']=self.server.origin
        if value is not None:
            body=json.dumps(value).encode(); values['Content-Type']='application/json'
        else: body=None
        values.update(headers or {})
        connection=http.client.HTTPConnection('127.0.0.1',self.server.server_address[1],timeout=3)
        try:
            connection.request(method,path,body=body,headers=values)
            response=connection.getresponse(); data=response.read()
            return response.status,dict(response.getheaders()),data
        finally: connection.close()

    def test_bind_loopback_and_static_allowlist_and_no_token_in_html(self):
        self.assertEqual(self.server.server_address[0],'127.0.0.1')
        status,headers,body=self.request('GET','/',authenticated=False,origin=False)
        self.assertEqual(status,200); self.assertNotIn(self.server.token.encode(),body)
        self.assertEqual(headers['X-Frame-Options'],'DENY')
        self.assertNotIn('Access-Control-Allow-Origin',headers)
        self.assertEqual(self.request('GET','/blockly/msg/ru.js',authenticated=False)[0],200)
        self.assertEqual(self.request('GET','/rules.json')[0],404)
        self.assertEqual(self.request('GET','/../state/rules.json')[0],404)
        self.assertEqual(self.request('GET','/%2e%2e/rules.json')[0],400)

    def test_state_requires_private_header_capability(self):
        self.assertEqual(self.request('GET','/api/state',authenticated=False)[0],403)
        code,_,body=self.request('GET','/api/state')
        self.assertEqual(code,200)
        self.assertEqual(json.loads(body)['config'],{'version':1,'enabled':False,'rules':[]})
        self.assertFalse((self.store.root/'rules.json').exists())

    def test_wrong_host_and_origin_and_missing_post_origin_refused(self):
        self.assertEqual(self.request('GET','/api/state',headers={'Host':'evil.example'})[0],403)
        self.assertEqual(self.request('GET','/api/state',headers={'Origin':'https://evil.example'})[0],403)
        self.assertEqual(self.request('POST','/api/stop',{},origin=False)[0],403)
        self.assertFalse((self.store.root/'command.json').exists())

    def test_no_query_token_or_cors_or_arbitrary_methods(self):
        self.assertEqual(self.request('GET','/api/state?token=fixture')[0],400)
        self.assertEqual(self.request('OPTIONS','/api/state')[0],403)
        self.assertEqual(self.request('PUT','/api/save',{})[0],405)

    def test_save_is_explicit_and_validates_unknown_fields(self):
        value={'version':1,'enabled':False,'rules':[]}
        code,_,body=self.request('POST','/api/save',{'config':value})
        self.assertEqual(code,200); self.assertEqual(json.loads(body)['config'],value)
        self.assertTrue((self.store.root/'rules.json').is_file())
        self.assertFalse((self.store.root/'command.json').exists())
        self.assertEqual(self.request('POST','/api/save',{'config':value,'shell':'anything'})[0],400)

    def test_preview_missing_native_static_error_and_stop_available(self):
        rule={'id':'a'*32,'name':'Fixture','enabled':False,'event':'manual','cooldown_seconds':10,'steps':[{'action':'turn','duration_ms':100}]}
        self.store.save({'version':1,'enabled':False,'rules':[rule]})
        code,_,body=self.request('POST','/api/preview',{'rule_id':'a'*32})
        self.assertEqual(code,503); self.assertEqual(json.loads(body),{'error':'native_unavailable'})
        code,_,body=self.request('POST','/api/stop',{})
        self.assertEqual(code,200); self.assertEqual(json.loads(body)['command']['action'],'stop')

    def test_oversized_and_non_json_body_refused(self):
        self.assertEqual(self.request('POST','/api/stop',{},headers={'Content-Type':'text/plain'})[0],400)
        self.assertEqual(self.request('POST','/api/save',{'config':'x'*42000})[0],400)

    def test_multiple_open_reuses_verified_single_instance_without_browser(self):
        self.server.write_session()
        with FileLock(self.store.root/'editor.lock',timeout=0), mock.patch('pet_tricks_server.PetTrickStore',return_value=self.store), mock.patch('pet_tricks_server.webbrowser.open') as browser:
            capture=io.StringIO()
            with redirect_stdout(capture): result=main(['--serve-only','--ready-stdout'])
            self.assertEqual(result,0); browser.assert_not_called()
            ready=json.loads(capture.getvalue())
            self.assertTrue(ready['reused']); self.assertEqual(ready['port'],self.server.server_address[1])

    def test_explicit_close_stops_server_and_clears_capability_file(self):
        self.server.write_session()
        self.assertEqual(self.request('POST','/api/close',{})[0],200)
        self.thread.join(timeout=4)
        self.assertFalse(self.thread.is_alive())
        self.assertFalse((self.store.root/'server-session.json').exists())

    def test_idle_timeout_closes_server(self):
        self.server.idle_seconds=.05
        self.server.last_activity=time.monotonic()-1
        self.thread.join(timeout=3)
        self.assertFalse(self.thread.is_alive())

    def test_manifest_hash_mismatch_refused_before_bind(self):
        (self.root/'assets'/'blockly'/'msg'/'ru.js').write_text('changed',encoding='utf-8')
        with self.assertRaises(ValueError): EditorServer(self.store,self.root/'assets')


if __name__=='__main__': unittest.main()
