"""verbatim.gui — a zero-dependency localhost web UI for browsing meetings.

Serves a small single-page app plus a JSON API backed by verbatim.core.
Bound to 127.0.0.1 only. Launch with `verbatim gui`.
"""
from __future__ import annotations

import json
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from . import core
from .core import VerbatimError

# ── background jobs (analysis & attribution can take minutes on long meetings) ─
_jobs: dict = {}                        # (full_id, kind) -> {"running","error"}
_jobs_lock = threading.Lock()


def _run_job(full_id: str, kind: str, fn) -> None:
    with _jobs_lock:
        _jobs[(full_id, kind)] = {"running": True, "error": None}
    err = None
    try:
        fn(full_id)
    except Exception as e:  # noqa: BLE001 — surface any failure to the client
        err = str(e)
    with _jobs_lock:
        _jobs[(full_id, kind)] = {"running": False, "error": err}


def _start_job(full_id: str, kind: str, fn) -> None:
    with _jobs_lock:
        j = _jobs.get((full_id, kind))
        if j and j["running"]:
            return
    threading.Thread(target=_run_job, args=(full_id, kind, fn), daemon=True).start()


def _job_status(full_id: str, kind: str) -> dict:
    with _jobs_lock:
        return dict(_jobs.get((full_id, kind), {"running": False, "error": None}))


INDEX = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Verbatim — Meeting Notes</title>
<style>
 :root{--bg:#14161b;--panel:#1c1f26;--panel2:#242832;--line:#2f3542;
   --txt:#e7e9ee;--dim:#98a1b2;--acc:#5c9ded;--acc2:#3a7bd5;--red:#e0577b;
   --grn:#57c98a;--amber:#e0a458;--chip:#2b3a52}
 :root[data-theme="light"]{--bg:#f4f6fa;--panel:#ffffff;--panel2:#eef1f6;--line:#d9dee7;
   --txt:#1b1f27;--dim:#5b6472;--acc:#2f6bd0;--acc2:#3a7bd5;--red:#d6335f;
   --grn:#2f9d63;--amber:#b7791f;--chip:#e4ecfa}
 .talktime{margin:0 0 14px}
 .ttbar{display:flex;height:8px;border-radius:5px;overflow:hidden;background:var(--panel2);margin:2px 0 6px}
 .ttseg.c0{background:var(--acc)}.ttseg.c1{background:var(--grn)}.ttseg.c2{background:var(--amber)}.ttseg.c3{background:var(--red)}.ttseg.c4{background:#9b7ede}.ttseg.c5{background:var(--dim)}
 .ttleg{display:flex;gap:12px;flex-wrap:wrap;font-size:11px;color:var(--dim)}
 .ttlab i{display:inline-block;width:9px;height:9px;border-radius:2px;margin-right:4px;vertical-align:-1px}
 .ttlab i.c0{background:var(--acc)}.ttlab i.c1{background:var(--grn)}.ttlab i.c2{background:var(--amber)}.ttlab i.c3{background:var(--red)}.ttlab i.c4{background:#9b7ede}.ttlab i.c5{background:var(--dim)}
 .snip{color:var(--dim);font-size:11px;margin-top:5px;line-height:1.4}
 *{box-sizing:border-box}
 body{margin:0;font:14px/1.6 -apple-system,Segoe UI,Roboto,sans-serif;
   background:var(--bg);color:var(--txt);height:100vh;display:flex;flex-direction:column}
 header{display:flex;align-items:center;gap:12px;padding:10px 16px;
   background:var(--panel);border-bottom:1px solid var(--line)}
 header h1{font-size:16px;margin:0;font-weight:700;letter-spacing:.4px}
 header .dot{width:9px;height:9px;border-radius:50%;background:var(--dim)}
 header.rec .dot{background:var(--red);animation:p 1.4s infinite}
 @keyframes p{0%{box-shadow:0 0 0 0 rgba(224,87,123,.5)}70%{box-shadow:0 0 0 8px rgba(224,87,123,0)}}
 header .spacer{flex:1}
 button{background:var(--panel2);color:var(--txt);border:1px solid var(--line);
   border-radius:7px;padding:7px 13px;cursor:pointer;font-size:13px;white-space:nowrap}
 button:hover{border-color:var(--acc)}
 button.primary{background:var(--acc2);border-color:var(--acc);color:#fff}
 button.danger:hover{border-color:var(--red);color:var(--red)}
 button:disabled{opacity:.5;cursor:default}
 main{flex:1;display:flex;min-height:0}
 #side{width:330px;border-right:1px solid var(--line);display:flex;
   flex-direction:column;background:var(--panel)}
 #search{margin:10px;padding:9px 11px;background:var(--bg);border:1px solid var(--line);
   border-radius:8px;color:var(--txt)}
 #list{overflow:auto;flex:1}
 .item{padding:11px 14px;border-bottom:1px solid var(--line);cursor:pointer}
 .item:hover{background:var(--panel2)}
 .item.sel{background:var(--panel2);border-left:3px solid var(--acc);padding-left:11px}
 .item .t{font-weight:600}
 .item .m{color:var(--dim);font-size:12px;margin-top:3px;display:flex;gap:8px;align-items:center}
 .badge{font-size:10px;padding:1px 6px;border-radius:10px;background:var(--chip);color:var(--acc)}
 #detail{flex:1;overflow:auto;padding:22px 30px;max-width:920px}
 .titlerow{display:flex;align-items:center;gap:10px;margin-bottom:4px}
 input.rn{background:transparent;border:1px solid transparent;color:var(--txt);
   border-radius:6px;padding:4px 6px;font-size:22px;font-weight:700;flex:1;min-width:0}
 input.rn:hover,input.rn:focus{border-color:var(--line);background:var(--bg)}
 .meta{color:var(--dim);font-size:13px;margin-bottom:16px}
 .chips{display:flex;gap:6px;flex-wrap:wrap;margin:8px 0 16px}
 .chip{background:var(--chip);border-radius:14px;padding:3px 11px;font-size:12px}
 .bar{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:14px}
 #lblbox{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px 16px;margin-bottom:16px}
 #lblbox .hint{color:var(--dim);font-size:12px;margin-bottom:8px}
 .lblrow{display:flex;align-items:center;gap:10px;margin:7px 0}
 .tagpill{background:var(--chip);border-radius:6px;padding:3px 10px;font-size:12px;color:var(--acc);min-width:110px;text-align:center}
 .lblinput{background:var(--bg);border:1px solid var(--line);color:var(--txt);border-radius:6px;padding:6px 10px;flex:1}
 .tabs{display:flex;gap:2px;border-bottom:1px solid var(--line);margin-bottom:18px}
 .tab{padding:9px 16px;cursor:pointer;color:var(--dim);border-bottom:2px solid transparent}
 .tab.on{color:var(--txt);border-bottom-color:var(--acc)}
 .empty{color:var(--dim);text-align:center;margin-top:14vh}
 .empty button{margin-top:14px}
 .spin{display:inline-block;width:13px;height:13px;border:2px solid var(--dim);
   border-top-color:transparent;border-radius:50%;animation:s .7s linear infinite;vertical-align:-2px}
 @keyframes s{to{transform:rotate(360deg)}}
 /* markdown */
 .md h2{font-size:13px;text-transform:uppercase;letter-spacing:.7px;color:var(--acc);
   margin:26px 0 8px;padding-bottom:5px;border-bottom:1px solid var(--line)}
 .md h3{margin:16px 0 6px;font-size:15px}
 .md h1{font-size:20px;margin:0 0 8px}
 .md ul{margin:.3em 0 .6em 1.1em;padding:0}
 .md li{margin:3px 0}
 .md p{margin:.5em 0}
 .md code{background:var(--panel2);padding:1px 5px;border-radius:4px;font-size:12.5px}
 .md hr{border:0;border-top:1px solid var(--line);margin:18px 0}
 .md table{border-collapse:collapse;width:100%;margin:10px 0;font-size:13px}
 .md th,.md td{border:1px solid var(--line);padding:7px 10px;text-align:left;vertical-align:top}
 .md th{background:var(--panel2);color:var(--acc)}
 .md tr:nth-child(even) td{background:rgba(255,255,255,.02)}
 .md blockquote{border-left:3px solid var(--acc);margin:8px 0;padding:2px 14px;color:var(--dim)}
 .tx .spk{display:inline-block;margin-top:14px;color:var(--acc);font-weight:700}
 .tx .ts{color:var(--dim);font-size:12px;margin-right:6px}
 .tx .seg{margin:2px 0}
 .block{margin:6px 0}
 .spkrow{display:flex;align-items:center;gap:8px;margin:15px 0 4px}
 .spksel{background:var(--panel2);color:var(--acc);border:1px solid var(--line);
   border-radius:6px;padding:4px 9px;font-weight:700;font-size:13px;cursor:pointer}
 .spksel:hover{border-color:var(--acc)}
 .aitag{font-size:10px;background:var(--chip);color:var(--acc);border-radius:8px;padding:1px 7px}
 .ident-banner{background:var(--panel);border:1px solid var(--line);border-radius:8px;
   padding:11px 14px;margin-bottom:12px;color:var(--dim)}
 .hintbar{color:var(--dim);font-size:12px;margin:0 0 12px}
</style></head><body>
<header id="hdr">
  <span class="dot"></span><h1>VERBATIM</h1>
  <span id="recinfo" class="meta"></span>
  <span class="spacer"></span>
  <button id="themeBtn" title="Toggle light / dark">☀</button>
  <button id="startBtn" class="primary">● Start recording</button>
  <button id="stopBtn" style="display:none">■ Stop &amp; save</button>
</header>
<main>
  <div id="side">
    <input id="search" placeholder="Search titles, transcripts, summaries…">
    <div id="list"></div>
  </div>
  <div id="detail"><div class="empty">Select a meeting, or start recording.</div></div>
</main>
<script>
const $=s=>document.querySelector(s), api=(u,o)=>fetch(u,o).then(r=>r.json());
let cur=null, meetings=[], tab='summary';

function applyTheme(t){document.documentElement.dataset.theme=t;localStorage.setItem('vbTheme',t);
  const b=$('#themeBtn');if(b)b.textContent=t==='light'?'☾':'☀';}
applyTheme(localStorage.getItem('vbTheme')||'dark');
$('#themeBtn').onclick=()=>applyTheme(document.documentElement.dataset.theme==='light'?'dark':'light');

function esc(s){return s.replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]))}
function inline(s){return s.replace(/\*\*(.+?)\*\*/g,'<b>$1</b>')
  .replace(/`(.+?)`/g,'<code>$1</code>')
  .replace(/\[(\d{1,2}:\d{2}(?::\d{2})?)\]/g,'<span class="ts">[$1]</span>')}

// Small but real markdown renderer: headings, lists, tables, hr, blockquote.
function mdToHtml(t){
  if(!t) return '';
  const L=esc(t).split('\n'); let out=[], i=0, inList=false;
  const closeList=()=>{if(inList){out.push('</ul>');inList=false}};
  while(i<L.length){
    let ln=L[i];
    // table
    if(/^\s*\|.*\|\s*$/.test(ln) && i+1<L.length && /^\s*\|?[\s:|-]+\|?\s*$/.test(L[i+1])){
      closeList();
      const row=r=>r.trim().replace(/^\||\|$/g,'').split('|').map(c=>c.trim());
      const head=row(ln); i+=2; let body=[];
      while(i<L.length && /^\s*\|.*\|\s*$/.test(L[i])){body.push(row(L[i])); i++}
      out.push('<table><thead><tr>'+head.map(h=>'<th>'+inline(h)+'</th>').join('')+
        '</tr></thead><tbody>'+body.map(r=>'<tr>'+r.map(c=>'<td>'+inline(c)+'</td>').join('')+
        '</tr>').join('')+'</tbody></table>');
      continue;
    }
    let m;
    if(m=ln.match(/^(#{1,3})\s+(.*)/)){closeList();
      out.push('<h'+m[1].length+'>'+inline(m[2])+'</h'+m[1].length+'>')}
    else if(/^\s*---+\s*$/.test(ln)){closeList(); out.push('<hr>')}
    else if(m=ln.match(/^\s*>\s?(.*)/)){closeList(); out.push('<blockquote>'+inline(m[1])+'</blockquote>')}
    else if(m=ln.match(/^\s*[-*]\s+(.*)/)){if(!inList){out.push('<ul>');inList=true}
      out.push('<li>'+inline(m[1])+'</li>')}
    else{closeList(); if(ln.trim()) out.push('<p>'+inline(ln)+'</p>')}
    i++;
  }
  closeList(); return out.join('\n');
}
function transcriptHtml(t){
  return esc(t).split('\n').map(ln=>{let m;
    if(m=ln.match(/^###\s+(.*)/)) return '<div class="spk">'+m[1]+'</div>';
    if(m=ln.match(/^\*\[(\d[\d:]+)\]\*\s*(.*)/)) return '<div class="seg"><span class="ts">['+m[1]+']</span>'+esc(m[2])+'</div>';
    if(/^#/.test(ln)||!ln.trim()) return '';
    return '<div class="seg">'+esc(ln)+'</div>';
  }).join('');
}

async function refreshStatus(){
  const s=await api('/api/status'); const rec=!!s.recording;
  $('#hdr').classList.toggle('rec',rec);
  $('#startBtn').style.display=rec?'none':'';
  $('#stopBtn').style.display=rec?'':'none';
  $('#recinfo').textContent=rec?('● Recording — '+(s.title||'')):'';
}
async function loadList(){
  const q=$('#search').value.trim();
  meetings = q ? await api('/api/search?q='+encodeURIComponent(q))
               : await api('/api/meetings');
  $('#list').innerHTML=meetings.map(m=>`<div class="item ${m.id===cur?'sel':''}" data-id="${m.id}">
    <div class="t">${esc(m.title||'(untitled)')}</div>
    <div class="m">${m.started_human} · ${m.duration_human}
      ${m.analyzed?'<span class="badge">✨</span>':''}
      ${m.match_in&&m.match_in!=='title'?'<span class="badge">'+esc(m.match_in)+'</span>':''}</div>
    ${m.snippet?`<div class="snip">${esc(m.snippet)}</div>`:''}</div>`).join('')
    || '<div class="empty" style="margin-top:30px">No meetings</div>';
  document.querySelectorAll('.item').forEach(e=>e.onclick=()=>openMeeting(e.dataset.id));
}

async function openMeeting(id){
  cur=id; loadList();
  const d=$('#detail'); d.innerHTML='<div class="empty"><span class="spin"></span></div>';
  const m=await api('/api/meeting/'+id);
  const chips=(m.speakers||[]).map(s=>`<span class="chip">${esc(s)}</span>`).join('');
  const stats=(m.stats||[]).filter(s=>s.pct>0);
  const tt=stats.length?`<div class="talktime"><div class="ttbar">${stats.map((s,i)=>
    `<span class="ttseg c${i%6}" style="flex:${s.pct}" title="${esc(s.name)}: ${s.pct}% · ${s.words} words"></span>`).join('')}</div>
    <div class="ttleg">${stats.map((s,i)=>`<span class="ttlab"><i class="c${i%6}"></i>${esc(s.name)} ${s.pct}%</span>`).join('')}</div></div>`:'';
  d.innerHTML=`
    <div class="titlerow"><input class="rn" id="rn" value="${esc(m.title||'')}"></div>
    <div class="meta">${m.started_human} · ${m.duration_human} · ${m.chunk_count} chunks · <code>${m.short_id}</code></div>
    <div class="chips">${chips||''}</div>
    ${tt}
    <div class="bar">
      <button id="sum" class="primary">✨ ${m.analyzed?'Re-analyze':'Analyze with Claude'}</button>
      <button id="copy">⧉ Copy summary</button>
      <button id="exp">⬇ Summary .md</button>
      <button id="ident">🧠 Identify speakers (AI)</button>
      <button id="note">💾 Save .md note</button>
      <button id="lbl">🏷 Label speakers</button>
      <button id="del" class="danger">🗑 Delete</button></div>
    <div id="lblbox" style="display:none"></div>
    <div class="tabs">
      <div class="tab ${tab==='summary'?'on':''}" data-t="summary">AI Summary</div>
      <div class="tab ${tab==='transcript'?'on':''}" data-t="transcript">Transcript</div></div>
    <div id="pane"></div>`;
  $('#rn').onchange=async()=>{await fetch('/api/meeting/'+id+'/rename',{method:'POST',
    body:JSON.stringify({title:$('#rn').value})}); loadList()};
  document.querySelectorAll('.tab').forEach(e=>e.onclick=()=>{tab=e.dataset.t;openMeeting(id)});
  const analysis=m.analysis||'';
  window._analysis=analysis;
  const pane=$('#pane');
  if(tab==='transcript'){
    await renderTranscript(id, pane);
  }else{
    pane.className='md';
    pane.innerHTML=analysis?mdToHtml(analysis):
      `<div class="empty">No AI summary yet.<br><button class="primary" onclick="doAnalyze()">✨ Analyze with Claude</button>
       <div class="meta" style="margin-top:8px">~1 min · runs locally via Claude Code, no cloud model server</div></div>`;
  }
  $('#sum').onclick=doAnalyze;
  $('#ident').onclick=doIdentify;
  $('#copy').onclick=()=>{navigator.clipboard.writeText(window._analysis||'');
    $('#copy').textContent='✓ Copied'};
  $('#exp').onclick=async()=>{const r=await fetch('/api/meeting/'+id+'/summary-md');
    if(!r.ok){alert((await r.text())||'No summary yet — analyze first');return}
    const blob=await r.blob(),a=document.createElement('a');
    a.href=URL.createObjectURL(blob);
    a.download=((m.title||'summary').replace(/[^\w -]+/g,'').trim()||'summary')+' - summary.md';
    a.click();URL.revokeObjectURL(a.href)};
  $('#note').onclick=async e=>{e.target.innerHTML='<span class="spin"></span> Saving…';
    const r=await api('/api/meeting/'+id+'/note',{method:'POST'});
    e.target.textContent=r.path?'✓ Saved .md':'⚠ failed'; if(r.error)alert(r.error)};
  $('#lbl').onclick=()=>{const box=$('#lblbox');
    if(box.style.display!=='none'){box.style.display='none';return}
    const slots=(m.speaker_slots||[]).filter(s=>s.num!==null);
    if(!slots.length){box.innerHTML='<div class="hint">No speakers detected yet.</div>';box.style.display='';return}
    box.innerHTML='<div class="hint">Name each speaker, then Save — updates the transcript and chips.</div>'+
      slots.map(s=>`<div class="lblrow"><span class="tagpill">${esc(s.raw)}</span>→
        <input class="lblinput" data-num="${s.num}" value="${esc(s.label)}" placeholder="Full name…"></div>`).join('')+
      '<button id="lblsave" class="primary" style="margin-top:8px">Save names</button>';
    box.style.display='';
    box.querySelector('#lblsave').onclick=async e=>{e.target.innerHTML='<span class="spin"></span> Saving…';
      const ins=[...box.querySelectorAll('.lblinput')].filter(i=>i.value.trim());
      for(const i of ins){await fetch('/api/meeting/'+id+'/label',{method:'POST',
        body:JSON.stringify({speaker:i.dataset.num,name:i.value.trim()})})}
      openMeeting(id)};
    box.querySelector('.lblinput')?.focus()};
  $('#del').onclick=async()=>{if(!confirm('Delete this meeting permanently?'))return;
    await fetch('/api/meeting/'+id+'/delete',{method:'POST'}); cur=null;
    $('#detail').innerHTML='<div class="empty">Deleted.</div>'; loadList()};
}
async function renderTranscript(id, pane){
  const data=await api('/api/meeting/'+id+'/utterances');
  const utts=data.utterances||[], names=data.names||[];
  if(!utts.length){pane.className='tx';pane.innerHTML='<i>No transcript.</i>';return}
  const anyAI=utts.some(u=>u.ai);
  // group consecutive utterances by effective speaker
  const blocks=[]; let cur=null;
  utts.forEach(u=>{if(u.speaker!==cur){cur=u.speaker;blocks.push({speaker:cur,lines:[]})}
    blocks[blocks.length-1].lines.push(u)});
  const hint=anyAI?'<div class="hintbar">✨ Speakers identified by AI — click any name to correct it (change one block, add a new name, or rename that person everywhere).</div>'
    :'<div class="hintbar">Click a speaker name to rename it, or use 🧠 Identify speakers (AI) to attribute each person automatically.</div>';
  pane.className='tx';
  pane.innerHTML=hint+blocks.map(b=>{
    const opts=names.map(n=>`<option${n===b.speaker?' selected':''}>${esc(n)}</option>`).join('');
    const ai=b.lines.some(l=>l.ai);
    return `<div class="block"><div class="spkrow">
      <select class="spksel" data-cur="${esc(b.speaker)}" data-lines="${b.lines.map(l=>l.i).join(',')}">
        ${opts}<option value="__new">➕ New name…</option><option value="__rename">✎ Rename everywhere…</option>
      </select>${ai?'<span class="aitag">AI</span>':''}</div>`+
      b.lines.map(l=>`<div class="seg"><span class="ts">[${l.t}]</span>${esc(l.text)}</div>`).join('')+
      `</div>`;
  }).join('');
  pane.querySelectorAll('.spksel').forEach(sel=>{sel.onchange=async()=>{
    const cur=sel.dataset.cur, v=sel.value;
    if(v==='__rename'){const nn=prompt('Rename "'+cur+'" everywhere to:',cur);
      if(nn&&nn!==cur){await fetch('/api/meeting/'+id+'/rename-speaker',{method:'POST',
        body:JSON.stringify({old:cur,new:nn})})}}
    else{let name=v; if(v==='__new'){name=prompt('New speaker name:'); if(!name){renderTranscript(id,pane);return}}
      for(const ln of sel.dataset.lines.split(',')){await fetch('/api/meeting/'+id+'/set-speaker',
        {method:'POST',body:JSON.stringify({line:+ln,name})})}}
    renderTranscript(id,pane); loadList();
  }});
}
async function doIdentify(){
  const who=prompt('Who was on this call? Comma-separated names (optional — but greatly improves splitting & naming, e.g. "Me, Kevin"):','');
  if(who===null)return;
  const id=cur; tab='transcript'; await openMeeting(id);
  const pane=$('#pane');
  const banner=document.createElement('div'); banner.className='ident-banner';
  banner.innerHTML='<span class="spin"></span> Claude is identifying who each speaker is… (~1–2 min for long meetings; safe to come back later)';
  pane.prepend(banner);
  await fetch('/api/meeting/'+id+'/identify',{method:'POST',body:JSON.stringify({who})});
  const poll=async()=>{if(cur!==id)return;
    let s; try{s=await api('/api/meeting/'+id+'/identify-status')}catch(e){setTimeout(poll,3000);return}
    if(s.error){banner.innerHTML='⚠ '+esc(s.error);return}
    if(!s.running && s.done){await renderTranscript(id,pane);loadList();return}
    setTimeout(poll,3000);
  }; poll();
}
async function doAnalyze(){
  tab='summary'; const id=cur;
  const pane=$('#pane'); pane.className='md';
  pane.innerHTML='<div class="empty"><span class="spin"></span> Claude is analyzing the transcript…'+
    '<div class="meta" style="margin-top:8px">Long meetings take a few minutes. Safe to leave this open or come back later.</div></div>';
  await fetch('/api/meeting/'+id+'/analyze',{method:'POST'});   // starts background job
  const poll=async()=>{
    if(cur!==id) return;                                        // user switched away
    let s; try{ s=await api('/api/meeting/'+id+'/analysis-status') }catch(e){ setTimeout(poll,3000); return }
    if(s.error){ pane.innerHTML='<div class="empty">⚠ '+esc(s.error)+'</div>'; return }
    if(!s.running && s.analysis){
      window._analysis=s.analysis; pane.innerHTML=mdToHtml(s.analysis);
      loadList(); const b=$('#sum'); if(b)b.textContent='✨ Re-analyze'; return;
    }
    setTimeout(poll,3000);
  };
  poll();
}

$('#startBtn').onclick=async()=>{const t=prompt('Meeting title:',
  'Meeting '+new Date().toISOString().slice(0,16).replace('T',' '));
  if(t===null)return; $('#startBtn').innerHTML='<span class="spin"></span> Starting…';
  const r=await api('/api/start',{method:'POST',body:JSON.stringify({title:t})});
  $('#startBtn').innerHTML='● Start recording';
  if(r.error)alert(r.error); refreshStatus()};
$('#stopBtn').onclick=async()=>{$('#stopBtn').innerHTML='<span class="spin"></span> Saving…';
  const r=await api('/api/stop',{method:'POST'});
  $('#stopBtn').innerHTML='■ Stop &amp; save'; refreshStatus(); loadList();
  if(r.id){cur=r.id; openMeeting(r.id)} if(r.error)alert(r.error)};
$('#search').oninput=loadList;
loadList(); refreshStatus(); setInterval(refreshStatus,4000);
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # quiet
        pass

    def _send(self, code=200, body=b"", ctype="application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj).encode(), "application/json")

    def _text(self, s, code=200):
        self._send(code, s.encode(), "text/plain; charset=utf-8")

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length", 0) or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return {}

    def _speaker_slots(self, meeting_id: str) -> list[dict]:
        """One entry per distinct speaker in the transcript, in first-appearance
        order: {num, raw, label}. Drives the click-only labeler."""
        import re
        try:
            raw = core.export_transcript(meeting_id, apply_labels=False)
        except VerbatimError:
            return []
        labels = core.speaker_labels(meeting_id)
        seen, slots = set(), []
        for tag in re.findall(r"^###\s+(.+?)\s*$", raw, re.M):
            num = core._speaker_num(tag)
            key = num if num is not None else tag
            if key in seen:
                continue
            seen.add(key)
            slots.append({"num": num, "raw": tag,
                          "label": labels.get(num, "") if num is not None else ""})
        return slots

    def _speakers(self, meeting_id: str) -> list[str]:
        """Speaker chips: assigned name if present, else the raw tag."""
        return [s["label"] or s["raw"] for s in self._speaker_slots(meeting_id)]

    # ── routing ──
    def do_GET(self):
        u = urlparse(self.path)
        p = u.path
        try:
            if p in ("/", "/index.html"):
                return self._send(200, INDEX.encode(), "text/html; charset=utf-8")
            if p == "/api/status":
                mid = core.is_recording()
                title = None
                if mid:
                    m = core.get_meeting(mid) or core.get_meeting("latest")
                    title = m.title if m else None
                return self._json({"recording": mid, "title": title})
            if p == "/api/meetings":
                q = parse_qs(u.query).get("q", [""])[0]
                out = []
                for m in core.list_meetings(query=q or None):
                    d = m.to_dict()
                    d["analyzed"] = core.analysis_cache_path(m.id).exists()
                    out.append(d)
                return self._json(out)
            if p == "/api/search":
                q = parse_qs(u.query).get("q", [""])[0]
                return self._json(core.search_meetings(q))
            if p.startswith("/api/meeting/"):
                parts = p.split("/")
                mid = parts[3]
                if len(parts) == 4:
                    m = core.get_meeting(mid)
                    if not m:
                        return self._json({"error": "not found"}, 404)
                    d = m.to_dict()
                    d["speaker_slots"] = self._speaker_slots(m.id)
                    d["speakers"] = [s["label"] or s["raw"] for s in d["speaker_slots"]]
                    d["analysis"] = core.get_cached_analysis(m.id) or ""
                    d["analyzed"] = bool(d["analysis"])
                    d["stats"] = core.speaker_stats(m.id)
                    return self._json(d)
                if parts[4] == "transcript":
                    return self._text(core.export_transcript(mid))
                if parts[4] == "analysis":
                    return self._text(core.get_cached_analysis(mid) or "")
                if parts[4] == "summary-md":
                    try:
                        md = core.summary_markdown(mid)
                    except VerbatimError as e:
                        return self._text(str(e), 400)
                    return self._send(200, md.encode(),
                                      "text/markdown; charset=utf-8")
                if parts[4] == "analysis-status":
                    m = core.get_meeting(mid)
                    if not m:
                        return self._json({"error": "not found"}, 404)
                    job = _job_status(m.id, "analysis")
                    text = core.get_cached_analysis(m.id) or ""
                    return self._json({"running": job["running"], "error": job["error"],
                                       "analysis": text,
                                       "done": bool(text) and not job["running"]})
                if parts[4] == "utterances":
                    return self._json(core.resolved_utterances(mid))
                if parts[4] == "identify-status":
                    m = core.get_meeting(mid)
                    if not m:
                        return self._json({"error": "not found"}, 404)
                    job = _job_status(m.id, "identify")
                    has = bool(core.get_attribution(m.id).get("lines"))
                    return self._json({"running": job["running"], "error": job["error"],
                                       "done": has and not job["running"]})
            return self._json({"error": "not found"}, 404)
        except VerbatimError as e:
            return self._json({"error": str(e)}, 500)

    def do_POST(self):
        p = urlparse(self.path).path
        b = self._body()
        try:
            if p == "/api/start":
                mid = core.start_meeting(b.get("title") or None,
                                        diarization="ml" if b.get("ml") else None)
                return self._json({"recording": mid})
            if p == "/api/stop":
                if core.is_recording():
                    core.stop_meeting()
                m = core.get_meeting("latest")
                if m:
                    core.build_note(m.id, engine="none")  # fast finalize
                return self._json({"id": m.id if m else None})
            if p.startswith("/api/meeting/"):
                parts = p.split("/")
                mid, action = parts[3], parts[4]
                if action == "analyze":  # async: start a background job, poll status
                    m = core.get_meeting(mid)
                    if not m:
                        return self._json({"error": "not found"}, 404)
                    _start_job(m.id, "analysis",
                               lambda i: core.analyze(i, refresh=True))
                    return self._json({"status": "started"})
                if action == "identify":  # async speaker attribution via Claude
                    m = core.get_meeting(mid)
                    if not m:
                        return self._json({"error": "not found"}, 404)
                    roster = (b.get("who") or "").strip() or None
                    _start_job(m.id, "identify",
                               lambda i: core.identify_speakers(i, refresh=True,
                                                                roster=roster))
                    return self._json({"status": "started"})
                if action == "set-speaker":
                    core.set_line_speaker(mid, int(b.get("line")), b.get("name", ""))
                    return self._json({"ok": True})
                if action == "rename-speaker":
                    core.rename_speaker(mid, b.get("old", ""), b.get("new", ""))
                    return self._json({"ok": True})
                if action == "summary":  # legacy alias (blocking)
                    return self._text(core.analyze(mid))
                if action == "note":
                    path, _ = core.build_note(mid)  # engine=claude (uses cache)
                    return self._json({"path": str(path)})
                if action == "rename":
                    core.rename_meeting(mid, b.get("title", ""))
                    return self._json({"ok": True})
                if action == "label":
                    core.label_speaker(mid, str(b.get("speaker")), b.get("name", ""))
                    return self._json({"ok": True})
                if action == "delete":
                    m = core.get_meeting(mid)
                    if m:
                        core.analysis_cache_path(m.id).unlink(missing_ok=True)
                        core.attribution_path(m.id).unlink(missing_ok=True)
                    core.delete_meeting(mid)
                    return self._json({"ok": True})
            return self._json({"error": "not found"}, 404)
        except VerbatimError as e:
            return self._json({"error": str(e)}, 500)


def serve(port: int = 8777, open_browser: bool = True) -> None:
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{port}/"
    print(f"Verbatim GUI → {url}  (Ctrl-C to stop)")
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
        srv.shutdown()
