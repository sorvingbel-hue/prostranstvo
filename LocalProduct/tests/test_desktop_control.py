"""Synthetic tests only. No WindowsBackend instance or real desktop API call."""
from dataclasses import replace
import ctypes
import io
import json
import os
import struct
import threading
import tempfile
from pathlib import Path
import unittest
import zlib

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from desktop_control import (Controller, ControlError, INPUT, State, Window,
                             MAX_TEXT_CHARS, PROTECTED_SCREEN_PROGRAMS,
                             WindowsBackend, chord, require_desktop)
from desktop_control_mcp import (MAX_LINE_BYTES, TOOLS, LazyWindowsBackend,
                                 StopFileGate, dispatch, serve, watch_stop_file)


def png(width=10, height=8):
    def chunk(kind, data):
        return struct.pack('>I',len(data))+kind+data+struct.pack('>I',zlib.crc32(kind+data)&0xffffffff)
    return (b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('>IIBBBBB',width,height,8,2,0,0,0))+
            chunk(b'IDAT',zlib.compress((b'\0'+b'\0'*(width*3))*height))+chunk(b'IEND',b''))


class Clock:
    def __init__(self): self.now=100.0
    def __call__(self): return self.now


class FakeBackend:
    def __init__(self):
        self.window=Window(5,7,12345,r'C:\Apps\browser.exe','Browser','A private title')
        self.state=State(1,'WinSta0','Default','Default',(-5,-4,10,8),self.window,(self.window,))
        self.calls=[]
        self.captures=0
        self.inspections=0
        self.image=png()
        self.failure=None
        self.on_inspect=None
        self.on_capture=None
    def inspect(self):
        self.inspections+=1
        if self.on_inspect: self.on_inspect()
        return self.state
    def screenshot(self):
        self.captures+=1
        if self.on_capture: self.on_capture()
        return self.image
    def invoke(self,operation,values,*,expected,valid_until,clock,is_stopped):
        if self.failure: raise self.failure
        if is_stopped() or clock()>=valid_until: raise ControlError('late_backend_gate')
        self.calls.append((operation,values,expected))


class ControlTests(unittest.TestCase):
    def setUp(self):
        self.backend=FakeBackend(); self.clock=Clock(); self.audit=[]
        self.ctrl=Controller(self.backend,enabled=True,clock=self.clock,audit=self.audit.append,
                             nonce=lambda _: 'unique-observation')
    def observe(self): return self.ctrl.observe()['observation_id']
    def click(self,**values):
        args={'observation_id':self.observe(),'x':-1,'y':-1,'button':'left'}
        args.update(values)
        return self.ctrl.act('desktop_click',args)
    def fails(self,code,fn):
        with self.assertRaises(ControlError) as caught: fn()
        self.assertEqual(caught.exception.code,code)
        self.assertFalse(self.backend.calls)
    def test_default_is_readonly(self):
        self.ctrl=Controller(self.backend,clock=self.clock)
        self.assertFalse(self.ctrl.status()['input_enabled'])
        self.fails('input_disabled_stopped_or_expired',lambda:self.click())
    def test_status_has_no_os_inspection(self):
        self.ctrl.status(); self.assertEqual(self.backend.inspections,0)
    def test_observe_returns_png_and_public_metadata(self):
        result=self.ctrl.observe()
        self.assertEqual(result['image']['type'],'image')
        self.assertEqual(result['image']['mimeType'],'image/png')
        self.assertEqual(result['foreground']['program'],'browser.exe')
        self.assertNotIn('executable',result['foreground'])
        self.assertFalse(self.backend.calls)
    def test_success_is_request_not_delivery(self):
        result=self.click()
        self.assertEqual(len(self.backend.calls),1)
        self.assertFalse(result['site_action_or_delivery_verified'])
    def test_single_use(self):
        ident=self.observe()
        self.ctrl.act('desktop_press',{'observation_id':ident,'chord':'TAB'})
        with self.assertRaisesRegex(ControlError,'fresh_observation_required'):
            self.ctrl.act('desktop_press',{'observation_id':ident,'chord':'TAB'})
        self.assertEqual(len(self.backend.calls),1)
    def test_failed_input_consumes_observation(self):
        ident=self.observe()
        self.fails('invalid_coordinate',lambda:self.ctrl.act('desktop_click',{'observation_id':ident,'x':True,'y':0,'button':'left'}))
        self.fails('fresh_observation_required',lambda:self.ctrl.act('desktop_press',{'observation_id':ident,'chord':'TAB'}))
    def test_negative_virtual_origin_valid(self): self.click(x=-5,y=-4)
    def test_outside_points_and_bool(self):
        for x,y in ((5,0),(-6,0),(0,4),(0,-5),(True,0),(0,1.5)):
            with self.subTest(x=x,y=y):
                code='invalid_coordinate' if type(x)is not int or type(y)is not int else 'coordinate_outside_observation'
                self.fails(code,lambda:self.click(x=x,y=y))
    def test_stale_observation(self):
        ident=self.observe(); self.clock.now+=20
        self.fails('observation_expired',lambda:self.ctrl.act('desktop_press',{'observation_id':ident,'chord':'TAB'}))
    def test_clock_rollback(self):
        ident=self.observe(); self.clock.now-=1
        self.fails('observation_expired',lambda:self.ctrl.act('desktop_press',{'observation_id':ident,'chord':'TAB'}))
    def test_focus_process_and_screen_changes(self):
        for mutation in (lambda s:replace(s,foreground=replace(s.foreground,pid=8)),
                         lambda s:replace(s,foreground=replace(s.foreground,started=999)),
                         lambda s:replace(s,bounds=(-5,-4,9,8))):
            with self.subTest(mutation=mutation):
                self.setUp(); ident=self.observe(); self.backend.state=mutation(self.backend.state)
                self.fails('desktop_or_focus_changed',lambda:self.ctrl.act('desktop_press',{'observation_id':ident,'chord':'TAB'}))
    def test_locked_wrong_disconnected_desktops_never_capture(self):
        for update,code in (({'session_id':0},'interactive_session_required'),
                            ({'connected':False},'connected_session_required'),
                            ({'station':'Service-0'},'unlocked_default_desktop_required'),
                            ({'input_desktop':'Winlogon'},'unlocked_default_desktop_required'),
                            ({'thread_desktop':'Other'},'unlocked_default_desktop_required')):
            with self.subTest(update=update):
                self.setUp(); self.backend.state=replace(self.backend.state,**update)
                self.fails(code,self.ctrl.observe); self.assertEqual(self.backend.captures,0)
    def test_unknown_foreground_rejected(self):
        self.backend.state=replace(self.backend.state,foreground=None)
        self.fails('foreground_identity_unavailable',self.ctrl.observe)
    def test_oversized_bounds_rejected_before_capture(self):
        self.backend.state=replace(self.backend.state,bounds=(0,0,32768,32768))
        self.fails('unsupported_screen_bounds',self.ctrl.observe)
        self.assertEqual(self.backend.captures,0)
    def test_protected_auth_windows_never_capture(self):
        for program in PROTECTED_SCREEN_PROGRAMS:
            with self.subTest(program=program):
                self.setUp(); self.backend.state=replace(self.backend.state,windows=(replace(self.backend.window,executable=program),))
                self.fails('protected_authentication_window_present',self.ctrl.observe)
                self.assertEqual(self.backend.captures,0)
    def test_protected_auth_beyond_public_window_cap(self):
        self.backend.state=replace(self.backend.state,protected_auth_present=True)
        self.fails('protected_authentication_window_present',self.ctrl.observe)
    def test_shell_foreground_input_blocked(self):
        self.backend.state=replace(self.backend.state,foreground=replace(self.backend.window,executable='powershell.exe'))
        self.fails('protected_foreground',lambda:self.click())
    def test_codex_foreground_allows_restore_browser_but_not_input(self):
        codex=replace(self.backend.window,hwnd=6,pid=9,executable='Codex.exe')
        self.backend.state=replace(self.backend.state,foreground=codex,windows=(codex,self.backend.window))
        ident=self.observe()
        self.ctrl.act('desktop_window',{'observation_id':ident,'window_id':self.backend.window.window_id,'action':'restore'})
        self.assertEqual(len(self.backend.calls),1)
        self.backend.calls.clear()
        for operation,extra in (('desktop_click',{'x':0,'y':0,'button':'left'}),('desktop_type',{'text':'hello'})):
            ident=self.observe()
            self.fails('protected_foreground',lambda:self.ctrl.act(operation,{'observation_id':ident,**extra}))
    def test_move_and_double_click(self):
        ident=self.observe(); self.ctrl.act('desktop_move',{'observation_id':ident,'x':-2,'y':-2})
        self.click(count=2)
        self.assertEqual(len(self.backend.calls),2)
        self.assertEqual(self.backend.calls[1][1]['count'],2)
    def test_click_count_bounded(self):
        for count in (0,3,True,1.5):
            self.fails('invalid_click_count',lambda:self.click(count=count))
    def test_drag_validated_and_forwarded(self):
        obs=self.observe()
        self.ctrl.act('desktop_drag',{'observation_id':obs,'x1':-4,'y1':-3,'x2':3,'y2':2,
                                     'button':'left','duration_ms':200,'steps':5})
        self.assertEqual(self.backend.calls[0][0],'desktop_drag')
    def test_drag_arguments_are_bounded(self):
        for key,value,code in (('x2',5,'coordinate_outside_observation'),
                              ('x1',True,'invalid_coordinate'),
                              ('button','middle','invalid_drag_button'),
                              ('duration_ms',99,'invalid_drag_duration'),
                              ('duration_ms',2001,'invalid_drag_duration'),
                              ('duration_ms',True,'invalid_drag_duration'),
                              ('steps',1,'invalid_drag_steps'),
                              ('steps',41,'invalid_drag_steps'),
                              ('steps',2.5,'invalid_drag_steps')):
            with self.subTest(key=key,value=value):
                self.setUp(); obs=self.observe()
                args={'observation_id':obs,'x1':-4,'y1':-3,'x2':3,'y2':2,'button':'left','duration_ms':200,'steps':5}
                args[key]=value
                self.fails(code,lambda:self.ctrl.act('desktop_drag',args))
    def test_own_process_is_not_input_target(self):
        self.backend.state=replace(self.backend.state,foreground=replace(self.backend.window,pid=os.getpid()))
        self.fails('protected_foreground',lambda:self.click())
    def test_window_identity_and_reused_handle(self):
        ident=self.observe()
        self.backend.state=replace(self.backend.state,windows=(replace(self.backend.window,started=6789),))
        self.fails('window_identity_changed',lambda:self.ctrl.act('desktop_window',{'observation_id':ident,'window_id':self.backend.window.window_id,'action':'minimize'}))
    def test_window_protected_target(self):
        target=replace(self.backend.window,hwnd=6,pid=9,executable='cmd.exe')
        self.backend.state=replace(self.backend.state,windows=(self.backend.window,target))
        ident=self.observe()
        self.fails('protected_target',lambda:self.ctrl.act('desktop_window',{'observation_id':ident,'window_id':target.window_id,'action':'minimize'}))
    def test_valid_window_request(self):
        ident=self.observe()
        self.ctrl.act('desktop_window',{'observation_id':ident,'window_id':self.backend.window.window_id,'action':'minimize'})
        self.assertEqual(self.backend.calls[0][1]['window'],self.backend.window)
    def test_window_close_and_unknown_id_refused(self):
        for action,ident,code in (('close',self.backend.window.window_id,'unsupported_window_action'),('activate','unknown','window_identity_changed')):
            obs=self.observe()
            self.fails(code,lambda:self.ctrl.act('desktop_window',{'observation_id':obs,'window_id':ident,'action':action}))
    def test_text_limits_controls_and_surrogates(self):
        for text in ('','x'*(MAX_TEXT_CHARS+1),'hello\x00','\ud800',123):
            obs=self.observe()
            self.fails('invalid_text',lambda:self.ctrl.act('desktop_type',{'observation_id':obs,'text':text}))
    def test_unicode_text_and_newline(self):
        obs=self.observe(); self.ctrl.act('desktop_type',{'observation_id':obs,'text':'Привет 🙂\nHello\tworld'})
        self.assertEqual(len(self.backend.calls),1)
    def test_chord_support_and_security(self):
        self.assertEqual(chord('CTRL+SHIFT+A'),([17,16],65))
        for bad in ('WIN+R','CTRL+CTRL+A','ALT+FOO','CTRL','CTRL+ALT+DELETE','A+B','',123):
            with self.subTest(bad=bad):
                with self.assertRaises(ControlError): chord(bad)
    def test_scroll_bounded(self):
        for amount in (0,21,-21,True,1.5):
            obs=self.observe()
            self.fails('invalid_scroll_amount',lambda:self.ctrl.act('desktop_scroll',{'observation_id':obs,'x':0,'y':0,'notches':amount}))
    def test_exact_argument_keys(self):
        self.fails('invalid_arguments',lambda:self.ctrl.act('desktop_press',{'observation_id':'x','chord':'TAB','shell':'bad'}))
        self.fails('invalid_arguments',lambda:self.ctrl.act('unknown',{}))
    def test_invalid_observation_ids(self):
        for ident in ('other','я',None,'x'*129):
            self.observe()
            self.fails('fresh_observation_required',lambda:self.ctrl.act('desktop_press',{'observation_id':ident,'chord':'TAB'}))
    def test_stop_revokes_input_and_capture(self):
        ident=self.observe(); self.ctrl.stop()
        self.assertFalse(self.ctrl.status()['input_enabled'])
        self.fails('input_disabled_stopped_or_expired',lambda:self.ctrl.act('desktop_press',{'observation_id':ident,'chord':'TAB'}))
        self.fails('session_stopped_or_expired',self.ctrl.observe)
    def test_stop_does_not_wait_for_operation_lock(self):
        done=threading.Event()
        with self.ctrl.lock:
            thread=threading.Thread(target=lambda:(self.ctrl.stop(),done.set()))
            thread.start(); self.assertTrue(done.wait(1))
        thread.join(); self.assertTrue(self.ctrl.revoked.is_set())
    def test_stop_during_capture_discards_image(self):
        self.backend.on_capture=self.ctrl.stop
        self.fails('session_stopped_or_expired',self.ctrl.observe)
    def test_session_expiry(self):
        self.clock.now=self.ctrl.deadline
        self.fails('session_stopped_or_expired',self.ctrl.observe)
    def test_expiry_during_capture(self):
        self.backend.on_capture=lambda:setattr(self.clock,'now',self.ctrl.deadline)
        self.fails('session_stopped_or_expired',self.ctrl.observe)
    def test_expiry_during_authorization_inspection(self):
        ident=self.observe()
        self.backend.on_inspect=lambda:setattr(self.clock,'now',self.ctrl.deadline)
        self.fails('observation_or_session_expired',lambda:self.ctrl.act('desktop_press',{'observation_id':ident,'chord':'TAB'}))
    def test_changed_during_capture(self):
        self.backend.on_capture=lambda:setattr(self.backend,'state',replace(self.backend.state,foreground=replace(self.backend.window,pid=99)))
        self.fails('desktop_changed_during_observation',self.ctrl.observe)
    def test_invalid_png_and_bounds_mismatch(self):
        for image,code in ((b'not-png','invalid_or_oversized_screenshot'),(png(9,8),'screenshot_bounds_mismatch')):
            self.backend.image=image; self.fails(code,self.ctrl.observe)
    def test_failed_observation_clears_old_token(self):
        ident=self.observe(); self.backend.image=b'bad'
        self.fails('invalid_or_oversized_screenshot',self.ctrl.observe)
        self.fails('fresh_observation_required',lambda:self.ctrl.act('desktop_press',{'observation_id':ident,'chord':'TAB'}))
    def test_native_exception_details_never_returned(self):
        self.backend.failure=RuntimeError('secret text and password')
        self.fails('native_operation_failed_outcome_unknown',lambda:self.click())
        self.assertNotIn('password',json.dumps(self.audit))
    def test_partial_input_revokes_session(self):
        self.backend.failure=ControlError('partial_input_outcome_unknown_do_not_retry')
        self.fails('partial_input_outcome_unknown_do_not_retry',lambda:self.click())
        self.assertTrue(self.ctrl.stopped)
    def test_audit_has_no_typed_text_title_or_screen(self):
        obs=self.observe(); self.ctrl.act('desktop_type',{'observation_id':obs,'text':'secret-payload'})
        for entry in self.audit: self.assertEqual(set(entry),{'operation','outcome','at_unix'})
        self.assertNotIn('secret-payload',json.dumps(self.audit))
        self.assertNotIn('private title',json.dumps(self.audit))
    def test_failed_audit_sink_does_not_change_action_outcome(self):
        def fail(_): raise RuntimeError('sink')
        self.ctrl.audit=fail; self.click(); self.assertEqual(len(self.backend.calls),1)
    def test_invalid_session_options(self):
        for opts in ({'session_seconds':0},{'session_seconds':3601},{'session_seconds':True},{'enabled':'yes'}):
            with self.assertRaises(ValueError): Controller(self.backend,**opts)
    @unittest.skipUnless(os.name=='nt','ctypes Windows ABI sizes differ on non-Windows')
    def test_input_abi_without_loading_native_dlls(self):
        self.assertEqual(ctypes.sizeof(INPUT),40 if ctypes.sizeof(ctypes.c_void_p)==8 else 28)


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        self.backend=FakeBackend(); self.controller=Controller(self.backend,clock=Clock())
    def request(self,name,args=None,meta=None):
        params={'name':name,'arguments':args or {}}
        if meta is not None: params['_meta']=meta
        return dispatch({'jsonrpc':'2.0','id':1,'method':'tools/call','params':params},self.controller)
    def wire(self,data):
        stream=io.BytesIO(); serve(self.controller,io.BytesIO(data),stream)
        return [json.loads(line) for line in stream.getvalue().splitlines()]
    def test_initialize_and_tools_do_not_inspect(self):
        for method in ('initialize','ping','tools/list'):
            result=dispatch({'jsonrpc':'2.0','id':1,'method':method},self.controller)
            self.assertIn('result',result)
        self.assertEqual(self.backend.inspections,0)
    def test_standard_meta_accepted(self):
        result=self.request('desktop_status',meta={'progressToken':'abc'})
        self.assertFalse(result['result']['isError'])
    def test_image_is_separate_content(self):
        result=self.request('desktop_observe')['result']
        self.assertEqual([item['type'] for item in result['content']],['text','image'])
        self.assertNotIn('data',json.loads(result['content'][0]['text']))
    def test_notifications_do_not_act_or_capture(self):
        result=dispatch({'jsonrpc':'2.0','method':'tools/call','params':{'name':'desktop_observe'}},self.controller)
        self.assertIsNone(result); self.assertEqual(self.backend.captures,0)
    def test_schema_additional_properties_closed(self):
        for tool in TOOLS:
            self.assertFalse(tool['inputSchema']['additionalProperties'])
        self.assertEqual(len(TOOLS),10)
    def test_unknown_method_tool_and_args(self):
        self.assertEqual(dispatch({'jsonrpc':'2.0','id':1,'method':'exec'},self.controller)['error']['code'],-32601)
        for name,args in (('shell',{}),('desktop_status',{'x':1})):
            self.assertTrue(self.request(name,args)['result']['isError'])
    def test_bad_and_duplicate_json_do_not_inspect(self):
        for data in (b'not json\n',b'{"jsonrpc":"2.0","id":1,"id":2}\n',b'\xff\n',b'{"x":NaN}\n',b'['*30+b'0'+b']'*30+b'\n'):
            with self.subTest(data=data): self.assertEqual(self.wire(data)[0]['error']['code'],-32700)
        self.assertEqual(self.backend.inspections,0)
    def test_oversized_frame_closes_before_next_request(self):
        data=b' '*MAX_LINE_BYTES+b'x\n'+b'{"jsonrpc":"2.0","id":1,"method":"ping"}\n'
        result=self.wire(data); self.assertEqual(len(result),1)
        self.assertEqual(result[0]['error']['code'],-32700)
    def test_json_rpc_ids_are_bounded(self):
        for ident in (None,True,{},'x'*129,2**60):
            result=dispatch({'jsonrpc':'2.0','id':ident,'method':'ping'},self.controller)
            self.assertEqual(result['error']['code'],-32600)
    def test_framing_multiple_requests(self):
        data=b'{"jsonrpc":"2.0","id":1,"method":"ping"}\n{"jsonrpc":"2.0","id":2,"method":"tools/list"}\n'
        self.assertEqual([value['id'] for value in self.wire(data)],[1,2])
    def test_status_of_lazy_backend_does_not_create_native_backend(self):
        lazy=LazyWindowsBackend(); controller=Controller(lazy)
        self.assertFalse(controller.status()['input_enabled']); self.assertIsNone(lazy.value)
    def test_indicator_handle_propagates_to_cached_backend(self):
        from types import SimpleNamespace
        lazy=LazyWindowsBackend(); lazy.value=SimpleNamespace(indicator_hwnd=None)
        lazy.set_indicator(55)
        self.assertEqual(lazy.indicator_hwnd,55)
        self.assertEqual(lazy.value.indicator_hwnd,55)
    def test_no_shell_launch_or_credential_tools(self):
        self.assertEqual({tool['name'] for tool in TOOLS},{'desktop_status','desktop_observe','desktop_stop','desktop_click','desktop_move','desktop_drag','desktop_type','desktop_press','desktop_scroll','desktop_window'})


class StopMarkerTests(unittest.TestCase):
    def test_missing_marker_then_revoke_and_removal_never_resumes(self):
        with tempfile.TemporaryDirectory() as folder:
            marker=Path(folder)/'STOP'; gate=StopFileGate(marker)
            controller=Controller(FakeBackend(),enabled=True)
            self.assertFalse(gate.check(controller))
            marker.write_text('stop',encoding='utf8')
            self.assertTrue(gate.check(controller)); self.assertTrue(controller.stopped)
            marker.unlink()
            self.assertFalse(gate.check(controller)); self.assertFalse(controller.status()['input_enabled'])
    def test_relative_and_network_paths_fail_closed(self):
        for value in ('relative/STOP',r'\\server\share\STOP'):
            self.assertTrue(StopFileGate(value).blocked())
    def test_parent_file_fails_closed(self):
        with tempfile.TemporaryDirectory() as folder:
            parent=Path(folder)/'not-directory'; parent.write_text('x')
            self.assertTrue(StopFileGate(parent/'STOP').blocked())
    def test_read_error_fails_closed(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as folder:
            gate=StopFileGate(Path(folder)/'STOP')
            with patch.object(Path,'lstat',side_effect=PermissionError('private-path')):
                self.assertTrue(gate.blocked())
    def test_reparse_attributes_fail_closed(self):
        from unittest.mock import patch
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as folder:
            gate=StopFileGate(Path(folder)/'STOP')
            with patch.object(Path,'lstat',return_value=SimpleNamespace(st_mode=0o040755,st_file_attributes=0x400)):
                self.assertTrue(gate.blocked())
    def test_watcher_revokes_without_ui_or_native_backend(self):
        with tempfile.TemporaryDirectory() as folder:
            marker=Path(folder)/'STOP'; gate=StopFileGate(marker)
            controller=Controller(FakeBackend(),enabled=True); finished=threading.Event()
            thread=threading.Thread(target=watch_stop_file,args=(gate,controller,finished))
            thread.start()
            try:
                marker.write_text('stop',encoding='utf8')
                self.assertTrue(controller.revoked.wait(1))
                self.assertFalse(controller.status()['input_enabled'])
            finally:
                finished.set(); thread.join(1)


class NativeAdapterFakeApiTests(unittest.TestCase):
    """Exercise the adapter with fake user32; never loads Windows DLLs."""
    def make_adapter(self, accepted):
        from types import SimpleNamespace
        native=WindowsBackend.__new__(WindowsBackend)
        state=FakeBackend().state; native.inspect=lambda **_:state
        calls=[]
        def send(count, items, size):
            calls.append([(item.type,item.ki.wVk,item.ki.dwFlags,item.mi.dwFlags) for item in items])
            return accepted if len(calls)==1 else count
        native.u=SimpleNamespace(SendInput=send,GetAsyncKeyState=lambda _:0)
        return native,state,calls
    def test_partial_keyboard_send_releases_only_synthetic_key(self):
        native,state,calls=self.make_adapter(1)
        events=[native._key(vk=17),native._key(vk=65),native._key(vk=65,flags=2),native._key(vk=17,flags=2)]
        with self.assertRaisesRegex(ControlError,'partial_input_outcome_unknown_do_not_retry'):
            native._send(events,state,1000,lambda:100,lambda:False)
        self.assertEqual(len(calls),2)
        self.assertEqual([(item[1],item[2]) for item in calls[1]],[(17,2)])
    def test_partial_mouse_send_releases_button_without_repeating_click(self):
        native,state,calls=self.make_adapter(2)
        events=[native._mouse(flags=1),native._mouse(flags=2),native._mouse(flags=4)]
        with self.assertRaises(ControlError):
            native._send(events,state,1000,lambda:100,lambda:False)
        self.assertEqual(len(calls),2)
        self.assertEqual([item[3] for item in calls[1]],[4])
    def test_zero_accepted_has_no_cleanup_input(self):
        native,state,calls=self.make_adapter(0)
        with self.assertRaises(ControlError):
            native._send([native._key(vk=65)],state,1000,lambda:100,lambda:False)
        self.assertEqual(len(calls),1)
    def test_final_native_guard_checks_expiry_and_revocation(self):
        for clock,stopped in ((lambda:1000,lambda:False),(lambda:100,lambda:True)):
            native,state,calls=self.make_adapter(1)
            with self.assertRaises(ControlError):
                native._send([native._key(vk=65)],state,1000,clock,stopped)
            self.assertFalse(calls)
    def test_pressed_mouse_button_blocks_native_move_or_click(self):
        native,state,calls=self.make_adapter(1)
        native.u.GetAsyncKeyState=lambda key:0x8000 if key==1 else 0
        with self.assertRaisesRegex(ControlError,'physical_mouse_button_pressed'):
            native._send([native._mouse(flags=1)],state,1000,lambda:100,lambda:False)
        self.assertFalse(calls)
    def test_own_process_identity_never_reads_own_window_text(self):
        from types import SimpleNamespace
        from ctypes import wintypes as W
        native=WindowsBackend.__new__(WindowsBackend)
        def pid(_hwnd,out): ctypes.cast(out,ctypes.POINTER(W.DWORD)).contents.value=os.getpid(); return 1
        def path(_handle,_flags,out,_size): out.value='python.exe'; return True
        def times(_handle,created,*unused):
            ctypes.cast(created,ctypes.POINTER(W.FILETIME)).contents.dwLowDateTime=1
            return True
        def klass(_hwnd,out,_size): out.value='TkTopLevel'; return 10
        def forbidden(*unused): self.fail('own-process WM_GETTEXT must never be called')
        native.u=SimpleNamespace(IsWindowVisible=lambda _:True,GetWindowThreadProcessId=pid,
                                 GetClassNameW=klass,GetWindowTextW=forbidden)
        native.k=SimpleNamespace(OpenProcess=lambda *a:1,CloseHandle=lambda _:True,
                                 QueryFullProcessImageNameW=path,GetProcessTimes=times)
        native._session=lambda _:1
        result=native._window(123,1)
        self.assertEqual(result.pid,os.getpid()); self.assertEqual(result.title,'')


class NativeDragFakeApiTests(unittest.TestCase):
    """Full drag state machine using fake SendInput/hit testing and fake time."""
    def setUp(self):
        from types import SimpleNamespace
        self.clock=Clock(); self.stopped=threading.Event(); self.calls=[]; self.held=False
        self.current=FakeBackend().state; self.fail_call=None; self.on_sleep=None
        self.hit=lambda x,y:5
        self.native=WindowsBackend.__new__(WindowsBackend)
        self.native.inspect=lambda **_:self.current
        self.native.sleep=self.sleep
        self.native.u=SimpleNamespace(SendInput=self.send,
            GetAsyncKeyState=lambda key:0x8000 if self.held and key==1 else 0,
            WindowFromPoint=lambda point:self.hit(point.x,point.y),GetAncestor=lambda hwnd,_:hwnd,
            GetCursorPos=self.cursor)
        self.values={'x1':-3,'y1':-2,'x2':1,'y2':2,'button':'left','duration_ms':100,'steps':2}
    def sleep(self,seconds):
        self.clock.now+=seconds
        if self.on_sleep: self.on_sleep()
    def cursor(self,out):
        from ctypes import wintypes as W
        point=ctypes.cast(out,ctypes.POINTER(W.POINT)).contents
        point.x=-1; point.y=0
        return True
    def send(self,count,items,_size):
        flags=[item.mi.dwFlags for item in items]
        self.calls.append(flags)
        if len(self.calls)==self.fail_call: return 0
        if any(flag&2 for flag in flags): self.held=True
        if any(flag&4 for flag in flags): self.held=False
        return count
    def drag(self):
        self.native.invoke('desktop_drag',self.values,expected=self.current,
                           valid_until=120,clock=self.clock,is_stopped=self.stopped.is_set)
    def test_success_has_one_down_stages_and_one_up(self):
        self.drag()
        self.assertEqual(self.calls,[[0xC001],[2],[0xC001],[0xC001],[4]])
        self.assertFalse(self.held)
        self.assertAlmostEqual(self.clock.now,100.1)
    def test_foreign_point_rejected_before_move_and_release_attempted(self):
        self.hit=lambda x,y:9 if x>=0 else 5
        with self.assertRaisesRegex(ControlError,'drag_point_not_in_observed_foreground'): self.drag()
        self.assertEqual(self.calls,[[0xC001],[2],[0xC001],[4]])
        self.assertFalse(self.held)
    def test_stop_during_wait_releases_without_later_drag_stage(self):
        self.on_sleep=self.stopped.set
        with self.assertRaisesRegex(ControlError,'drag_stopped_or_expired'): self.drag()
        self.assertEqual(self.calls,[[0xC001],[2],[4]])
        self.assertFalse(self.held)
    def test_expiry_during_wait_attempts_release(self):
        self.on_sleep=lambda:setattr(self.clock,'now',120)
        with self.assertRaises(ControlError): self.drag()
        self.assertEqual(self.calls[-1],[4]); self.assertFalse(self.held)
    def test_partial_move_never_retries_and_releases(self):
        self.fail_call=3
        with self.assertRaisesRegex(ControlError,'partial_drag_input_outcome_unknown_do_not_retry'): self.drag()
        self.assertEqual(self.calls,[[0xC001],[2],[0xC001],[4]])
        self.assertFalse(self.held)
    def test_partial_down_still_attempts_release(self):
        self.fail_call=2
        with self.assertRaises(ControlError): self.drag()
        self.assertEqual(self.calls,[[0xC001],[2],[4]])
    def test_focus_change_prevents_input_into_foreign_window_reports_cleanup_unknown(self):
        self.on_sleep=lambda:setattr(self,'current',replace(self.current,foreground=replace(self.current.foreground,pid=8)))
        with self.assertRaisesRegex(ControlError,'drag_release_failed_outcome_unknown'): self.drag()
        self.assertEqual(self.calls,[[0xC001],[2]])
        self.assertTrue(self.held)  # A real user may need to release the button.
    def test_failed_release_is_explicit_unknown_outcome(self):
        self.fail_call=5
        with self.assertRaisesRegex(ControlError,'drag_release_failed_outcome_unknown'): self.drag()
        self.assertEqual(self.calls[-1],[4])
    def test_backend_exception_after_down_still_attempts_release(self):
        def fail(): raise RuntimeError('opaque native failure')
        self.on_sleep=fail
        with self.assertRaises(RuntimeError): self.drag()
        self.assertEqual(self.calls[-1],[4]); self.assertFalse(self.held)
    def test_initial_foreign_point_has_no_input_or_cleanup(self):
        self.hit=lambda x,y:9
        with self.assertRaises(ControlError): self.drag()
        self.assertFalse(self.calls)
    def test_secure_desktop_change_suppresses_release(self):
        self.on_sleep=lambda:setattr(self,'current',replace(self.current,input_desktop='Winlogon'))
        with self.assertRaisesRegex(ControlError,'drag_release_failed_outcome_unknown'): self.drag()
        self.assertEqual(self.calls,[[0xC001],[2]])
    def test_nonactivating_foreign_overlay_suppresses_drop(self):
        self.native.u.GetCursorPos=lambda _:False
        with self.assertRaisesRegex(ControlError,'drag_release_failed_outcome_unknown'): self.drag()
        self.assertTrue(self.held)
    def test_slow_inspection_cannot_extend_move_deadline(self):
        def inspect(**_):
            if self.held: self.clock.now+=0.4
            return self.current
        self.native.inspect=inspect
        with self.assertRaisesRegex(ControlError,'observation_or_session_expired'): self.drag()
        self.assertEqual(self.calls,[[0xC001],[2],[4]])
    def test_wait_crossed_deadline_never_sleeps_negative(self):
        values=iter((100.0,100.1,100.2)); sleeps=[]
        self.native.sleep=lambda delay:sleeps.append(delay)
        self.native._drag_wait(100.05,120,lambda:next(values),lambda:False)
        self.assertEqual(sleeps,[0.02])
    def test_hidden_indicator_blocks_new_input_but_not_cleanup(self):
        self.held=True
        def inspect(*,cleanup=False):
            if not cleanup: raise ControlError('visible_indicator_required')
            return self.current
        self.native.inspect=inspect
        with self.assertRaisesRegex(ControlError,'visible_indicator_required'):
            self.native._same(self.current)
        self.assertTrue(self.native._drag_release(4,self.current))
        self.assertEqual(self.calls,[[4]])
        self.assertFalse(self.held)


if __name__=='__main__': unittest.main()
