import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
"""Optional API gates on synthetic credentials and fake transports only.

The fake protector tests protected-storage plumbing, not actual DPAPI security
or provider privacy readiness. No real API key or network connection is used.
"""
import base64
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock
import urllib.error

from cat_api import (ApiError, ApiGateway, ApiSettings, ENDPOINT, MAX_CONFIG,
                     MAX_RESPONSE, POLICY, ResponsesTransport, _NoRedirect, make_input)


SYNTHETIC_KEY = "synthetic-unit-key-" + "X" * 32
HERE = Path(__file__).resolve().parents[1]
ASSET_ROOT = HERE if (HERE / "assets").is_dir() else HERE.parent


class FakeProtector:
    def __init__(self):
        self.protected = []
        self.unprotected = []

    def protect(self, data):
        self.protected.append(data)
        return b"unit-test-envelope:" + bytes(value ^ 0xA7 for value in data)

    def unprotect(self, data):
        self.unprotected.append(data)
        if not data.startswith(b"unit-test-envelope:"):
            raise RuntimeError("synthetic-private-protector-detail")
        return bytes(value ^ 0xA7 for value in data[len(b"unit-test-envelope:"):])


class FakeTransport:
    def __init__(self, answer="Это синтетический ответ для проверки.", error=None):
        self.answer = answer
        self.error = error
        self.calls = []
        self.lock = threading.Lock()

    def send(self, key, payload):
        with self.lock:
            self.calls.append((key,payload))
        if self.error is not None:
            raise self.error
        return self.answer


class FakeResponse:
    def __init__(self, body, code=200):
        self.body = body
        self.code = code
        self.read_limits = []

    def __enter__(self):return self
    def __exit__(self,*args):return None
    def getcode(self):return self.code
    def read(self, limit):
        self.read_limits.append(limit)
        return self.body[:limit]


class FakeOpener:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = []

    def open(self, request, timeout):
        self.calls.append((request,timeout))
        if self.error is not None:raise self.error
        return self.response


def provider_body(text="Это проверочный ответ.", *, role="assistant"):
    return json.dumps({"status":"completed", "output":[{
        "type":"message","role":role,"content":[{"type":"output_text","text":text}]}]},ensure_ascii=True).encode("utf-8")


class ApiFixture(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="cat-api-synthetic-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name) / "settings"
        self.protector = FakeProtector()
        self.shared_lock = threading.RLock()
        self.clock_value = datetime(2026,11,7,12,0,tzinfo=timezone.utc)
        self.settings = self.reopen()
        # Any accidental attempt to construct a real network opener fails.
        network_guard = mock.patch("urllib.request.build_opener",side_effect=AssertionError("Real network forbidden in tests"))
        network_guard.start()
        self.addCleanup(network_guard.stop)

    def reopen(self):
        return ApiSettings(self.root,protector=self.protector,
                           clock=lambda:self.clock_value,lock_factory=lambda root:self.shared_lock)

    def configure(self, settings=None, **changes):
        settings = self.settings if settings is None else settings
        values = dict(enabled=True,model="unit-test-model",daily_request_limit=5,
                      max_output_tokens=400,parental_consent=True,zdr_attested=True,
                      approval_reference="Synthetic operator declaration; no real provider verification")
        values.update(changes)
        return settings.configure(**values)

    def ready(self, **changes):
        self.settings.set_api_key(SYNTHETIC_KEY)
        self.configure(**changes)
        return self.settings

    def gateway(self, transport=None, settings=None):
        return ApiGateway(self.settings if settings is None else settings,
                          transport=FakeTransport() if transport is None else transport)

    def ask(self, gateway, request_id="test-request", text="Почему видна Луна?",history=None,context=None):
        return gateway.respond(text,[] if history is None else history,
                               [] if context is None else context,request_id)


class SettingsTests(ApiFixture):
    def test_select_mode_preserves_connection_settings_and_reopens(self):
        self.ready()
        before = self.settings.snapshot()
        offline = self.settings.set_enabled(False)
        self.assertFalse(offline['ready'])
        self.assertFalse(self.reopen().snapshot()['enabled'])
        self.assertEqual(self.reopen().set_enabled(True), before)

    def test_construction_and_disabled_snapshot_are_inert(self):
        self.assertFalse(self.root.exists())
        status = self.settings.snapshot()
        self.assertEqual(status["state"],"disabled")
        self.assertEqual(status["mode"],"offline")
        self.assertFalse(status["ready"])
        self.assertFalse(self.root.exists())
        self.assertEqual(self.protector.protected,[])

    def test_protected_storage_and_redacted_snapshot(self):
        status = self.settings.set_api_key(SYNTHETIC_KEY)
        saved = json.loads(self.settings.path.read_text(encoding="utf-8"))
        self.assertEqual(self.protector.protected,[SYNTHETIC_KEY.encode("ascii")])
        self.assertNotIn(SYNTHETIC_KEY,self.settings.path.read_text(encoding="utf-8"))
        self.assertTrue(base64.b64decode(saved["key_blob"]).startswith(b"unit-test-envelope:"))
        self.assertNotIn("key_blob",status)
        self.assertNotIn("key",status)
        self.assertNotIn("requests",status)
        self.assertFalse(status["enabled"])
        self.assertFalse(status["provider_privacy_verified"])
        self.assertFalse(status["network_tested"])
        self.assertFalse(status["shared_windows_account_isolation"])

    def test_offline_and_each_privacy_gate_prevent_all_transport_calls(self):
        self.settings.set_api_key(SYNTHETIC_KEY)
        for overrides,expected in (({"enabled":False},"disabled"),
                                   ({"parental_consent":False},"privacy_required"),
                                   ({"zdr_attested":False},"privacy_required"),
                                   ({"approval_reference":" "},"privacy_required")):
            with self.subTest(overrides=overrides):
                self.configure(**overrides)
                transport = FakeTransport()
                result = self.ask(self.gateway(transport))
                self.assertEqual(result,{"status":"blocked","code":"api_not_ready"})
                self.assertEqual(transport.calls,[])
                self.assertEqual(self.settings.snapshot()["state"],expected)
        self.settings.clear_api_key()
        self.configure()
        transport = FakeTransport()
        self.assertEqual(self.settings.snapshot()["state"],"key_missing")
        self.assertEqual(self.ask(self.gateway(transport))["status"],"blocked")
        self.assertEqual(transport.calls,[])

    def test_changing_or_clearing_key_resets_attestations_and_keeps_budget(self):
        self.ready(daily_request_limit=1)
        self.assertEqual(self.ask(self.gateway())["status"],"completed")
        self.settings.set_api_key("second-synthetic-key-" + "Y"*32)
        status = self.settings.snapshot()
        for flag in ("enabled","parental_consent","zdr_attested","ready"):
            self.assertFalse(status[flag])
        self.assertEqual(status["approval_reference"],"")
        self.assertEqual(len(json.loads(self.settings.path.read_text())["requests"]),1)
        self.configure(daily_request_limit=1)
        transport = FakeTransport()
        self.assertEqual(self.ask(self.gateway(transport),"another")["code"],"daily_limit")
        self.assertEqual(transport.calls,[])
        self.settings.clear_api_key()
        status = self.settings.snapshot()
        self.assertFalse(status["key_configured"])
        self.assertFalse(status["enabled"])
        self.assertFalse(status["parental_consent"])
        self.assertFalse(status["zdr_attested"])

    def test_invalid_settings_never_create_ready_configuration(self):
        cases = ({"enabled":1},{"model":"https://wrong.example/model"},
                 {"daily_request_limit":True},{"daily_request_limit":501},
                 {"max_output_tokens":127},{"max_output_tokens":1201},
                 {"approval_reference":"bad\nreference"},{"approval_reference":"sk-not-a-reference"})
        for changes in cases:
            with self.subTest(changes=changes),self.assertRaises(ApiError):self.configure(**changes)
        self.assertFalse(self.root.exists())

    def test_invalid_key_and_protector_failure_are_sanitized(self):
        for value in (None,"short","bad key with spaces "*3):
            with self.subTest(value=type(value).__name__),self.assertRaises(ApiError):self.settings.set_api_key(value)
        with mock.patch.object(self.protector,"protect",side_effect=RuntimeError("private-crypto-detail")):
            with self.assertRaisesRegex(ApiError,"^credential_unavailable$"):
                self.settings.set_api_key(SYNTHETIC_KEY)
        self.assertFalse(self.root.exists())

    def test_corrupt_configuration_fails_closed_at_gateway(self):
        self.ready()
        valid = self.settings.path.read_bytes()
        bad_bodies = (b"not-json", b"\xff", b'{"version":1,"version":1}', b"x"*(MAX_CONFIG+1))
        for body in bad_bodies:
            with self.subTest(body_length=len(body)):
                self.settings.path.write_bytes(body)
                transport = FakeTransport()
                gateway = self.gateway(transport)
                self.assertEqual(gateway.status()["state"],"settings_unavailable")
                self.assertFalse(gateway.status()["ready"])
                self.assertEqual(self.ask(gateway)["status"],"blocked")
                self.assertEqual(transport.calls,[])
        self.settings.path.write_bytes(valid)

    def test_boolean_configuration_version_is_rejected(self):
        self.ready()
        value = json.loads(self.settings.path.read_text())
        value["version"] = True
        self.settings.path.write_text(json.dumps(value),encoding="utf-8")
        self.assertFalse(self.gateway().status()["ready"])

    def test_nonfinite_or_control_configuration_fails_closed(self):
        self.ready()
        valid = json.loads(self.settings.path.read_text())
        for edit in ({"max_output_tokens":float("nan")}, {"approval_reference":"bad\x00reference"}):
            with self.subTest(edit=list(edit)):
                self.settings.path.write_text(json.dumps(dict(valid,**edit)),encoding="utf-8")
                gateway = self.gateway()
                self.assertFalse(gateway.status()["ready"])
                self.assertEqual(gateway.status()["state"],"settings_unavailable")

    def test_unprotect_failure_cannot_send_or_leak_details(self):
        self.ready()
        value = json.loads(self.settings.path.read_text())
        value["key_blob"] = base64.b64encode(b"wrong-envelope").decode("ascii")
        self.settings.path.write_text(json.dumps(value),encoding="utf-8")
        transport = FakeTransport()
        result = self.ask(self.gateway(transport))
        self.assertEqual(result,{"status":"blocked","code":"credential_unavailable"})
        self.assertEqual(transport.calls,[])
        self.assertNotIn("synthetic-private",json.dumps(result))


class GatewayTests(ApiFixture):
    def test_success_has_store_false_no_tools_and_no_plaintext_chat_in_budget(self):
        self.ready()
        transport = FakeTransport()
        result = self.ask(self.gateway(transport),text="Синтетическое личное условие")
        self.assertEqual(result["status"],"completed")
        self.assertEqual(result["provider"],"openai")
        key,payload = transport.calls[0]
        self.assertEqual(key,SYNTHETIC_KEY)
        self.assertIs(payload["store"],False)
        self.assertEqual(payload["model"],"unit-test-model")
        self.assertEqual(payload["max_output_tokens"],400)
        self.assertNotIn("tools",payload)
        self.assertIn(POLICY,payload["instructions"])
        saved = self.settings.path.read_text(encoding="utf-8")
        self.assertNotIn("Синтетическое личное условие",saved)
        self.assertNotIn(transport.answer,saved)
        state = json.loads(saved)["requests"]["test-request"]
        self.assertEqual(state["state"],"completed")
        self.assertRegex(state["fingerprint"],r"^[0-9a-f]{64}$")
        self.assertFalse(self.settings.snapshot()["provider_privacy_verified"])

    def test_durable_deduplication_and_conflicting_payload_do_not_resend(self):
        self.ready()
        transport = FakeTransport()
        gateway = self.gateway(transport)
        self.assertEqual(self.ask(gateway)["status"],"completed")
        reopened = self.gateway(transport,settings=self.reopen())
        self.assertEqual(self.ask(reopened)["code"],"request_already_attempted")
        self.assertEqual(self.ask(reopened,text="Другое условие")["code"],"request_conflict")
        self.assertEqual(len(transport.calls),1)

    def test_timeout_reservation_is_durable_uncertain_and_never_retried(self):
        self.ready()
        transport = FakeTransport(error=TimeoutError("private-timeout-key-"+SYNTHETIC_KEY))
        first = self.ask(self.gateway(transport))
        self.assertEqual(first,{"status":"uncertain","code":"request_uncertain"})
        saved = json.loads(self.settings.path.read_text())
        self.assertEqual(saved["requests"]["test-request"]["state"],"uncertain")
        self.assertEqual(self.ask(self.gateway(transport,settings=self.reopen()))["code"],"request_already_attempted")
        self.assertEqual(len(transport.calls),1)
        self.assertNotIn(SYNTHETIC_KEY,json.dumps(first))

    def test_http_rejection_is_sanitized_and_still_consumes_budget(self):
        self.ready(daily_request_limit=1)
        error = urllib.error.HTTPError(ENDPOINT,429,"private-provider-error",{},io.BytesIO(SYNTHETIC_KEY.encode("ascii")))
        transport = FakeTransport(error=error)
        first = self.ask(self.gateway(transport))
        self.assertEqual(first,{"status":"unavailable","code":"provider_429"})
        self.assertEqual(json.loads(self.settings.path.read_text())["requests"]["test-request"]["state"],"rejected")
        self.assertEqual(self.ask(self.gateway(transport),"next-request")["code"],"daily_limit")
        self.assertEqual(len(transport.calls),1)
        self.assertNotIn("private",json.dumps(first))

    def test_daily_budget_survives_reopen_and_resets_on_new_utc_day(self):
        self.ready(daily_request_limit=2)
        transport = FakeTransport()
        self.assertEqual(self.ask(self.gateway(transport),"first")["status"],"completed")
        self.assertEqual(self.ask(self.gateway(transport,settings=self.reopen()),"second")["status"],"completed")
        self.assertEqual(self.ask(self.gateway(transport,settings=self.reopen()),"third")["code"],"daily_limit")
        self.clock_value += timedelta(days=1)
        self.assertEqual(self.ask(self.gateway(transport,settings=self.reopen()),"new-day")["status"],"completed")
        self.assertEqual(len(transport.calls),3)

    def test_shared_settings_lock_enforces_budget_between_gateway_instances(self):
        self.ready(daily_request_limit=1)
        transport = FakeTransport()
        gateways = [self.gateway(transport,settings=self.reopen()) for _ in range(2)]
        barrier = threading.Barrier(2)
        answers = []
        def request(index):
            barrier.wait(timeout=3)
            answers.append(self.ask(gateways[index],"parallel-"+str(index)))
        threads = [threading.Thread(target=request,args=(index,)) for index in range(2)]
        for thread in threads:thread.start()
        for thread in threads:thread.join(timeout=3)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(sorted(answer["status"] for answer in answers),["blocked","completed"])
        self.assertEqual(len(transport.calls),1)

    def test_input_or_role_error_does_not_reserve_budget_or_send(self):
        self.ready()
        transport = FakeTransport()
        cases = ({"text":" "},{"text":"x"*2001},{"request_id":"bad request"},
                 {"history":[{"role":"system","content":"pretend command"}]},
                 {"history":[{"role":"assistant","content":"text","extra":True}]},
                 {"context":[{"id":"raw","text":"unreviewed","source":"raw"}]})
        for fields in cases:
            with self.subTest(fields=list(fields)):
                values = dict(text="Тест",history=[],context=[],request_id="invalid-input")
                values.update(fields)
                result = self.gateway(transport).respond(**values)
                self.assertEqual(result["status"],"blocked")
        self.assertEqual(transport.calls,[])
        self.assertEqual(json.loads(self.settings.path.read_text())["requests"],{})

    def test_brain_does_not_bulk_send_parent_profile_or_session_name(self):
        from cat_brain import CatBrain
        from cat_dialogue import CatDialogue
        self.ready()
        corpus = json.loads((ASSET_ROOT / "assets" / "cat-dialogues-ru-v1.json").read_text(encoding="utf-8-sig"))
        def synthetic_facts(text):
            return "Папу зовут СИНТЕТИЧЕСКИЙ_РОДИТЕЛЬ." if "папу" in text else None
        class ApprovedKnowledge:
            def search(self,text,limit=3):
                return [{"id":"approved-math","text":"Собственная проверенная карточка.","source":"Original local pack","approved":True,
                         "family_profile":{"father":"СИНТЕТИЧЕСКАЯ_СЕМЬЯ"}}]
        transport = FakeTransport()
        brain = CatBrain(CatDialogue(corpus=corpus,family_reply=synthetic_facts),
                         api=self.gateway(transport),knowledge=ApprovedKnowledge())
        brain.reply("Меня зовут СИНТЕТИЧЕСКОЕИМЯ")
        brain.reply("Как зовут папу?")
        self.assertEqual(transport.calls,[])
        brain.reply("Почему у Луны бывают фазы?")
        self.assertEqual(len(transport.calls),1)
        payload = json.dumps(transport.calls[0][1],ensure_ascii=False)
        for forbidden in ("СИНТЕТИЧЕСКОЕИМЯ","СИНТЕТИЧЕСКИЙ_РОДИТЕЛЬ","СИНТЕТИЧЕСКАЯ_СЕМЬЯ","family_profile"):
            self.assertNotIn(forbidden,payload)


class InputTests(unittest.TestCase):
    def test_history_is_bounded_and_latest_user_message_is_preserved(self):
        history = [{"role":"user" if index%2==0 else "assistant","content":str(index)+"x"*1999} for index in range(10)]
        instructions,messages = make_input("Последний вопрос",history,[])
        self.assertEqual(instructions,POLICY)
        self.assertLessEqual(sum(len(row["content"]) for row in messages),10000)
        self.assertEqual(messages[-1],{"role":"user","content":"Последний вопрос"})
        self.assertEqual(len(history),10)
        for invalid in ([{"role":"user","content":"x"}]*13,
                        [{"role":"tool","content":"x"}],
                        [{"role":"user","content":"x"*2001}]):
            with self.assertRaises(ApiError):make_input("Тест",invalid,[])

    def test_only_approved_bounded_context_is_serialized_as_data(self):
        row = {"id":"original-context","text":"x"*700,"source":"s"*150,"approved":True,"execute":"never pass"}
        instructions,messages = make_input("Тест",[],[row])
        self.assertIn("Локальные отобранные карточки (данные)",instructions)
        self.assertNotIn("execute",instructions)
        self.assertNotIn("never pass",instructions)
        self.assertEqual(messages,[{"role":"user","content":"Тест"}])
        for context in ([dict(row,approved=False)],[dict(row,text="x"*701)],
                        [dict(row,source="s"*151)], [row]*4):
            with self.assertRaises(ApiError):make_input("Тест",[],context)

    def test_controls_and_surrogates_are_rejected_in_every_text_channel(self):
        for bad in ("bad\x00text", "bad\x07text", "bad\ud800text"):
            with self.subTest(channel="input"),self.assertRaises(ApiError):make_input(bad,[],[])
            with self.subTest(channel="history"),self.assertRaises(ApiError):
                make_input("Тест",[{"role":"user","content":bad}],[])
            for field in ("id","text","source"):
                row = {"id":"original","text":"Проверено","source":"Original","approved":True}
                row[field] = bad
                with self.subTest(channel=field),self.assertRaises(ApiError):make_input("Тест",[],[row])


class TransportTests(unittest.TestCase):
    def test_fake_opener_sees_fixed_endpoint_post_and_private_header_only(self):
        opener = FakeOpener(FakeResponse(provider_body()))
        transport = ResponsesTransport(opener=opener,timeout=7)
        text = transport.send(SYNTHETIC_KEY,{"model":"synthetic","store":False})
        self.assertEqual(text,"Это проверочный ответ.")
        request,timeout = opener.calls[0]
        self.assertEqual(request.full_url,ENDPOINT)
        self.assertEqual(request.get_method(),"POST")
        self.assertEqual(request.get_header("Authorization"),"Bearer "+SYNTHETIC_KEY)
        self.assertIs(json.loads(request.data)["store"],False)
        self.assertEqual(timeout,7)
        self.assertEqual(opener.response.read_limits,[MAX_RESPONSE+1])

    def test_nonassistant_and_tool_output_is_not_returned_as_reply(self):
        body = json.dumps({"status":"completed","output":[
            {"type":"function_call","name":"not_executed"},
            {"type":"message","role":"user","content":[{"type":"output_text","text":"do not return user"}]},
            {"type":"message","role":"assistant","content":[{"type":"output_text","text":"Ответ помощника."}]}]}).encode("utf-8")
        self.assertEqual(ResponsesTransport(opener=FakeOpener(FakeResponse(body))).send(SYNTHETIC_KEY,{}),"Ответ помощника.")
        with self.assertRaises(ApiError):
            ResponsesTransport(opener=FakeOpener(FakeResponse(provider_body(role="user")))).send(SYNTHETIC_KEY,{})

    def test_invalid_json_incomplete_output_control_and_oversized_text_rejected(self):
        bodies = (b"not-json",b"\xff",b'{"status":"in_progress","output":[]}',
                  b'{"status":"completed","output":"not-a-list"}',
                  b'{"status":"completed","output":[]}',
                  provider_body("x"*3001),provider_body("bad\x00text"),
                  b'{"status":"completed","status":"completed","output":[]}')
        for body in bodies:
            with self.subTest(body_length=len(body)),self.assertRaises((ApiError,ValueError,UnicodeError)):
                ResponsesTransport(opener=FakeOpener(FakeResponse(body))).send(SYNTHETIC_KEY,{})

    def test_response_limit_and_payload_limit_bound_io(self):
        response = FakeResponse(b"x"*(MAX_RESPONSE+10))
        opener = FakeOpener(response)
        with self.assertRaisesRegex(ApiError,"^response_limit$"):
            ResponsesTransport(opener=opener).send(SYNTHETIC_KEY,{})
        self.assertEqual(response.read_limits,[MAX_RESPONSE+1])
        opener = FakeOpener(FakeResponse(provider_body()))
        with self.assertRaisesRegex(ApiError,"^request_limit$"):
            ResponsesTransport(opener=opener).send(SYNTHETIC_KEY,{"text":"x"*65536})
        self.assertEqual(opener.calls,[])

    def test_redirect_is_refused_and_wrong_http_status_not_parsed(self):
        with self.assertRaisesRegex(ApiError,"^redirect_refused$"):
            _NoRedirect().redirect_request(None,None,302,"redirect",{},"https://elsewhere.invalid/")
        response = FakeResponse(provider_body(),code=500)
        with self.assertRaisesRegex(ApiError,"^provider_rejected$"):
            ResponsesTransport(opener=FakeOpener(response)).send(SYNTHETIC_KEY,{})
        self.assertEqual(response.read_limits,[])

    def test_nonfinite_json_and_malformed_content_lists_are_rejected(self):
        valid = json.loads(provider_body())
        bodies = [json.dumps(dict(valid,extra=float("nan"))).encode("utf-8"),provider_body("bad\ud800text")]
        for content in ({"type":"output_text","text":"Ответ."},[{}]*33):
            value = {"status":"completed","output":[{"type":"message","role":"assistant","content":content}]}
            bodies.append(json.dumps(value).encode("utf-8"))
        for body in bodies:
            with self.subTest(body_length=len(body)),self.assertRaises(ApiError):
                ResponsesTransport(opener=FakeOpener(FakeResponse(body))).send(SYNTHETIC_KEY,{})

    def test_default_transport_builds_proxy_free_tls_and_no_redirect_opener(self):
        opener = FakeOpener(FakeResponse(provider_body()))
        with mock.patch("cat_api.urllib.request.build_opener",return_value=opener) as build:
            ResponsesTransport().send(SYNTHETIC_KEY,{"store":False})
        handlers = build.call_args.args
        proxy = next(handler for handler in handlers if hasattr(handler,"proxies"))
        self.assertEqual(proxy.proxies,{})
        self.assertTrue(any(isinstance(handler,_NoRedirect) for handler in handlers))
        self.assertTrue(any(type(handler).__name__=="HTTPSHandler" for handler in handlers))

    def test_apierror_does_not_echo_arbitrary_error_payload(self):
        self.assertEqual(str(ApiError("private / path "+SYNTHETIC_KEY)),"api_unavailable")


if __name__ == "__main__":
    unittest.main()
