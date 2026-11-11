// ==== VAJRA 2.1 DATA PATH (frozen) ===================================================================
// Server PUSH (SSE) with coalesced repaint; polling only as a fallback; feed watchdog; lag pill; the 10-s
// client beacon. Presentation lives in ui.js: window.VAJRA_UI.render(state, heavy) and
// window.VAJRA_UI.watchdog({kind,text,detail,lastRoundTs}). Requires sound.js loaded first.
//
// 1.x made the browser fetch the WHOLE state (20-60 KB) every 250 ms and repaint ~15 tables; repaints fell
// behind, background tabs were throttled to 1/s (then 1/min), and one-round events (sounds) were missed.
// The server pushes every engine round over SSE: event "hot" (small live part) every round, "full" (whole
// state) at most once per second, "beat" keep-alive every 2 s when no new round.
const _UI=()=>window.VAJRA_UI||{render:()=>{},watchdog:()=>{}};
const hhmmss=ms=>ms?new Date(ms).toLocaleTimeString('en-GB'):'—';

// ---- FEED WATCHDOG — a dead engine or stalled feed must be UNMISSABLE, never a frozen-but-green screen ----
let _wdLastOk=0,_wdLastRts=null,_wdLastRtsChange=0,_wdKind=null;
let _closeHM=15*60+40;   // F&O close from the engine (regime.closing.session_close); 15:40 under CAS
const _hmOf=t=>{const x=String(t||'').split(':');return x.length>=2?(+x[0])*60+(+x[1]):null;};
const todHM=ms=>{const d=new Date(ms);return d.getHours()*60+d.getMinutes();};
function applyWatchdog(fetchFailed){
  // Judged by SERVER round-time, not the wall clock — an evening tape REPLAY has round_ts advancing
  // and must render normally, never masked by a false SESSION CLOSED.
  const now=Date.now();
  const noFetch=fetchFailed||(_wdLastOk>0&&now-_wdLastOk>5000);
  const stalled=(!noFetch&&_wdLastRtsChange>0&&(now-_wdLastRtsChange>5000));
  let st;
  if(!noFetch&&!stalled){ st={kind:'ok',text:'',detail:'',lastRoundTs:_wdLastRts}; }
  else {
    // data has stopped — decide CLOSED (expected, calm) vs FROZEN (a real problem, loud)
    const endOfDay=_wdLastRts!=null&&todHM(_wdLastRts)>=_closeHM-1;   // last round reached the F&O close
    if(endOfDay) st={kind:'closed',text:'SESSION CLOSED — last round '+hhmmss(_wdLastRts)+' · tapes closed',detail:'',lastRoundTs:_wdLastRts};
    else if(noFetch) st={kind:'frozen',text:'DATA FROZEN — no response from engine · last update '+(_wdLastOk?hhmmss(_wdLastOk):'never'),detail:'check the engine terminal',lastRoundTs:_wdLastRts};
    else st={kind:'frozen',text:'DATA FROZEN — engine not advancing · last round '+hhmmss(_wdLastRts),detail:'feed stalled or engine hung',lastRoundTs:_wdLastRts};
  }
  document.body.classList.toggle('frozen', st.kind==='frozen');
  const key=st.kind+'|'+st.text;
  if(key!==_wdKind){ _wdKind=key; try{ _UI().watchdog(st); }catch(e){ console.error('watchdog', e); } }
  return st.kind!=='ok';
}

// ---- transport ----------------------------------------------------------------------------------------
let _full={}, _pending=null, _raf=false, _lastHeavy=0, _lagMs=null, _es=null, _sseFails=0, _pollTimer=null, _lastMsgAt=0, _heavyDue=false;
function _onState(s, isFull){
  _wdLastOk=Date.now();
  if(s.server_ms) _lagMs=Date.now()-s.server_ms;
  if(s.round_ts!==_wdLastRts){ _wdLastRts=s.round_ts; _wdLastRtsChange=Date.now(); }
  // 2.2: count RECEIVED rounds here (the beacon used to count only painted ones, so a hidden window --
  // 97% of 11 Nov -- reported 0 rounds while it received and played everything)
  if(s.round_ts && s.round_ts!==_cb.lastRecvRts){ _cb.recvRounds++; _cb.lastRecvRts=s.round_ts; }
  _lastMsgAt=Date.now(); if(isFull) _heavyDue=true;
  if(isFull) _full=s; else _full=Object.assign({}, _full, s);
  const _cl=(_full.regime||{}).closing||{}; const _chm=_hmOf(_cl.session_close); if(_chm) _closeHM=_chm;
  consumeSounds(_full);                       // sounds NEVER wait for a repaint (works in background tabs)
  // 2.0.3: on 21 Oct the audio was suspended for 3.5 min while the window was hidden; retry from the
  // stream (not a throttled timer), and keep the beacon flowing even when the window is minimised.
  try{ if(_ctx && _ctx.state==='suspended') _ctx.resume().catch(()=>{}); }catch(e){}
  if(Date.now()-_lastBeaconAt>=10000 && typeof _sendBeacon==='function') _sendBeacon();
  _pending=_full;
  _schedulePaint();
}
// 2.1.1: a HIDDEN window cannot show anything -- Chrome pauses rAF and clamps timers to 1/s (1/min after
// 5 min), which is where the "1 round per second, 1 s old" readings of the 3 Nov tape came from (the page
// was hidden 70% of the day). Hidden: do not schedule (sounds + beacon already ran above). Visible: rAF.
// On reveal the newest state is painted at once (see the visibilitychange handler below).
function _schedulePaint(){
  if(document.hidden) return;
  if(!_raf){ _raf=true; requestAnimationFrame(_paint);
             setTimeout(()=>{ if(_raf) _paint(); }, 400); }   // belt and braces if rAF stalls
}
document.addEventListener('visibilitychange', ()=>{
  if(!document.hidden){ _lastMsgAt=Date.now(); if(_full && _full.round_ts!=null){ _pending=_full; _heavyDue=true; _raf=true; _paint(); } }
});
try{ document.addEventListener('resume', ()=>{ _lastMsgAt=Date.now(); }); }catch(e){}
function render(s, heavy){
  window._lastState=s;
  try{ _UI().render(s, heavy); }catch(e){ console.error('render', e); }
  applyWatchdog(false);
  _sndPill();
}
function _paint(){
  if(!_raf||!_pending) { _raf=false; return; }
  _raf=false; const s=_pending; _pending=null;
  // heavy tables repaint when a FULL payload has arrived (in step with the 1 s full push), or after 2 s
  const now=Date.now(), heavy=_heavyDue||(now-_lastHeavy)>=2000; if(heavy){ _lastHeavy=now; _heavyDue=false; }
  const t0=performance.now();
  render(s, heavy);
  const paintMs=performance.now()-t0;
  // 2.1.1 pipeline gauges for the beacon: engine publish -> painted (same machine clock in live mode)
  if(s.round && s.round.published_ms && Math.abs(now-s.round.published_ms)<60000){
    const pl=now-s.round.published_ms; _cb.pubLagSum+=pl; _cb.pubLagN++; _cb.pubLagMax=Math.max(_cb.pubLagMax,pl); }
  _cb.paintSum+=paintMs; _cb.paintN++; _cb.paintMax=Math.max(_cb.paintMax,paintMs);
  const lp=document.getElementById('lagpill');
  if(lp){ let ms=_lagMs==null?null:Math.max(0,_lagMs);
    // engine round -> this browser (same machine clock in live mode); replay rounds are historic, skip
    if(s.round_ts && Math.abs(now-s.round_ts)<60000) ms=Math.max(ms||0, now-s.round_ts);
    lp.textContent=(_es?'live ':'poll ')+(ms==null?'—':ms+' ms')+' · paint '+Math.round(performance.now()-t0)+' ms';
    lp.className='pill '+(ms==null?'':(ms<500?'feed-ok':(ms<2000?'snd-muted':'feed-bad'))); }
}
function _startSSE(){
  if(!window.EventSource){ _startPoll(); return; }
  _es=new EventSource('/api/stream');
  _es.addEventListener('full', ev=>{ _sseFails=0; try{ _onState(JSON.parse(ev.data), true); }catch(e){} });
  _es.addEventListener('hot',  ev=>{ _sseFails=0; try{ _onState(JSON.parse(ev.data), false); }catch(e){} });
  _es.addEventListener('beat', ev=>{ _wdLastOk=Date.now(); _lastMsgAt=Date.now(); try{ const b=JSON.parse(ev.data); _lagMs=Date.now()-b.server_ms; _clkOff=Date.now()-b.server_ms; if(b.sounds_rev) _sndRevSeen(b.sounds_rev); }catch(e){}
                                     if(_full.round_ts) { _pending=_full; _schedulePaint(); } });
  // a non-200 / closed stream never retries by itself (readyState CLOSED) -> fall back at once (T05).
  // 2.1.1: a single transient error no longer flashes DATA FROZEN (EventSource retries within 1 s).
  _es.onerror=()=>{ _sseFails++; if(_sseFails>=5 || (_es && _es.readyState===2)){ try{_es.close();}catch(e){} _es=null; _startPoll(); }
                    else if(Date.now()-_wdLastOk>5000) applyWatchdog(true); };
}
function _startPoll(){                         // fallback: old behaviour, but guarded
  if(_pollTimer) return; let busy=false; _cb.pollMode++;
  const tick=async()=>{ if(!_pollTimer) return;
    if(!busy){ busy=true;
      try{ const s=await (await fetch('/api/state')).json(); _onState(s, true); }
      catch(e){ applyWatchdog(true); _sndPill(); }
      finally{ busy=false; } }
    if(_pollTimer) _pollTimer=setTimeout(tick, 500); };     // self-rescheduling: never piles up
  _pollTimer=setTimeout(tick, 0);
  setTimeout(()=>{ if(!_es){ clearTimeout(_pollTimer); _pollTimer=null; _sseFails=0; _cb.sseReconnects++; _startSSE(); } }, 5000);  // try push again soon
}
// ---- 2.0.1 client beacon: what THIS browser painted and played, every 10 s -> client_tape.jsonl ----
const _cb={paints:0, rounds:0, lastRts:null, recvRounds:0, lastRecvRts:null, sounds:[], sseReconnects:0, pollMode:0, maxLag:0, lagSum:0, lagN:0, boot:Date.now(),
           pubLagSum:0, pubLagN:0, pubLagMax:0, paintSum:0, paintN:0, paintMax:0};
(function(){ const orr=render; render=function(s,h){ _cb.paints++; if(s&&s.round_ts&&s.round_ts!==_cb.lastRts){ _cb.rounds++; _cb.lastRts=s.round_ts; }
  if(_lagMs!=null){ _cb.maxLag=Math.max(_cb.maxLag,_lagMs); _cb.lagSum+=_lagMs; _cb.lagN++; } return orr(s,h); };
  const op=playKind; playKind=function(k,q,f){ if(!f) _cb.sounds.push([Date.now(),k]); return op(k,q,f); }; })();
_beaconNow=function(){ try{ fetch('/api/client',{method:'POST',headers:{'Content-Type':'application/json'},
  body:JSON.stringify({now:true, snd:_sndHealth(), snd_outcomes:_outcomes.slice(-20), boot_id:_sndBoot}),keepalive:true}).catch(()=>{}); }catch(e){} };
setTimeout(()=>_beaconNow(), 1500);           // tell the engine at once that a page is listening
// 2.0.3 (21 Oct tape): a hidden/minimised window gets its timers throttled to once a minute by Chrome,
// so the beacon is ALSO driven by the data stream (which is never throttled): at most one per 10 s.
let _lastBeaconAt=0;
function _sendBeacon(){ _lastBeaconAt=Date.now(); const b={paints:_cb.paints, rounds:_cb.rounds, last_round_ts:_cb.lastRts,
    recv_rounds:_cb.recvRounds, last_recv_round_ts:_cb.lastRecvRts, sounds:_cb.sounds.splice(0),
    mode:(_es?'sse':'poll'), snd_mode:_sndState(), snd:_sndHealth(), snd_outcomes:_outcomes.splice(0), locked:_locked(), hidden:document.hidden, max_lag_ms:Math.round(_cb.maxLag),
    avg_lag_ms:_cb.lagN?Math.round(_cb.lagSum/_cb.lagN):null, snd_seq:_sndSeq, boot_id:_sndBoot, page_up_s:Math.round((Date.now()-_cb.boot)/1000),
    // 2.1.1: engine publish -> painted, and paint cost (so tomorrow's tape proves each stage)
    pub_lag_avg_ms:_cb.pubLagN?Math.round(_cb.pubLagSum/_cb.pubLagN):null, pub_lag_max_ms:Math.round(_cb.pubLagMax),
    paint_avg_ms:_cb.paintN?Math.round(_cb.paintSum/_cb.paintN*10)/10:null, paint_max_ms:Math.round(_cb.paintMax*10)/10,
    sse_reconnects:_cb.sseReconnects, poll_mode:_cb.pollMode};
  _cb.paints=0; _cb.rounds=0; _cb.recvRounds=0; _cb.maxLag=0; _cb.lagSum=0; _cb.lagN=0;
  _cb.pubLagSum=0; _cb.pubLagN=0; _cb.pubLagMax=0; _cb.paintSum=0; _cb.paintN=0; _cb.paintMax=0;
  try{ fetch('/api/client',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b),keepalive:true}).catch(()=>{}); }catch(e){} }
setInterval(()=>{ if(Date.now()-_lastBeaconAt>=9500) _sendBeacon(); }, 10000);
_startSSE();
setInterval(()=>{ applyWatchdog(false); _sndPill();
  // stream open but silent > 6 s (beats come every 2 s) -> it is dead: switch to polling (T05).
  // 2.1.1: never while hidden (Chrome freezes the tab; every 3 Nov flip was a healthy stream judged
  // from a frozen timer), and only when the stream is really closed or silent while visible.
  if(_es && !document.hidden && _lastMsgAt && Date.now()-_lastMsgAt>6000){ try{_es.close();}catch(e){} _es=null; _startPoll(); }
  const lp=document.getElementById('lagpill');
  if(lp && _lastMsgAt && Date.now()-_lastMsgAt>3000){ lp.textContent='NO DATA '+Math.round((Date.now()-_lastMsgAt)/1000)+' s'; lp.className='pill feed-bad'; }
}, 1000);   // watchdog keeps judging even with no messages
