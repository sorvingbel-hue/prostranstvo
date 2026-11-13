'use strict';

// Only these small declarative actions cross the local API. No code generator
// is loaded and Blockly workspace data is never stored by this editor.
const PetTricksEditor = (() => {
  const ACTIONS = Object.freeze({turn:'Повернуться',heart:'Показать сердечко',blink:'Поморгать',purr:'Помурлыкать',wait:'Подождать',stretch:'Потянуться'});
  const EVENTS = Object.freeze({greet:'я появился',petted:'меня погладили',manual:'нажали «Проверить»'});
  const IDS = /^[a-f0-9]{32}$/;
  const fail = message => { throw new Error(message); };
  const exact = (value,keys) => value && typeof value==='object' && !Array.isArray(value) && Object.keys(value).sort().join('|')===keys.slice().sort().join('|');
  function validateConfig(value) {
    if (!exact(value,['version','enabled','rules']) || value.version!==1 || typeof value.enabled!=='boolean' || !Array.isArray(value.rules) || value.rules.length>8) fail('В мастерской может быть не больше 8 трюков.');
    const ids=new Set();
    for (const rule of value.rules) {
      if (!exact(rule,['id','name','enabled','event','cooldown_seconds','steps']) || typeof rule.id!=='string' || !IDS.test(rule.id) || ids.has(rule.id)) fail('У трюка повреждён идентификатор. Добавь новый блок «Когда…».');
      ids.add(rule.id);
      if (typeof rule.name!=='string' || !rule.name.trim() || rule.name.length>60 || /[\x00-\x1f\x7f]/.test(rule.name)) fail('Назови каждый трюк: от 1 до 60 букв и знаков.');
      if (typeof rule.enabled!=='boolean' || !Object.hasOwn(EVENTS,rule.event) || !Number.isInteger(rule.cooldown_seconds) || rule.cooldown_seconds<10 || rule.cooldown_seconds>3600) fail('Пауза между трюками — от 10 до 3600 секунд.');
      if (!Array.isArray(rule.steps) || !rule.steps.length || rule.steps.length>8) fail('Соедини с каждым событием от 1 до 8 действий.');
      let total=0,purr=0;
      for (const step of rule.steps) {
        if (!exact(step,['action','duration_ms']) || !Object.hasOwn(ACTIONS,step.action) || !Number.isInteger(step.duration_ms) || step.duration_ms<100 || step.duration_ms>5000) fail('Длительность действия — от 0,1 до 5 секунд.');
        total+=step.duration_ms;
        if (step.action==='purr') { purr+=step.duration_ms; if (step.duration_ms>2000) fail('Одно мурчание — не дольше 2 секунд.'); }
      }
      if (total>30000) fail('Один трюк может длиться не больше 30 секунд.');
      if (purr>3000) fail('Всего мурчания в одном трюке — не больше 3 секунд.');
    }
    return value;
  }
  function newId() {
    const bytes=new Uint8Array(16);globalThis.crypto.getRandomValues(bytes);
    return Array.from(bytes,n=>n.toString(16).padStart(2,'0')).join('');
  }
  function compileWorkspace(workspace,enabled,idFactory=newId) {
    const tops=workspace.getTopBlocks(true);
    if (tops.length>8) fail('Оставь не больше 8 событий. Лишний блок можно перетащить в корзину.');
    const rules=tops.map(hat=>{
      if (hat.type!=='cat_event') fail('Присоедини свободные действия к блоку «Когда…».');
      if (!IDS.test(hat.data || '')) hat.data=idFactory();
      const steps=[],seen=new Set();let next=hat.getNextBlock();
      while (next) {
        if (seen.has(next) || steps.length>=8) fail('У одного события может быть не больше 8 действий.');
        seen.add(next);
        const action=next.type.startsWith('cat_')?next.type.slice(4):'';
        if (!Object.hasOwn(ACTIONS,action)) fail('Этот блок не является действием Мурчика.');
        steps.push({action,duration_ms:Math.round(Number(next.getFieldValue('SECONDS'))*1000)});
        next=next.getNextBlock();
      }
      return {id:hat.data,name:String(hat.getFieldValue('NAME') || '').trim(),enabled:hat.getFieldValue('ENABLED')==='TRUE',event:hat.getFieldValue('EVENT'),cooldown_seconds:Number(hat.getFieldValue('COOLDOWN')),steps};
    });
    return validateConfig({version:1,enabled:!!enabled,rules});
  }
  function tokenFromHash(hash) {
    let token='';
    try { token=hash.startsWith('#token=')?new URLSearchParams(hash.slice(1)).get('token'):(hash.startsWith('#')?decodeURIComponent(hash.slice(1)):''); } catch (_) { return ''; }
    return typeof token==='string' && /^[A-Za-z0-9_-]{24,256}$/.test(token)?token:'';
  }
  function configReady(native) { return !!native && native.available===true && native.config_ok===true; }
  function receiptSignature(result) {
    const native=result.native || {},preview=result.preview || {};
    return JSON.stringify([native.available===true,native.config_ok===true,preview.request_id || '',preview.state || preview.status || '',preview.reason || '',preview.action || '']);
  }
  class StatusHold {
    constructor() { this.key=null; }
    get active() { return this.key!==null; }
    hold(result) { this.key=receiptSignature(result); }
    userAction() { this.key=null; }
    update(result) {
      if (this.active && this.key===receiptSignature(result)) return true;
      this.key=null;return false;
    }
  }
  return {ACTIONS,EVENTS,validateConfig,compileWorkspace,tokenFromHash,newId,configReady,receiptSignature,StatusHold};
})();

if (typeof module!=='undefined' && module.exports) module.exports=PetTricksEditor;

if (typeof document!=='undefined') document.addEventListener('DOMContentLoaded',()=>{
  const $=id=>document.getElementById(id);
  const token=PetTricksEditor.tokenFromHash(location.hash);
  // Keep the capability in this tab's memory, never localStorage, logs or URL.
  history.replaceState(null,'',location.pathname+location.search);
  const state={workspace:null,config:null,dirty:false,loading:false,busy:false,closed:false,polling:false,epoch:0,revision:0,nativeReady:false,lastReceipt:{},statusHold:new PetTricksEditor.StatusHold()};
  const status=(text,error=false)=>{ $('status').textContent=text;$('status').classList.toggle('error',error); };
  const apiError=text=>{state.statusHold.hold(state.lastReceipt);status(text,true);};
  function buttons() {
    $('save').disabled=state.busy || !state.config || !state.dirty || state.closed;
    $('preview').disabled=state.busy || state.dirty || state.closed || !state.nativeReady || !$('preview-rule').value;
    $('add-template').disabled=state.busy || state.closed || !state.workspace;
    $('enabled').disabled=state.busy || state.closed || !state.config;
    $('dirty-status').textContent=state.dirty?'Есть изменения — сохрани их перед проверкой.':'Сохранено на этом ПК.';
  }
  function dirty() { if (!state.loading) {state.statusHold.userAction();state.revision++;state.dirty=true;buttons();} }
  async function api(path,payload) {
    if (!token) throw new Error('auth');
    const controller=new AbortController();const timer=setTimeout(()=>controller.abort(),6500);
    try {
      const response=await fetch(path,{method:payload===undefined?'GET':'POST',credentials:'omit',cache:'no-store',referrerPolicy:'no-referrer',headers:{Authorization:'Bearer '+token,...(payload===undefined?{}:{'Content-Type':'application/json'})},body:payload===undefined?undefined:JSON.stringify(payload),signal:controller.signal});
      const result=await response.json();
      if (!response.ok || !result || result.error) throw new Error(typeof result.error==='string'?result.error:'local_api');
      return result;
    } finally {clearTimeout(timer);}
  }
  function defineBlocks() {
    const defs=[{type:'cat_event',message0:'Когда %1 · %2',args0:[{type:'field_dropdown',name:'EVENT',options:Object.entries(PetTricksEditor.EVENTS).map(([key,label])=>[label,key])},{type:'field_input',name:'NAME',text:'Новый трюк'}],message1:'Включён %1 · пауза %2 с',args1:[{type:'field_checkbox',name:'ENABLED',checked:true},{type:'field_number',name:'COOLDOWN',value:30,min:10,max:3600,precision:1}],nextStatement:'CatAction',colour:35,tooltip:'Событие запускает присоединённые действия сверху вниз.'}];
    for (const [action,label] of Object.entries(PetTricksEditor.ACTIONS)) defs.push({type:'cat_'+action,message0:label+' %1 с',args0:[{type:'field_number',name:'SECONDS',value:action==='purr' ? .7 : 1,min:.1,max:action==='purr'?2:5,precision:.1}],previousStatement:'CatAction',nextStatement:'CatAction',colour:action==='wait'?210:action==='purr'?290:155,tooltip:'Действие выполняет только настольный котик.'});
    const define=Blockly.common && Blockly.common.defineBlocksWithJsonArray?Blockly.common.defineBlocksWithJsonArray:Blockly.defineBlocksWithJsonArray;
    define(defs);
  }
  function buildRule(rule,x=25,y=25) {
    const hat=state.workspace.newBlock('cat_event');hat.data=rule.id;
    hat.setFieldValue(rule.name,'NAME');hat.setFieldValue(rule.event,'EVENT');hat.setFieldValue(rule.enabled?'TRUE':'FALSE','ENABLED');hat.setFieldValue(String(rule.cooldown_seconds),'COOLDOWN');
    hat.initSvg();hat.render();hat.moveBy(x,y);let previous=hat;
    for (const step of rule.steps) {
      const block=state.workspace.newBlock('cat_'+step.action);block.setFieldValue(String(step.duration_ms/1000),'SECONDS');block.initSvg();block.render();previous.nextConnection.connect(block.previousConnection);previous=block;
    }
    return hat;
  }
  function loadConfig(config) {
    PetTricksEditor.validateConfig(config);state.loading=true;Blockly.Events.disable();
    try {
      state.workspace.clear();let y=25;
      for (const rule of config.rules) {const hat=buildRule(rule,25,y);y+=hat.getHeightWidth().height+45;}
      $('enabled').checked=config.enabled;state.config=config;state.dirty=false;
      const selected=$('preview-rule').value;$('preview-rule').replaceChildren();
      for (const rule of config.rules) {const option=document.createElement('option');option.value=rule.id;option.textContent=rule.name;$('preview-rule').append(option);}
      if (config.rules.some(rule=>rule.id===selected)) $('preview-rule').value=selected;
    } finally {Blockly.Events.enable();state.loading=false;buttons();}
  }
  function renderReceipt(result,force=false) {
    const native=result.native || {},preview=result.preview || {};
    state.nativeReady=PetTricksEditor.configReady(native);
    $('native-status').textContent=state.nativeReady?'Настольный котик отвечает.':(native.available===true?'Котик ещё проверяет сохранённые трюки…':'Котик пока не подтвердил связь. Открой его на рабочем столе.');
    const held=state.statusHold.update(result);state.lastReceipt=result;buttons();
    if (!force && (held || state.busy || state.dirty)) return;
    const messages={idle:'Мастерская готова. Сохрани трюк и проверь его на котике.',queued:'Запуск ожидает ответа настольного котика.',started:'Котик начал трюк.',completed:'Котик подтвердил: трюк завершён.',blocked:'Трюк заблокирован или приостановлен. Могут действовать режим уроков, полный экран или тихий режим.',stopped:'Остановка подтверждена. Можно изменить трюк.',failed:'Котик сообщил об ошибке трюка.',expired:'Время ожидания истекло. Запуск не подтверждён.'};
    const reasons={context_blocked:'Сейчас тихий режим, полный экран или уроки. Трюк заблокирован; после паузы его можно проверить снова.',cooldown:'Мурчик отдыхает между трюками. Подожди несколько секунд и проверь снова.',busy:'Мурчик занят другим действием. Подожди или нажми «Стоп».',disabled:'Этот трюк сейчас выключен.',config_invalid:'Котик не смог прочитать настройки. Сохрани трюки ещё раз.',config_changed:'Настройки изменились во время трюка. Запуск прерван.',rule_missing:'Сохранённый трюк больше не найден.',clock_invalid:'Котик не смог проверить время запуска.',interrupted:'Трюк прерван. Автоматического повтора нет.'};
    const phase=preview.state || preview.status;
    status(phase==='queued' && preview.action==='stop'?'Остановка ожидает ответа настольного котика.':((phase==='blocked' || phase==='failed') && reasons[preview.reason]?reasons[preview.reason]:(messages[phase] || 'Состояние запуска пока неизвестно.')));
  }
  async function poll() {
    if (state.closed || state.polling || state.busy) return;
    state.polling=true;const epoch=state.epoch;
    try {const result=await api('/api/state');if (epoch===state.epoch) renderReceipt(result);} catch (_) {if (!state.closed && epoch===state.epoch && !state.statusHold.active) status('Не удалось связаться с локальной мастерской. Если она закрыта, открой её снова через котика.',true);} finally {state.polling=false;}
  }
  $('enabled').addEventListener('change',dirty);
  $('preview-rule').addEventListener('change',()=>{state.statusHold.userAction();buttons();});
  $('save').addEventListener('click',async()=>{
    if (state.busy || state.closed) return;
    state.statusHold.userAction();
    let config;try {config=PetTricksEditor.compileWorkspace(state.workspace,$('enabled').checked);} catch (error) {status(error.message,true);return;}
    state.busy=true;const epoch=++state.epoch,revision=state.revision;buttons();status('Сохраняю трюки на этом ПК…');
    try {const result=await api('/api/save',{config});renderReceipt(result);if (revision===state.revision) loadConfig(result.config);else {state.config=result.config;state.dirty=true;}if (epoch===state.epoch) status(revision!==state.revision?'Сохранена предыдущая версия. Твои новые изменения остались в блоках — сохрани их отдельно.':(config.enabled?'Сохранено. Трюки включены; для ручной проверки выбери трюк.':'Сохранено. Трюки выключены; включи их и сохрани, когда будешь готов.'));}
    catch (_) {if (epoch===state.epoch) apiError('Сохранить не получилось. Блоки остались в редакторе — можно повторить.');}
    finally {state.busy=false;buttons();}
  });
  $('preview').addEventListener('click',async()=>{
    if (state.busy || state.dirty || state.closed || !state.nativeReady || !$('preview-rule').value) return;
    state.statusHold.userAction();
    state.busy=true;const epoch=++state.epoch;buttons();status('Передаю запрос котику. Запуск ещё не подтверждён…');
    try {const result=await api('/api/preview',{rule_id:$('preview-rule').value});state.busy=false;if (epoch===state.epoch) renderReceipt(result);}
    catch (error) {if (epoch===state.epoch) apiError(error.message==='native_unavailable'?'Настольный котик сейчас не отвечает. Открой котика и повтори проверку.':(error.message==='config_invalid'?'Котик ещё не подтвердил сохранённые трюки. Подожди проверки и повтори.':'Не удалось подтвердить запрос. Нажми «Стоп» перед повтором.'));}
    finally {state.busy=false;buttons();}
  });
  $('stop').addEventListener('click',async()=>{
    if (state.closed) return;
    state.statusHold.userAction();
    const epoch=++state.epoch;
    status('Запрашиваю остановку. Жду ответа котика…');
    try {const result=await api('/api/stop',{});if (epoch===state.epoch) renderReceipt(result,true);} catch (_) {if (epoch===state.epoch) apiError('Остановка пока не подтверждена: локальная мастерская недоступна.');}
  });
  $('close-editor').addEventListener('click',async()=>{
    if (state.closed) return;
    state.statusHold.userAction();
    if (state.dirty && !confirm('В редакторе есть несохранённые блоки. Закрыть мастерскую без сохранения?')) return;
    ++state.epoch;
    try {await api('/api/close',{});state.closed=true;buttons();$('stop').disabled=true;status('Локальная мастерская закрыта. Эту вкладку можно закрыть.');$('native-status').textContent='Чтобы продолжить, снова нажми «Научить трюку» у котика.';}
    catch (_) {apiError('Закрытие мастерской пока не подтверждено. Можно повторить.');}
  });
  $('add-template').addEventListener('click',()=>{
    if (!state.workspace || state.busy || state.closed) return;
    state.statusHold.userAction();
    if (state.workspace.getTopBlocks(false).length>=8) {status('В мастерской уже 8 событий. Удали ненужное перед добавлением.',true);return;}
    const event=$('template').value;
    const templates={petted:{name:'Спасибо за ласку',steps:[{action:'turn',duration_ms:500},{action:'heart',duration_ms:1200},{action:'purr',duration_ms:700}]},greet:{name:'Доброе утро, Мурчик',steps:[{action:'blink',duration_ms:500},{action:'stretch',duration_ms:1500}]},manual:{name:'Мой первый трюк',steps:[{action:'blink',duration_ms:500},{action:'wait',duration_ms:300},{action:'heart',duration_ms:1000}]}};
    const template=templates[event];if (!template) return;
    const tops=state.workspace.getTopBlocks(false);const y=tops.reduce((end,block)=>Math.max(end,block.getRelativeToSurfaceXY().y+block.getHeightWidth().height+35),25);
    buildRule({id:PetTricksEditor.newId(),name:template.name,event,enabled:true,cooldown_seconds:30,steps:template.steps},25,y);
    dirty();status('Пример добавлен. Измени блоки и сохрани. Глобальный переключатель сам не включается.');
  });
  window.addEventListener('beforeunload',event=>{if (state.dirty && !state.closed) {event.preventDefault();event.returnValue='';}});
  async function start() {
    if (!token) {state.closed=true;buttons();$('stop').disabled=true;status('Ссылка мастерской недействительна. Открой «Научить трюку» через котика.',true);return;}
    if (typeof Blockly==='undefined') {status('Не найдены локальные файлы Blockly. Переустанови полную сборку приложения.',true);return;}
    try {
      defineBlocks();
      state.workspace=Blockly.inject('workspace',{media:'/blockly/media/',sounds:false,toolbox:{kind:'categoryToolbox',contents:[{kind:'category',name:'События',colour:35,contents:[{kind:'block',type:'cat_event'}]},{kind:'category',name:'Действия',colour:155,contents:Object.keys(PetTricksEditor.ACTIONS).map(action=>({kind:'block',type:'cat_'+action}))}]},trashcan:true,comments:false,disable:false,maxBlocks:72,move:{scrollbars:true,drag:true,wheel:true},zoom:{controls:true,wheel:true,startScale:.9,maxScale:1.5,minScale:.5,scaleSpeed:1.1}});
      state.workspace.addChangeListener(event=>{if (!event.isUiEvent) dirty();});
      const result=await api('/api/state');loadConfig(result.config);renderReceipt(result);
      window.petTricksEditor=Object.freeze({compile:()=>PetTricksEditor.compileWorkspace(state.workspace,$('enabled').checked),getSavedConfig:()=>JSON.parse(JSON.stringify(state.config))});
      window.addEventListener('resize',()=>Blockly.svgResize(state.workspace));
      setInterval(poll,1500);
    } catch (_) {status('Мастерскую не удалось открыть. Локальные данные не изменены; попробуй открыть её заново через котика.',true);}
  }
  start();
});
