'use strict';
// Pure compiler regression tests. No browser, speaker, network or desktop pet.
const assert=require('node:assert/strict');
const editor=require('../assets/tricks/editor.js');
const id='1'.repeat(32);
const rule=()=>({id,name:'Ласковый кот',enabled:true,event:'petted',cooldown_seconds:30,steps:[{action:'heart',duration_ms:1000}]});
const config=()=>({version:1,enabled:false,rules:[rule()]});
let passed=0;
function test(name,work) {work();passed++;}
test('canonical JSON stays data and global enable remains explicit',()=>{
  const value=config();assert.equal(editor.validateConfig(value),value);assert.equal(value.enabled,false);
  value.rules[0].name='<script>alert(1)</script>';assert.equal(editor.validateConfig(value).rules[0].name,value.rules[0].name);
});
test('unknown commands, fields and nested execution are rejected',()=>{
  for (const change of [v=>v.command='powershell',v=>v.rules[0].steps[0].action='exec',v=>v.rules[0].steps[0].code='arbitrary',v=>v.rules[0].event='browser_url']) {
    const value=config();change(value);assert.throws(()=>editor.validateConfig(value));
  }
});
test('bounded loops, durations, counts and purring are enforced',()=>{
  for (const change of [v=>v.rules=Array(9).fill(rule()),v=>v.rules[0].steps=[],v=>v.rules[0].steps=Array(9).fill({action:'wait',duration_ms:100}),v=>v.rules[0].steps=Array(7).fill({action:'wait',duration_ms:5000}),v=>v.rules[0].steps=[{action:'purr',duration_ms:2100}],v=>v.rules[0].steps=[{action:'purr',duration_ms:2000},{action:'purr',duration_ms:1500}],v=>v.rules[0].cooldown_seconds=0,v=>v.rules[0].steps[0].duration_ms=NaN]) {
    const value=config();change(value);assert.throws(()=>editor.validateConfig(value));
  }
});
test('compiler emits only canonical actions and keeps stable rule IDs',()=>{
  const action={type:'cat_heart',getFieldValue:()=>1.2,getNextBlock:()=>null};
  const fields={NAME:' Мой трюк ',ENABLED:'TRUE',EVENT:'manual',COOLDOWN:'30'};
  const hat={type:'cat_event',data:null,getFieldValue:key=>fields[key],getNextBlock:()=>action};
  const workspace={getTopBlocks:()=>[hat]};
  const first=editor.compileWorkspace(workspace,false,()=>id);
  assert.deepEqual(first,{version:1,enabled:false,rules:[{id,name:'Мой трюк',enabled:true,event:'manual',cooldown_seconds:30,steps:[{action:'heart',duration_ms:1200}]}]});
  assert.equal(editor.compileWorkspace(workspace,false,()=>{throw Error('must retain ID');}).rules[0].id,id);
});
test('detached, cyclic and non-whitelisted blocks cannot be saved',()=>{
  assert.throws(()=>editor.compileWorkspace({getTopBlocks:()=>[{type:'cat_heart'}]},false));
  const action={type:'cat_wait',getFieldValue:()=>.1};action.getNextBlock=()=>action;
  const hat={type:'cat_event',data:id,getNextBlock:()=>action,getFieldValue:key=>({NAME:'Цикл',ENABLED:'TRUE',EVENT:'manual',COOLDOWN:10})[key]};
  assert.throws(()=>editor.compileWorkspace({getTopBlocks:()=>[hat]},false));
});
test('fragment token is bounded; no query token or malformed value',()=>{
  const token='A_-'.repeat(12);
  assert.equal(editor.tokenFromHash('#token='+token),token);assert.equal(editor.tokenFromHash('#'+token),token);
  for (const value of ['?token='+token,'#tiny','#token=<script>','#token='+'x'.repeat(257),'#%ED%A0%80']) assert.equal(editor.tokenFromHash(value),'');
});
test('preview waits for both a fresh native connection and accepted config',()=>{
  for (const native of [undefined,null,{}, {available:true}, {available:false,config_ok:true}, {available:true,config_ok:false}, {available:1,config_ok:true}, {available:true,config_ok:'true'}]) assert.equal(editor.configReady(native),false);
  assert.equal(editor.configReady({available:true,config_ok:true}),true);
});
test('explicit errors survive idle heartbeat polling until action or meaningful acknowledgement',()=>{
  const snapshot={native:{available:true,config_ok:true,updated_utc:1},preview:{state:'idle',request_id:'',reason:'idle',action:'',updated_utc:1}};
  const hold=new editor.StatusHold();hold.hold(snapshot);
  const heartbeat=JSON.parse(JSON.stringify(snapshot));heartbeat.native.updated_utc=200;heartbeat.preview.updated_utc=200;
  assert.equal(hold.update(heartbeat),true);assert.equal(hold.active,true);
  hold.userAction();assert.equal(hold.update(heartbeat),false);
  hold.hold(snapshot);
  const started=JSON.parse(JSON.stringify(snapshot));started.preview={state:'started',request_id:'request-1',reason:'started',action:'run'};
  assert.equal(hold.update(started),false);assert.equal(hold.active,false);
  hold.hold(snapshot);
  const checking=JSON.parse(JSON.stringify(snapshot));checking.native.config_ok=false;
  assert.equal(hold.update(checking),false);
});
console.log('Pet tricks editor: '+passed+' pure compiler tests passed.');
