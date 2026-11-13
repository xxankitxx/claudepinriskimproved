// ---- ALERT SOUNDS (VAJRA 2.0.2, frozen "sound council" contract) ------------------------------------
// The ENGINE decides when a sound is due; this page only plays it. Cues: entry / exit_profit / exit_loss /
// approach (up / down) / feed_alarm / armed (09:20 sound check). Sister/stock breaches NEVER make a sound.
// Rate limits + hysteresis live in vajra/alerts.py. ONLY real MP3 files play -- no browser tones, no
// Python/Windows sounds. Sound is ON by default and stays ON until the operator turns it OFF (and OFF stays
// OFF until he turns it ON). A locked page shows a red banner + tab title and keeps missed trade cues for the
// unlock; a higher-priority cue cuts a lower one; each cue's length decides when the next may start; files are
// re-fetched when they change (sounds_rev); a broken file shows a red banner; only one tab plays; every cue's
// outcome is reported to the engine (which warns on its console when no page can play).
//
// DOM contract (index.html must provide): #sndbtn (the pill), #sndbanner (fixed red banner).
// Globals used by data.js: consumeSounds, _sndPill, _sndHealth, _sndState, _outcomes, _locked, _muted,
// _isLeader, _ctx, _sndSeq, _sndBoot, _sndRevSeen, playKind, _beaconNow (set by data.js).
const SND={entry:'entry.mp3',exit_profit:'exit_profit.mp3',exit_loss:'exit_loss.mp3',
           approach_UP:'approach_up.mp3',approach_DOWN:'approach_dn.mp3',feed_alarm:'feed_alarm.mp3',armed:'armed.mp3'};
const PRIO={exit_loss:5,exit_profit:5,entry:4,feed_alarm:3,approach_UP:2,approach_DOWN:2,armed:1,test:1};
const TRADE_KINDS={entry:1,exit_profit:1,exit_loss:1,feed_alarm:1};
function _ls(k,v){try{ if(v===undefined) return localStorage.getItem(k); localStorage.setItem(k,v);}catch(e){return null;}}
// ON by default; OFF only when the operator turns it off, and it stays off until he turns it back on.
let _sndOff=_ls('vajra_snd_off')==='1';
const _muted=()=>_sndOff;
let _ctx=null,_buf={},_bad={},_q=[],_cur=null,_sndSeq=null,_sndBoot=null,_lastSndText='',_sndRev=null,
    _clkOff=0,_pendLocked=[],_outcomes=[],_wasLocked=null,_lockedAtDown=false,_fbSeqs=new Set();
let _beaconNow=()=>{};
const _tabId=Math.random().toString(36).slice(2,10);
function _audioCtx(){
  if(_ctx && _ctx.state!=='closed') return _ctx;
  const C=window.AudioContext||window.webkitAudioContext; if(!C) return null;
  try{ _ctx=new C(); }catch(e){ _ctx=null; return null; }
  _buf={}; _loadSounds(_sndRev||'0');
  _ctx.onstatechange=()=>{ _sndPill(); if(_ctx&&_ctx.state==='running') _flushLocked(); };
  return _ctx; }
function _loadSounds(rev){ const c=_ctx; if(!c) return;
  Object.entries(SND).forEach(([k,f])=>{
    fetch('/sounds/'+f+'?v='+rev,{cache:'no-store'})
      .then(r=>{ if(!r.ok) throw new Error('HTTP '+r.status); return r.arrayBuffer(); })
      .then(b=>c.decodeAudioData(b))
      .then(d=>{ if(c===_ctx){ _buf[k]=d; delete _bad[k]; } _sndPill(); })
      .catch(e=>{ _bad[k]=f+' ('+(e&&e.message||'decode failed')+')'; _sndPill(); }); }); }
function _sndRevSeen(rev){ if(!rev||rev===_sndRev) return; _sndRev=rev; if(_ctx) _loadSounds(rev); }   // files changed -> re-decode
function _locked(){ return !_ctx || _ctx.state!=='running'; }
function _unlock(){ const c=_audioCtx(); if(c&&c.state==='suspended') c.resume().then(()=>{_sndPill(); _flushLocked();}).catch(()=>{}); }
// leader election: only one VAJRA tab plays (localStorage lease, 5 s; an unlocked tab takes over from a locked one)
function _leader(){ let L=null; try{ L=JSON.parse(_ls('vajra_snd_leader')||'null'); }catch(e){}
  const now=Date.now(), me=!L||L.id===_tabId||now-L.t>5000||(L.locked&&!_locked());
  if(me) _ls('vajra_snd_leader',JSON.stringify({id:_tabId,t:now,locked:_locked()}));
  return me || _ls('vajra_snd_leader')===null; }
let _isLeader=_leader();
function _outcome(k,seq,o){ _outcomes.push([Date.now(),k,seq,o]); if(_outcomes.length>60) _outcomes.shift();
  if(o==='locked'||o==='error') _beaconNow(); }
function _stopCur(){ if(!_cur) return; const c=_cur; _cur=null; clearTimeout(c.timer);
  try{ if(c.gain&&_ctx){ c.gain.gain.setTargetAtTime(0,_ctx.currentTime,0.03); } if(c.src) c.src.stop(_ctx.currentTime+0.12); }catch(e){} }
function _start(it){
  const c=_ctx, b=_buf[it.k]; let dur=0, src=null, gain=null;
  if(b){ src=c.createBufferSource(); gain=c.createGain(); gain.gain.value=0.9; src.buffer=b; src.connect(gain).connect(c.destination);
         src.start(); dur=b.duration; }
  else { _outcome(it.k,it.seq,'file'); setTimeout(_next,0); return; }   // only real MP3s ever play
  const fin=()=>{ if(_cur!==cur) return; _cur=null; clearTimeout(cur.timer); setTimeout(_next,120); };
  const cur={k:it.k,prio:PRIO[it.k]||1,src,gain,timer:setTimeout(fin,(dur+0.5)*1000)};
  if(src) src.onended=fin;
  _cur=cur; if(it.seq!=null) _outcome(it.k,it.seq,'played'); }
function _next(){ if(_cur||!_q.length) return; if(_locked()) return;
  _q.sort((a,b)=>(PRIO[b.k]||1)-(PRIO[a.k]||1)||a.t-b.t); _start(_q.shift()); }
function playKind(k, seq, force){
  if(!force){
    if(_muted()){ _outcome(k,seq,'muted'); return; }
    if(!_isLeader){ _outcome(k,seq,'other_tab'); return; }
  }
  if(_locked()){ _unlock();
    if(_locked()){ if(TRADE_KINDS[k]){ _pendLocked.push({k,seq,t:Date.now()}); if(_pendLocked.length>6) _pendLocked.shift(); }
                   _outcome(k,seq,'locked'); _sndPill(); return; } }
  const p=PRIO[k]||1, it={k,seq,t:Date.now()};
  if(k.startsWith('exit')) _q=_q.filter(x=>{ if(x.k==='entry'){ _outcome(x.k,x.seq,'superseded'); return false; } return true; });  // never an entry after its exit
  if(_cur){
    if(p>_cur.prio){ _stopCur(); _start(it); return; }          // exit cuts entry; entry cuts approach
    if(p<=2 && _cur.prio>=4){ _outcome(k,seq,'busy'); return; }   // no approach tick on top of a trade cue
    _q.push(it); if(_q.length>6){ _q.sort((a,b)=>(PRIO[b.k]||1)-(PRIO[a.k]||1)); const d=_q.pop(); _outcome(d.k,d.seq,'dropped'); }
    return; }
  _start(it); }
function _flushLocked(){ if(_locked()||!_pendLocked.length) return;
  const now=Date.now(); let p=_pendLocked.filter(x=>now-x.t<=120000); _pendLocked=[];
  const li=p.map(x=>x.k.startsWith('exit')).lastIndexOf(true);
  if(li>=0) p=p.filter((x,i)=>!(x.k==='entry'&&i<li));      // a whole trade happened: the exit tells it
  p.filter(x=>!_fbSeqs.has(x.seq)).forEach(x=>playKind(x.k,x.seq,true)); }   // Windows already played it -> skip
function _sndState(){ if(_muted()) return 'muted'; if(_locked()) return 'locked'; if(Object.keys(_bad).length) return 'bad'; return 'on'; }
function _sndPill(){ const b=document.getElementById('sndbtn'), st=_sndState();
  if(b){
    b.textContent={on:'🔔 sound ON',muted:'🔇 SOUND OFF — click to turn on',locked:'🔒 SOUND LOCKED — click',bad:'⚠ sound file broken'}[st]+(_isLeader?'':' (other tab)');
    b.title='Click: turn sound OFF / ON (it stays as you set it). If LOCKED, a click only unlocks sound and plays the 09:20 check sound (armed.mp3). Shift-click: hear every cue.'+
            (Object.keys(_bad).length?(' Broken: '+Object.values(_bad).join(', ')+' — that cue cannot play.'):'')+
            (_lastSndText?(' Last: '+_lastSndText):'');
    b.className='pill '+(st==='on'?'snd-on':(st==='muted'?'snd-muted':'snd-bad')); }
  const bn=document.getElementById('sndbanner');
  if(bn){ const show=(st==='locked'&&_isLeader)||st==='bad';
    bn.style.display=show?'block':'none';
    bn.textContent=st==='locked'?'🔒 SOUND IS LOCKED — click anywhere on this page to turn trade sounds on'+(_pendLocked.length?(' ('+_pendLocked.length+' missed cue'+(_pendLocked.length>1?'s':'')+' will play)'):''):
                   ('⚠ Sound file broken: '+Object.values(_bad).join(', ')+' — that cue CANNOT play. Check vajra/sounds/.'); }
  const base='VAJRA', want=st==='locked'?'🔒 SOUND LOCKED · '+base:(st==='muted'?'🔇 SOUND OFF · '+base:(st==='bad'?'⚠ SOUND · '+base:base));
  if(document.title!==want) document.title=want;
  if(_wasLocked!==null && _wasLocked!==_locked()) _beaconNow();
  _wasLocked=_locked(); }
(function(){const b=document.getElementById('sndbtn'); if(b) b.onclick=function(ev){
  ev.stopPropagation();
  if(_lockedAtDown||_locked()){ _lockedAtDown=false; _unlock(); setTimeout(()=>{ if(!_locked()) playKind('armed',null,true); _sndPill(); },200); return; }   // never changes the mode
  if(ev.shiftKey){ ['entry','approach_UP','approach_DOWN','exit_profit','exit_loss','feed_alarm','armed'].forEach((k,i)=>setTimeout(()=>playKind(k,null,true),i*3000)); return; }
  if(_sndOff){ _sndOff=false; _ls('vajra_snd_off','0'); setTimeout(()=>playKind('armed',null,true),150); }
  else { _sndOff=true; _ls('vajra_snd_off','1'); _stopCur(); _q=[]; }
  _sndPill(); _beaconNow(); };
// any click / key anywhere unlocks audio (browser rule: sound needs one user gesture per page load)
['pointerdown','keydown','touchstart'].forEach(evn=>document.addEventListener(evn,()=>{ _lockedAtDown=_locked(); _unlock(); },true));
_audioCtx();
// keep trying: a Chrome window started with --autoplay-policy=no-user-gesture-required resumes at once
setInterval(()=>{ const c=_audioCtx(); if(c&&c.state==='suspended') c.resume().catch(()=>{}); _isLeader=_leader(); _flushLocked(); _sndPill(); }, 2000);
})();
// play every engine sound event exactly once (seq cursor), judged on the BROWSER clock (server offset).
// Trade cues are kept for 120 s (if a whole trade is late, only its exit plays); approach / feed / armed
// expire after 20 s. On first load the last 15 s are played, older history is not.
function consumeSounds(s){
  if(s.server_ms) _clkOff=Date.now()-s.server_ms;
  if(s.sounds_rev) _sndRevSeen(s.sounds_rev);
  if(s.snd_fallback) _fbSeqs=new Set(s.snd_fallback);
  if(s.boot_id!==undefined && _sndBoot!==null && s.boot_id!==_sndBoot) _sndSeq=0;
  if(s.boot_id!==undefined) _sndBoot=s.boot_id;
  const ev=s.sound_events||[]; if(!ev.length){ if(_sndSeq===null) _sndSeq=0; return; }
  const maxSeq=ev[ev.length-1].seq, nowSrv=Date.now()-_clkOff;
  if(_sndSeq===null){ const rec=ev.filter(e=>nowSrv-e.ts<=15000);      // first load: only the newest cue of
    _sndSeq=rec.length?rec[rec.length-1].seq-1:maxSeq; }                 // the last 15 s, never a burst
  if(maxSeq<_sndSeq) _sndSeq=0;
  let fresh=ev.filter(e=>e.seq>_sndSeq).map(e=>{ let k=e.kind; if(k==='approach') k=(/upper/i.test(e.text||'')?'approach_UP':'approach_DOWN');
    return {k,seq:e.seq,age:nowSrv-e.ts,text:e.text}; });
  const li=fresh.map(x=>x.k.startsWith('exit')).lastIndexOf(true);
  fresh.forEach((x,i)=>{
    _lastSndText=new Date(Date.now()-x.age).toLocaleTimeString()+' '+x.k+' — '+(x.text||'');
    const lim=TRADE_KINDS[x.k]?120000:20000;
    if(x.age>lim){ _outcome(x.k,x.seq,'stale'); return; }
    if(x.k==='entry' && i<li && x.age>20000){ _outcome(x.k,x.seq,'stale'); return; }
    playKind(x.k,x.seq); });
  _sndSeq=maxSeq; _sndPill(); }
function _sndHealth(){ return {on:!_muted()&&!_locked(), muted:_muted(), locked:_locked(),
  leader:_isLeader, ctx:_ctx?_ctx.state:'none', bad:Object.keys(_bad), loaded:Object.keys(_buf), rev:_sndRev}; }
