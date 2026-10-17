'use strict';
const $ = (id) => document.getElementById(id);
let token = sessionStorage.getItem('relay-token') || '';
const fragment = new URLSearchParams(location.hash.slice(1));
if (fragment.has('token')) {
  token = fragment.get('token');
  sessionStorage.setItem('relay-token', token);
  history.replaceState(null, '', location.pathname);
}
let connected = false;
let authGeneration = 0;
let threads = [];
let checked = null;
let selectedStop = null;
let reading = false;
let quota = null;
let lastQuotaFetch = 0;
let starting = false;
let checkGeneration = 0;
let pickerGeneration = 0;
let lastWatchRead = 0;
let refreshGeneration = 0;
const MUTATION_KEY = 'relay-unconfirmed-web-action-v1';
const MUTATION_LOCK = 'relay-web-mutation-v1';
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const assessments = new Map();
const taskStates = {running:'Running', quota_limited:'Paused by usage limit', needs_attention:'Needs your attention',
  idle:'Turn finished', empty:'Empty conversation', interrupted:'Stopped manually', other_failure:'Other error',
  unsupported:'Not supported', unknown:'Unknown status', connecting:'Waiting for connection', archived:'Archived',
  cloud:'Cloud · not resumable'};
const states = {
  starting: ['Starting', 'muted'], watching: ['Monitoring · running', ''], waiting_quota: ['Monitoring · waiting for usage', 'warning'],
  waiting_connection: ['Monitoring · waiting for connection', 'warning'], needs_attention: ['Needs your attention', 'warning'],
  resumed: ['Resume sent', ''], stopped: ['Stopped', 'muted'], paused: ['Stopped manually', 'muted'],
  uncertain: ['Result unconfirmed', 'danger'], blocked: ['Needs checking', 'warning'],
  budget: ['Resume limit reached', 'muted'], changed: ['Task changed', 'warning'], retrying: ['Retrying connection', 'warning']
};
function node(tag, cls, text) { const el = document.createElement(tag); if (cls) el.className = cls; if (text !== undefined) el.textContent = text; return el; }
function notice(text, warning = false) { $('notice').textContent = text; $('notice').className = warning ? 'notice warning' : 'notice'; $('notice').hidden = !text; }
function busy(button, value, label) { button.disabled = value; button.setAttribute('aria-busy', String(value)); button.textContent = label; }
function timeLabel(value) { return new Date(value * 1000).toLocaleString('en-US', {month:'short', day:'numeric', hour:'numeric', minute:'2-digit'}); }
function pendingMutation() {
  try {
    const raw=localStorage.getItem(MUTATION_KEY);
    if(raw===null) return null;
    const value=JSON.parse(raw);
    if(!value || !['start','stop'].includes(value.action) || !UUID.test(value.threadId) || !Number.isFinite(value.createdAt)) throw new Error('invalid intent');
    return value;
  } catch { return {invalid:true}; } // Corrupt/unavailable storage is never permission to resend.
}
function mutationUnavailable() {
  if(!navigator.locks?.request) return 'This browser doesn\'t support safe cross-tab locking: read-only, can\'t start or stop monitoring.';
  try {
    const key=MUTATION_KEY+'-storage-probe';localStorage.setItem(key,'1');localStorage.removeItem(key);
  } catch { return 'Browser storage is unavailable: read-only, can\'t start or stop monitoring.'; }
  return '';
}
function renderMutation() {
  const pending=pendingMutation(), unavailable=mutationUnavailable();
  $('mutation-warning').hidden=!pending && !unavailable;
  $('mutation-description').textContent=unavailable || (pending ? 'The result of the last start or stop action isn\'t confirmed yet. Refreshing, reconnecting and other tabs never retry automatically. Check the watch records and any in-progress request on this Mac.' : '');
  $('acknowledge-mutation').disabled=!pending || !!unavailable || !connected || !$('confirm-mutation-review').checked || lastWatchRead <= (pending.createdAt || 0);
  $('start-button').disabled=starting || !!pending || !!unavailable;
  $('confirm-stop').disabled=!!pending || !!unavailable;
}
async function acknowledgeMutation() {
  const pending=pendingMutation();
  if(!pending || mutationUnavailable() || !connected || !$('confirm-mutation-review').checked || lastWatchRead <= (pending.createdAt || 0)) { renderMutation(); return; }
  const generation=authGeneration;
  try {
    await navigator.locks.request(MUTATION_LOCK,{ifAvailable:true},async lock=>{
      if(generation!==authGeneration || !connected || !$('confirm-mutation-review').checked || lastWatchRead <= (pending.createdAt || 0))return;
      if(!lock) throw new Error('Another tab is still sending a request; protection can\'t be cleared.');
      const current=pendingMutation();
      if(JSON.stringify(current)!==JSON.stringify(pending)) throw new Error('The action awaiting review has changed; refresh again.');
      localStorage.removeItem(MUTATION_KEY);
      $('confirm-mutation-review').checked=false;
      notice('Protection cleared based on your manual check; nothing was sent or retried.');
    });
  } catch(e) { notice(e.message,true); }
  renderMutation();
}
async function mutate(action,data) {
  const generation=authGeneration;
  const unavailable=mutationUnavailable();
  if(unavailable) throw new Error(unavailable);
  return navigator.locks.request(MUTATION_LOCK,{ifAvailable:true},async lock=>{
    if(generation!==authGeneration) throw new Error('The connection changed; this action wasn\'t sent.');
    if(!lock || pendingMutation()) throw new Error('The previous action isn\'t confirmed yet; check the watch records first.');
    // Persist before dispatch, while holding the same origin-wide Web Lock.
    localStorage.setItem(MUTATION_KEY,JSON.stringify({action,threadId:data.threadId,createdAt:Date.now()}));
    $('confirm-mutation-review').checked=false;renderMutation();
    try {
      const result=await api(action,data);
      if(!result || (action==='start' ? result.threadId!==data.threadId || !Object.hasOwn(states,result.status) : result.stopped!==data.threadId)) throw new Error('The response doesn\'t match the task or status; the result is unconfirmed.');
      localStorage.removeItem(MUTATION_KEY);
      return result;
    } catch(e) {
      if([400,401,403,404,413,415].includes(e.status)) localStorage.removeItem(MUTATION_KEY);
      throw e;
    } finally { renderMutation(); }
  });
}
function connectedUI(value) {
  connected = value;
  $('connection-state').textContent = value ? 'Dashboard connected' : 'Not connected';
  $('connection-state').className = `connection-state${value ? ' connected' : ''}`;
  $('login-panel').hidden = value;
  $('refresh').disabled = !value;
  $('choose-task').disabled = !value;
  $('disconnect').hidden = !value;
  renderMutation();
}
function forget() {
  authGeneration += 1;
  refreshGeneration += 1; reading=false; lastWatchRead=0;
  token = ''; sessionStorage.removeItem('relay-token'); connectedUI(false);
  const loginButton=$('login-form').querySelector('button');if(loginButton)busy(loginButton,false,'Connect');
  quota = null; threads = []; checked = null; lastQuotaFetch = 0; assessments.clear();
  if ($('task-dialog').open) $('task-dialog').close();
  if ($('stop-dialog').open) $('stop-dialog').close();
  $('watch-list').replaceChildren(node('p', 'empty-state', 'Page disconnected. Background monitoring keeps running; reconnect to view or stop it.'));
  $('watch-updated').textContent = 'Monitoring status unknown';
  $('nav-count').textContent = '—'; $('task-count').textContent = '—';
  for (const key of ['primary','secondary']) { $(key+'-percent').textContent = '—'; $(key+'-bar').value = 0; $(key+'-reset').textContent = 'Reset time not read yet'; $(key+'-clock').textContent = '—'; }
  $('quota-updated').textContent = 'Not connected'; $('quota-reason').textContent = 'Real usage is read after connecting';
}
async function api(path, data) {
  const generation = authGeneration;
  const controller=new AbortController();
  const timeout=setTimeout(()=>controller.abort(),70000);
  try {
  const response = await fetch(`/api/${path}`, {
    signal:controller.signal,
    method: data === undefined ? 'GET' : 'POST', cache: 'no-store',
    headers: {'X-Resume-Token': token, ...(data === undefined ? {} : {'Content-Type':'application/json'})},
    ...(data === undefined ? {} : {body: JSON.stringify(data)})
  });
  const result = await response.json();
  if (generation !== authGeneration) throw new Error('The connection changed; read again.');
  if (response.status === 401) { forget(); $('login-error').textContent = result.error; }
  if (!response.ok) { const error=new Error(result.error || 'Service temporarily unavailable; refresh shortly.');error.status=response.status;throw error; }
  return result;
  } finally { clearTimeout(timeout); }
}
function renderQuota(value) {
  quota = value;
  $('version').textContent = `App ${value.app.version} · ${value.app.build}`;
  $('quota-updated').textContent = `Read ${timeLabel(value.checkedAt)}`;
  $('quota-reason').textContent = value.reason;
  for (const key of ['primary','secondary']) {
    const window = value[key];
    if (!window || !Number.isFinite(window.usedPercent) || window.usedPercent < 0 || window.usedPercent > 100) {
      $(key+'-percent').textContent = '—'; $(key+'-bar').value = 0;
      $(key+'-reset').textContent = 'Window data unavailable'; continue;
    }
    const remaining = Math.max(0, Math.min(100, 100-window.usedPercent));
    $(key+'-percent').textContent = `${Math.round(remaining)}%`;
    $(key+'-bar').value = remaining;
    $(key+'-reset').textContent = Number.isFinite(window.resetsAt) ? `Resets ${timeLabel(window.resetsAt)}` : 'Reset time unknown';
  }
  clocks();
}
function clocks() {
  for (const key of ['primary','secondary']) {
    const reset = quota?.[key]?.resetsAt;
    if (!Number.isFinite(reset)) { $(key+'-clock').textContent = '—'; continue; }
    const seconds = Math.ceil(reset - Date.now()/1000);
    if (seconds <= 0) { $(key+'-clock').textContent = 'Time reached, waiting to recheck'; continue; }
    const days = Math.floor(seconds/86400), hours = Math.floor((seconds%86400)/3600), minutes = Math.floor((seconds%3600)/60);
    $(key+'-clock').textContent = days > 0 ? `about ${days}d ${hours}h` : `${String(hours).padStart(2,'0')}:${String(minutes).padStart(2,'0')}:${String(seconds%60).padStart(2,'0')}`;
  }
}
function titleFor(id) { return threads.find(t=>t.id===id)?.title || 'Conversation title unavailable'; }
function renderWatches(rows,readStarted=Date.now()) {
  if(!Array.isArray(rows) || rows.some(r=>!r || !UUID.test(r.thread_id) || ![0,1,false,true].includes(r.enabled))) throw new Error('Watch records are malformed; status can\'t be confirmed.');
  lastWatchRead=Math.max(lastWatchRead,readStarted);renderMutation();
  $('watch-error').textContent = '';
  $('task-count').textContent = rows.length;
  $('nav-count').textContent = rows.filter(r=>r.enabled).length;
  $('watch-updated').textContent = `Watch records updated ${new Date().toLocaleTimeString('en-US')} · refreshes every 15 s while open`;
  if (!rows.length) {
    const empty = node('div','empty-state');
    const symbol = node('div','relay-symbol'); symbol.setAttribute('aria-hidden','true'); symbol.append(node('span'),node('i'),node('span'));
    empty.append(symbol,node('h3','','No monitored tasks yet'),node('p','','Pick a long task that\'s running. If it stops because the usage limit ran out, Relay waits for the reset.'),node('span','empty-note','Never takes over other tasks · Never approves actions'));
    $('watch-list').replaceChildren(empty); return;
  }
  const cards = rows.map(row => {
    const card=node('article','watch-card'), main=node('div'), side=node('div','watch-side');
    main.append(node('h3','',titleFor(row.thread_id)),node('p','',row.reason));
    const meta=node('div','watch-meta'); meta.append(node('span','',`Attempts ${row.attempts} / ${row.max_resumes}`),node('span','',`Updated ${timeLabel(row.updated)}`));
    if (row.process === 'not_running') meta.append(node('span','error','The watcher process has exited; check, then start it again'));
    main.append(meta);
    const state=states[row.status] || ['Unknown status','warning']; side.append(node('span',`pill ${state[1]}`,state[0]));
    const button=node('button','button secondary',row.enabled ? 'Stop monitoring' : 'Check task');
    button.addEventListener('click',()=>row.enabled ? openStop(row) : openChooser(row.thread_id)); side.append(button); card.append(main,side); return card;
  });
  $('watch-list').replaceChildren(...cards);
}
async function refresh(forceQuota=false) {
  if (!connected || reading || document.hidden) return;
  reading=true; busy($('refresh'),true,'Reading…');
  const generation=authGeneration, request=++refreshGeneration, readStarted=Date.now();
  const current=()=>generation===authGeneration && request===refreshGeneration;
  const jobs = [api('watches').then(rows=>{if(current())renderWatches(rows,readStarted);}).catch(e=>{
    if(!current()) return;
    $('watch-error').textContent=`Read failed: ${e.message}`;
    $('watch-updated').textContent='These are the last records read and may not reflect the current status.';
    for(const button of $('watch-list').querySelectorAll('button')) button.disabled=true;
  })];
  if(forceQuota || Date.now()-lastQuotaFetch>60000) {
    lastQuotaFetch=Date.now();
    jobs.push(api('quota').then(value=>{if(current())renderQuota(value);}).catch(e=>{ if(!current())return; $('quota-reason').textContent=`Read failed: ${e.message}`; $('quota-updated').textContent='Data may be stale; refresh'; }));
  }
  await Promise.allSettled(jobs); if(current()){reading=false; busy($('refresh'),!connected,'Refresh');}
}
async function login(event) {
  event?.preventDefault();
  const generation=++authGeneration;
  refreshGeneration+=1;reading=false;lastWatchRead=0;
  if(event) { token=$('token').value.trim(); sessionStorage.setItem('relay-token',token); }
  if(!token) return;
  const button=$('login-form').querySelector('button'); busy(button,true,'Connecting…'); $('login-error').textContent='';
  try {
    const readStarted=Date.now();
    const rows=await api('watches'); if(generation!==authGeneration)return;
    connectedUI(true); $('token').value=''; renderWatches(rows,readStarted); notice('');
    await Promise.allSettled([refresh(true), api('threads').then(async data=>{if(generation!==authGeneration)return;threads=data;const started=Date.now();const watches=await api('watches');if(generation===authGeneration)renderWatches(watches,started);})]);
  } catch(e) { if(generation===authGeneration){$('login-error').textContent=e.message; connectedUI(false);} }
  finally { if(generation===authGeneration)busy(button,false,'Connect'); }
}
function selectedId() { return $('thread-id').value.trim().toLowerCase(); }
function updateSelection() {
  const id=selectedId(), task=threads.find(t=>t.id===id);
  $('selected-task').hidden=!id;
  $('selected-title').textContent=task?.title || 'Manually entered task';
  $('check-button').hidden=!id;
  for(const button of $('thread-options').querySelectorAll('.thread-option')) {
    const selected=button.dataset.threadId===id;
    button.setAttribute('aria-pressed',String(selected));
    button.querySelector('.selection-marker').hidden=!selected;
    const assessment=assessments.get(button.dataset.threadId);
    const label=button.querySelector('.task-state');
    if(label) label.textContent=assessment ? `Last check: ${taskStates[assessment.taskState]||'Unknown status'}` : 'Select to check status';
  }
}
function renderThreads() {
  const term=$('search').value.trim().toLocaleLowerCase();
  const matches=threads.filter(t=>($('show-archived').checked || !t.archived) && (t.title+' '+t.id).toLocaleLowerCase().includes(term));
  $('thread-count').textContent=`${threads.length} local conversations read · showing ${matches.length}`;
  const buttons=matches.map(t=>{
    const button=node('button','thread-option'); button.type='button'; button.dataset.threadId=t.id;
    const marker=node('span','selection-marker','Selected'); marker.hidden=true;
    button.append(node('strong','',t.title),node('span','task-state meta','Select to check status'),marker);
    if(t.archived) button.append(node('span','pill muted','Archived'));
    if(t.host && t.host!=='local') button.append(node('span','pill muted','Cloud · not resumable'));
    button.addEventListener('click',()=>{ if(starting) return; $('thread-id').value=t.id; updateSelection(); checkTask(); }); return button;
  });
  $('thread-options').replaceChildren(...(buttons.length ? buttons : [node('p','meta',term?'No matching conversations; try another title.':'No local conversations yet; start a task in Codex first.')]));
  updateSelection();
}
function invalidateCheck() {
  checkGeneration+=1;
  checked=null; $('start-form').hidden=true; $('check-result').hidden=true; $('confirm-start').checked=false; $('action-error').textContent='';
  busy($('check-button'),false,'Check task');
}
async function openChooser(id='') {
  if(starting) { notice('A start request is still in progress; wait for the result and check the watch records.',true); return; }
  const generation=++pickerGeneration;
  pickerView(false);
  $('manual-selection').open=false;
  $('thread-id').value=id; invalidateCheck(); updateSelection(); $('search').value=''; $('thread-error').textContent='';
  $('thread-options').replaceChildren(node('p','meta','Reading all local conversations…'));
  $('thread-count').textContent='Reading the full list (including later pages)';
  if(!$('task-dialog').open) $('task-dialog').showModal();
  try {
    const rows=await api('threads');
    if(generation!==pickerGeneration || !$('task-dialog').open) return;
    threads=rows; renderThreads();
  } catch(e) {
    if(generation!==pickerGeneration || !$('task-dialog').open) return;
    $('thread-error').textContent=e.message; $('thread-options').replaceChildren();
  }
}
function pickerView(details) {
  $('picker-browser').hidden=details;
  $('task-details').hidden=!details;
}
function backToList() {
  const button=node('button','button secondary back-to-list','Back to conversation list');
  button.type='button';
  button.addEventListener('click',()=>{ if(starting) return; invalidateCheck(); pickerView(false); $('search').focus({preventScroll:true}); });
  return button;
}
async function checkTask(event) {
  event?.preventDefault();
  if(starting) return;
  const id=selectedId(); invalidateCheck(); updateSelection();
  if(!/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(id)) {
    pickerView(false);
    $('action-error').textContent='Pick a conversation from the list, or enter the full conversation ID under the advanced option.'; return;
  }
  const generation=checkGeneration;
  const stillSelected=()=>generation===checkGeneration && selectedId()===id && $('task-dialog').open;
  busy($('check-button'),true,'Checking…');
  pickerView(true);
  $('task-details').scrollTop=0;
  $('check-result').className='check-result';
  $('check-result').replaceChildren(backToList(),node('p','','Checking task status…'));
  $('check-result').hidden=false;
  try {
    const result=await api(`check?threadId=${encodeURIComponent(id)}`);
    if(!stillSelected()) return;
    if(result.threadId!==id) throw new Error('The check result doesn\'t match the selected task, so it was discarded. Check again.');
    checked=result; assessments.set(id,result); updateSelection();
    const canStart=canEnroll(result);
    $('check-result').className=`check-result${canStart?'':' blocked'}`;
    $('check-result').replaceChildren(backToList(),node('span','pill',taskStates[result.taskState]||'Unknown status'),node('strong','',result.title||titleFor(id)),node('p','',result.reason||'Task status couldn\'t be confirmed; it can\'t be monitored.'),node('p','meta',`${result.source==='live'?'Live app status':'Saved conversation history'} · ${canStart?'Can be monitored':'No need to monitor, or not monitorable yet'}`));
    if(result.source==='history' && canStart) $('check-result').append(node('p','meta','The live connection isn\'t back yet. After you start, the tool will try to load the original conversation in the app. History is only used for screening; nothing is sent until live status and usage are re-verified.'));
    if(result.model) $('check-result').append(node('p','meta',`Keeps model: ${result.model}`));
    if(result.ignoredInterruptedPickerRequests) $('check-result').append(node('p','meta',`Verified that ${result.ignoredInterruptedPickerRequests} old folder-picker request(s) belong to interrupted turns; current approvals still block resuming.`));
    $('check-result').hidden=false; $('start-form').hidden=!canStart;
    $('check-result').scrollIntoView({block:'start'});
  } catch(e) {
    if(!stillSelected()) return;
    checked=null; $('start-form').hidden=true;
    $('check-result').className='check-result blocked';
    $('check-result').replaceChildren(backToList(),node('strong','','Status unavailable: '+titleFor(id)),node('p','',e.message),node('p','meta','Neither live status nor history could confirm the task is eligible, or the interface is incompatible. Monitoring wasn\'t started and nothing was sent; existing watches aren\'t affected by this check.'));
    $('check-result').hidden=false;
    $('check-result').scrollIntoView({block:'start'});
  } finally { if(stillSelected()) busy($('check-button'),false,'Check again'); }
}
function canEnroll(result) {
  return result?.canMonitor===true && ['running','quota_limited'].includes(result.taskState) && ['wait','resume'].includes(result.decision);
}
async function startTask(event) {
  event.preventDefault();
  if(!checked || checked.threadId!==selectedId() || !canEnroll(checked) || starting) return;
  starting=true; busy($('start-button'),true,'Starting…'); $('action-error').textContent='';
  const generation=authGeneration;
  const data={threadId:checked.threadId,maxResumes:Number($('max-resumes').value),confirmed:$('confirm-start').checked};
  if($('limit-id').value.trim()) data.limitId=$('limit-id').value.trim();
  try {
    const result=await mutate('start',data); if(generation!==authGeneration)return; $('task-dialog').close();
    notice(`Task submitted for monitoring. Current status: ${(states[result.status]||['Pending confirmation'])[0]}. The watch records below are authoritative.`); await refresh();
  } catch(e) {
    if(generation!==authGeneration)return;
    $('action-error').textContent=e.message;
    // An uncertain HTTP outcome must never encourage a blind resubmit.
    checked=null; $('start-form').hidden=true; $('check-result').hidden=true;
    notice('The start result needs checking. Refresh the watch records before deciding whether to re-check the task.',true); await refresh();
  } finally { starting=false; busy($('start-button'),false,'Start monitoring');renderMutation(); }
}
function openStop(row) { selectedStop=row.thread_id; $('stop-task').textContent=titleFor(row.thread_id); $('stop-error').textContent=''; $('stop-dialog').showModal();renderMutation(); }
async function stopTask() {
  if(!selectedStop) return;
  const generation=authGeneration, target=selectedStop;
  busy($('confirm-stop'),true,'Stopping…');
  try { await mutate('stop',{threadId:target}); if(generation!==authGeneration)return; $('stop-dialog').close(); notice('Future automatic resumes stopped. A running Codex task isn\'t affected.'); await refresh(); }
  catch(e) { if(generation===authGeneration){$('stop-error').textContent=e.message;notice('The stop result needs checking. Refresh the records and verify manually; nothing retries automatically.',true);} }
  finally { busy($('confirm-stop'),false,'Stop monitoring');renderMutation(); }
}
for (const dialog of [$('task-dialog'), $('stop-dialog')]) {
  dialog.addEventListener('keydown', event=>{
    if(event.key==='Escape') { event.preventDefault(); dialog.close(); }
  });
}
$('login-form').addEventListener('submit',login);
$('refresh').addEventListener('click',()=>refresh(true));
$('choose-task').addEventListener('click',()=>openChooser());
$('close-dialog').addEventListener('click',()=>$('task-dialog').close());
$('search').addEventListener('input',renderThreads);
$('show-archived').addEventListener('change',renderThreads);
$('thread-id').addEventListener('input',()=>{ invalidateCheck(); updateSelection(); });
$('task-dialog').addEventListener('close',()=>{ pickerGeneration+=1; invalidateCheck(); });
$('check-form').addEventListener('submit',checkTask);
$('start-form').addEventListener('submit',startTask);
$('cancel-stop').addEventListener('click',()=>$('stop-dialog').close());
$('confirm-stop').addEventListener('click',stopTask);
$('disconnect').addEventListener('click',()=>{forget();notice('Page disconnected; background monitoring isn\'t affected.');});
setInterval(()=>refresh(),15000); setInterval(clocks,1000);
document.addEventListener('visibilitychange',()=>{if(!document.hidden) refresh(true);});
if(token) login();

$('confirm-mutation-review').addEventListener('change',renderMutation);
$('acknowledge-mutation').addEventListener('click',acknowledgeMutation);
$('refresh-mutation').addEventListener('click',()=>refresh());
window.addEventListener('storage',event=>{if(event.key===MUTATION_KEY || event.key===null){$('confirm-mutation-review').checked=false;renderMutation();}});
renderMutation();
