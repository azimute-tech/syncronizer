"""The single-page admin UI (vanilla HTML/CSS/JS, no dependencies)."""

PAGE = r"""<!doctype html>
<html lang="pt-br">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Syncronizer</title>
<style>
  :root { --bg:#0f1419; --panel:#1a2230; --line:#2b3648; --fg:#e6edf3; --mut:#8b98a9;
          --acc:#3b82f6; --ok:#22c55e; --warn:#f59e0b; --err:#ef4444; }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--fg); font:14px/1.5 system-ui,Segoe UI,Arial; }
  header { padding:14px 20px; border-bottom:1px solid var(--line); display:flex; align-items:center; gap:14px; }
  header h1 { font-size:18px; margin:0; }
  header .pill { font-size:12px; padding:3px 9px; border-radius:99px; background:var(--panel); color:var(--mut); }
  header .pill.ok { color:var(--ok); } header .pill.warn { color:var(--warn); }
  nav { display:flex; gap:4px; padding:10px 20px 0; border-bottom:1px solid var(--line); }
  nav button { background:none; border:none; color:var(--mut); padding:8px 14px; cursor:pointer; border-radius:8px 8px 0 0; font-size:14px; }
  nav button.active { color:var(--fg); background:var(--panel); }
  main { padding:20px; max-width:1000px; }
  .card { background:var(--panel); border:1px solid var(--line); border-radius:12px; padding:18px; margin-bottom:16px; }
  .card h2 { margin:0 0 12px; font-size:15px; }
  .grid { display:grid; grid-template-columns:240px 1fr; gap:10px 14px; align-items:center; }
  label { color:var(--mut); }
  input[type=text],input[type=password],input[type=number] { width:100%; padding:8px 10px; background:var(--bg); border:1px solid var(--line); border-radius:8px; color:var(--fg); }
  input[type=checkbox]{ width:18px; height:18px; }
  button.btn { background:var(--acc); color:#fff; border:none; padding:9px 16px; border-radius:8px; cursor:pointer; font-size:14px; }
  button.btn.sec { background:#334155; } button.btn.danger { background:var(--err); }
  button.btn.small { padding:5px 10px; font-size:13px; }
  table { width:100%; border-collapse:collapse; }
  th,td { text-align:left; padding:9px 8px; border-bottom:1px solid var(--line); }
  th { color:var(--mut); font-weight:600; }
  .muted { color:var(--mut); } .ok{color:var(--ok)} .warn{color:var(--warn)} .err{color:var(--err)}
  pre.log { background:#0b0f14; border:1px solid var(--line); border-radius:8px; padding:12px; max-height:60vh; overflow:auto; white-space:pre-wrap; font:12px/1.45 ui-monospace,Consolas,monospace; }
  .row { display:flex; gap:10px; align-items:center; flex-wrap:wrap; }
  .toast { position:fixed; bottom:20px; right:20px; background:var(--panel); border:1px solid var(--line); padding:12px 16px; border-radius:10px; opacity:0; transition:.2s; }
  .toast.show { opacity:1; } .sub { font-size:12px; color:var(--mut); margin:-6px 0 12px; }
</style>
</head>
<body>
<header>
  <h1>🐂 Syncronizer</h1>
  <span id="svc" class="pill">…</span>
  <span id="fb" class="pill">Firebird: …</span>
  <span style="flex:1"></span>
  <button class="btn sec small" onclick="restart()">Reiniciar serviço</button>
</header>
<nav>
  <button data-tab="status" class="active">Status</button>
  <button data-tab="config">Configuração</button>
  <button data-tab="endpoints">Endpoints</button>
  <button data-tab="logs">Logs</button>
</nav>
<main>
  <section id="tab-status"></section>
  <section id="tab-config" hidden></section>
  <section id="tab-endpoints" hidden></section>
  <section id="tab-logs" hidden></section>
</main>
<div id="toast" class="toast"></div>

<script>
const $ = s => document.querySelector(s);
let STATE = null;
let configRendered = false;   // render the config form ONCE so auto-refresh never wipes typing
function toast(msg, ms=2500){ const t=$('#toast'); t.textContent=msg; t.classList.add('show'); setTimeout(()=>t.classList.remove('show'), ms); }
async function api(path, opts){ const r = await fetch(path, opts); if(!r.ok) throw new Error(await r.text()); const ct=r.headers.get('content-type')||''; return ct.includes('json')? r.json(): r.text(); }

document.querySelectorAll('nav button').forEach(b=>b.onclick=()=>{
  document.querySelectorAll('nav button').forEach(x=>x.classList.remove('active'));
  b.classList.add('active');
  ['status','config','endpoints','logs'].forEach(t=>$('#tab-'+t).hidden = (t!==b.dataset.tab));
  if(b.dataset.tab==='logs') loadLogs();
});

async function refresh(){
  STATE = await api('/api/state');
  const s = STATE.status;
  $('#svc').textContent = 'Serviço: ' + s.service; $('#svc').className='pill ok';
  $('#fb').textContent = 'Firebird: ' + (s.firebird_configured ? (s.firebird_ok?'OK':'configurado') : 'NÃO configurado');
  $('#fb').className = 'pill ' + (s.firebird_ok?'ok':(s.firebird_configured?'warn':'warn'));
  renderStatus(); renderEndpoints();
  if(!configRendered){ renderConfig(); configRendered=true; }   // never re-render while editing
}

function renderStatus(){
  const s = STATE.status;
  $('#tab-status').innerHTML = `<div class="card"><h2>Estado</h2><div class="grid">
    <label>Versão</label><div>${s.version}</div>
    <label>Último ciclo</label><div>${s.last_cycle||'(nenhum ainda)'}</div>
    <label>Firebird configurado</label><div class="${s.firebird_configured?'ok':'err'}">${s.firebird_configured?'sim':'não — configure na aba Configuração'}</div>
    <label>API configurada</label><div class="${s.api_configured?'ok':'warn'}">${s.api_configured?'sim':'não'}</div>
    <label>Total enviados / pendentes</label><div>${s.totals.sent} / ${s.totals.pending}</div>
  </div></div>` + renderBackup(s);
}

function renderBackup(s){
  const lb = s.last_backup;
  let last = '(nenhum ainda)';
  if(lb){
    if(lb.skipped) last = '<span class="warn">backup em andamento…</span>';
    else if(lb.ok) last = `<span class="ok">OK</span> — ${lb.data_referencia||''} · ${fmtBytes(lb.size_bytes)} · ${lb.finished_at||''}`;
    else last = `<span class="err">FALHOU</span> — ${lb.error||''} · ${lb.finished_at||''}`;
  }
  return `<div class="card"><h2>Backup do banco</h2><div class="grid">
    <label>Backup noturno</label><div class="${s.backup_enabled?'ok':'muted'}">${s.backup_enabled?'habilitado':'desabilitado — habilite na aba Configuração'}</div>
    <label>Último backup</label><div>${last}</div>
  </div><div class="row" style="margin-top:12px"><button class="btn" onclick="backupNow()">Executar backup agora</button></div></div>`;
}

function fmtBytes(n){
  if(n==null) return '-';
  const u=['B','KB','MB','GB','TB']; let i=0, v=Number(n);
  while(v>=1024 && i<u.length-1){ v/=1024; i++; }
  return (i?v.toFixed(1):v) + ' ' + u[i];
}
async function backupNow(){
  if(!confirm('Executar um backup do banco agora? Pode levar alguns minutos.')) return;
  try{ await api('/api/backup-now',{method:'POST'}); toast('Backup iniciado — acompanhe pelos logs e pelo status.'); }
  catch(e){ toast('Erro ao iniciar backup: '+e.message); }
}

function renderConfig(){
  const groups = {};
  STATE.config.forEach(f=>{ (groups[f.group]=groups[f.group]||[]).push(f); });
  let html = '<p class="sub">Caminhos podem usar / ou \\ — normalizo automaticamente. Após salvar, reinicie para aplicar.</p>';
  for(const g in groups){
    html += `<div class="card"><h2>${g}</h2><div class="grid">`;
    groups[g].forEach(f=>{
      const id='cfg_'+f.key;
      if(f.type==='bool') html += `<label for="${id}">${f.label}</label><div><input type="checkbox" id="${id}" ${f.value?'checked':''}></div>`;
      else { const t = f.type==='int'?'number':(f.is_secret?'password':'text'); html += `<label for="${id}">${f.label}</label><input type="${t}" id="${id}" value="${String(f.value??'').replace(/"/g,'&quot;')}">`; }
    });
    html += `</div></div>`;
  }
  html += `<div class="row"><button class="btn" onclick="saveConfig()">Salvar</button><button class="btn sec" onclick="saveAndRestart()">Salvar e reiniciar</button></div>`;
  $('#tab-config').innerHTML = html;
}
function collectConfig(){ const out={}; STATE.config.forEach(f=>{ const el=$('#cfg_'+f.key); out[f.key]= f.type==='bool'? el.checked : el.value; }); return out; }
async function saveConfig(){ await api('/api/config',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(collectConfig())}); toast('Configuração salva. Reinicie para aplicar.'); configRendered=false; refresh(); }
async function saveAndRestart(){ await api('/api/config',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(collectConfig())}); toast('Salvo. Reiniciando…'); setTimeout(restart, 400); }

function renderEndpoints(){
  let rows = STATE.endpoints.map(e=>`<tr>
    <td><b>${e.name}</b><div class="sub">${e.api_path||''}</div></td>
    <td class="${e.enabled?'ok':'muted'}">${e.enabled?'ativo':'desativado'}</td>
    <td>${e.counts.total}</td><td>${e.counts.sent}</td><td>${e.counts.pending}</td><td>${e.counts.deleted}</td>
    <td class="sub">${e.last_send_at||'-'}</td>
    <td class="row">
      <button class="btn small sec" onclick="epAction('${e.name}','${e.enabled?'disable':'enable'}')">${e.enabled?'Desativar':'Ativar'}</button>
      <button class="btn small danger" onclick="epReset('${e.name}')">Limpar + re-sincronizar</button>
    </td></tr>`).join('');
  $('#tab-endpoints').innerHTML = `<div class="card"><h2>Endpoints</h2>
    <table><thead><tr><th>Endpoint</th><th>Estado</th><th>Total</th><th>Enviados</th><th>Pendentes</th><th>Deletados</th><th>Último envio</th><th></th></tr></thead>
    <tbody>${rows||'<tr><td colspan=8 class="muted">nenhum endpoint</td></tr>'}</tbody></table></div>`;
}
async function epAction(name, action){ await api('/api/endpoint',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({name,action})}); toast('Feito.'); refresh(); }
async function epReset(name){ if(!confirm('Limpar TODOS os dados de "'+name+'" e re-sincronizar do zero?')) return;
  await api('/api/endpoint',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({name,action:'reset'})}); toast('Dados limpos — vai re-sincronizar no próximo ciclo.'); refresh(); }

async function loadLogs(){ const txt = await api('/api/logs?n=300'); $('#tab-logs').innerHTML = `<div class="card"><div class="row" style="justify-content:space-between"><h2>Logs</h2><button class="btn sec small" onclick="loadLogs()">Atualizar</button></div><pre class="log">${txt.replace(/[&<]/g,c=>({'&':'&amp;','<':'&lt;'}[c]))}</pre></div>`; const p=$('#tab-logs pre'); if(p) p.scrollTop=p.scrollHeight; }
async function restart(){ try{ await api('/api/restart',{method:'POST'}); }catch(e){} toast('Reiniciando o serviço… recarregue em ~20s.'); }

refresh();
// auto-refresh status/endpoints, but NEVER while the Config or Logs tab is open
setInterval(()=>{ if(!$('#tab-config').hidden || !$('#tab-logs').hidden) return; refresh(); }, 15000);
</script>
</body></html>
"""
