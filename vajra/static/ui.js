// ==== VAJRA 2.2 PRESENTATION LAYER ===================================================================
// window.VAJRA_UI.render(state, heavy)  — called by data.js on every coalesced repaint (~4x/s).
//                                        heavy===true at most ~once a second → big tables repaint then.
// window.VAJRA_UI.watchdog(status)     — {kind:'ok'|'closed'|'frozen', text, detail, lastRoundTs}
// Pure presentation. Contract: docs/DASHBOARD_STATE_CONTRACT.md. Never throws; every field may be missing.
// Times are IST = the machine clock (contract); HH:MM strings from the engine are placed on the round's local day.
(function () {
  'use strict';

  // ---------- DOM cache + change-gated writers ------------------------------------------------------
  const _el = Object.create(null), _txt = Object.create(null), _cls = Object.create(null), _html = Object.create(null),
        _sty = Object.create(null), _attr = Object.create(null);
  const $ = id => _el[id] || (_el[id] = document.getElementById(id));
  function txt(id, s) { s = (s == null ? '—' : String(s)); if (_txt[id] === s) return; const e = $(id); if (e) { e.textContent = s; _txt[id] = s; } }
  function cls(id, c) { if (_cls[id] === c) return; const e = $(id); if (e) { e.className = c + (e.classList.contains('hid') ? ' hid' : ''); _cls[id] = c; } }
  function html(id, h) { if (_html[id] === h) return; const e = $(id); if (e) { e.innerHTML = h; _html[id] = h; } }
  function show(id, on) { const e = $(id); if (e) e.classList.toggle('hid', !on); }
  function attr(id, k, v) { const key = id + '\0' + k; if (_attr[key] === v) return; const e = $(id); if (e) { if (v == null) e.removeAttribute(k); else e.setAttribute(k, v); _attr[key] = v; } }
  function style(id, k, v) { const key = id + '\0' + k; if (_sty[key] === v) return; const e = $(id); if (e) { e.style[k] = v; _sty[key] = v; } }

  // ---------- formatting (Intl objects cached: a heavy frame makes ~500 calls) ----------------------
  const NF = Object.create(null);
  const nf = d => NF[d] || (NF[d] = new Intl.NumberFormat('en-IN', { minimumFractionDigits: d, maximumFractionDigits: d }));
  const DTF = new Intl.DateTimeFormat('en-GB', { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false });
  const DMON = new Intl.DateTimeFormat('en-GB', { day: '2-digit', month: 'short' });
  const isNum = v => typeof v === 'number' && isFinite(v);
  const S = v => (v == null ? '' : (typeof v === 'string' ? v : String(v)));
  const ESC = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
  const esc = s => S(s).replace(/[&<>"']/g, c => ESC[c]);
  function n(v, d) { if (!isNum(v)) return '—'; return nf(d == null ? 2 : d).format(v); }
  function n0(v) { return n(v, 0); }
  function K(v) { return isNum(v) ? String(Math.round(v)) : '—'; }                       // strikes: never grouped
  function sg(v, d) { if (!isNum(v)) return '—'; return (v > 0 ? '+' : v < 0 ? '−' : '') + n(Math.abs(v), d == null ? 1 : d); }
  function inr(v, d) { if (!isNum(v)) return '—'; return (v < 0 ? '−' : '') + '₹' + n(Math.abs(v), d == null ? 0 : d); }
  function inrS(v) { if (!isNum(v)) return '—'; return (v > 0 ? '+' : v < 0 ? '−' : '') + '₹' + n(Math.abs(v), 0); }
  function pct(v, d) { if (!isNum(v)) return '—'; return (v > 0 ? '+' : v < 0 ? '−' : '') + n(Math.abs(v), d == null ? 2 : d) + '%'; }
  function hms(ms) { if (!isNum(ms) || ms <= 0) return '—'; try { return DTF.format(new Date(ms)); } catch (e) { return '—'; } }
  function dur(sec) {
    if (!isNum(sec)) return '—'; sec = Math.max(0, Math.round(sec));
    if (sec < 60) return sec + 's';
    const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
    if (h) return h + 'h ' + (m < 10 ? '0' : '') + m + 'm';
    return m + 'm ' + (s < 10 ? '0' : '') + s + 's';
  }
  function mmss(sec) {                                                                    // hold time: m:ss (h:mm:ss past an hour)
    if (!isNum(sec) || sec < 0) return '—'; sec = Math.round(sec);
    const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), x = sec % 60;
    return (h ? h + ':' + (m < 10 ? '0' : '') : '') + m + ':' + (x < 10 ? '0' : '') + x;
  }
  const secs = v => (v < 10 ? n(v, 1) : String(Math.round(v)));                           // "1.2" / "14"
  const trim = v => n(v, Math.abs(v - Math.round(v)) < 0.05 ? 0 : 1);                     // "6" / "2.5"
  function ago(now, ts) { if (!isNum(now) || !isNum(ts)) return ''; return dur((now - ts) / 1000) + ' ago'; }
  function atHM(ref, hhmm) { if (!isNum(ref) || !hhmm) return null; const p = S(hhmm).split(':'); if (p.length < 2) return null; const d = new Date(ref); d.setHours(+p[0], +p[1], p[2] ? +p[2] : 0, 0); return d.getTime(); }
  function expLabel(iso) { if (!iso) return ''; const d = new Date(S(iso) + 'T00:00:00'); if (isNaN(d)) return S(iso); try { return DMON.format(d).replace(' ', '-').toUpperCase(); } catch (e) { return S(iso); } }
  const sgnCls = v => (isNum(v) ? (v > 0 ? 'up' : v < 0 ? 'dn' : '') : '');
  const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
  const get = (o, k, d) => (o && o[k] != null ? o[k] : d);
  const EXIT = Object.assign(Object.create(null), { TRAIL_SL: ['Trail SL', 'rs-trail'], SL_HIT: ['Hard SL −12%', 'rs-sl'], BE_STOP: ['Protective stop', 'rs-be'], EXPIRY_FLATTEN: ['Square-off', 'rs-flat'], END_OF_SESSION: ['End of session', 'rs-flat'] });
  function exitReason(r, s) { const m = EXIT[S(r)]; if (!m) return [S(r) || '—', '']; let t = m[0]; if (S(r) === 'EXPIRY_FLATTEN') { const fb = get(get(s.regime, 'closing', null), 'flatten_before', null); if (fb) t += ' ' + fb; } return [t, m[1]]; }

  // ---------- module state --------------------------------------------------------------------------
  let _wd = { kind: 'ok', text: '', detail: '' };
  let _last = null;
  const ring = { key: null, t: [], p: [], s: [], lastT: 0, dirty: false };   // premium-vs-stop ring buffer (one trade)
  const RING_MAX = 720, RING_STEP = 5000;                                         // 5-s buckets → an hour of trade
  const _errAt = Object.create(null);

  // ---------- derived helpers -----------------------------------------------------------------------
  function openRow(s) { const j = s.journal; if (!Array.isArray(j)) return null; for (let i = j.length - 1; i >= 0; i--) if (j[i] && j[i].status === 'OPEN') return j[i]; return null; }
  function entryTs(s) { const p = s.position; if (p && isNum(p.entry_ts)) return p.entry_ts; const o = openRow(s); return o && isNum(o.entry_ts) ? o.entry_ts : null; }
  function lastEntryTs(s) { const j = s.journal; if (!Array.isArray(j) || !j.length) return null; let t = null; for (const r of j) if (r && isNum(r.entry_ts) && (t == null || r.entry_ts > t)) t = r.entry_ts; return t; }
  function proxPts(s) { return get(s.arming, 'proximity_pts', get(s.idle, 'proximity_pts', 7)); }
  function inTrade(s) { return !!(s.position && s.position.open); }
  function feedFrozen(s) { const fh = s.feed_health; return !!(fh && fh.ok === false && isNum(s.round_ts) && get(s.nifty, 'spot_source', '') !== 'CLOSING_PROXY'); }
  // one distance source: nifty.dist_up / dist_dn (arming.dist_to_level only as a fallback)
  function dists(s) {
    const nf_ = s.nifty || {}, a = s.arming || {};
    let du = isNum(nf_.dist_up) ? nf_.dist_up : (isNum(nf_.upper) && isNum(nf_.spot) ? nf_.upper - nf_.spot : null);
    let dd = isNum(nf_.dist_dn) ? nf_.dist_dn : (isNum(nf_.lower) && isNum(nf_.spot) ? nf_.spot - nf_.lower : null);
    if (du == null && dd == null && isNum(a.dist_to_level)) { if (a.side === 'LOWER') dd = a.dist_to_level; else du = a.dist_to_level; }
    let side = null, d = null;
    if (isNum(du) && isNum(dd)) { side = du <= dd ? 'UPPER' : 'LOWER'; d = Math.min(du, dd); }
    else if (isNum(du)) { side = 'UPPER'; d = du; } else if (isNum(dd)) { side = 'LOWER'; d = dd; }
    return { du, dd, side, d, lvl: side === 'UPPER' ? nf_.upper : side === 'LOWER' ? nf_.lower : null };
  }
  // the BUY target is shown ONLY when nothing blocks the entry (belt and braces over the engine's own null)
  function buyAllowed(s) { const a = s.arming || {}, eb = a.entry_block; return !!(a.target && a.target.action) && (eb == null || eb === 'ENTERED') && !get(s.cooldown, 'stop_cooldown_active', false) && !inTrade(s); }
  function noTradeReason(s) { const a = s.arming || {}; const eb = a.entry_block; const wg = waitGate(s); if (wg) return waitWhy(s, wg);
    if (lagClosed(s) && !(eb && eb !== 'ENTERED') && !get(s.cooldown, 'stop_cooldown_active', false)) { const l = lagSecs(s); return 'NIFTY feed ' + (isNum(l) ? secs(l) + ' s ' : '') + 'late — entries paused'; } return (eb && eb !== 'ENTERED') ? S(eb) : (get(s.cooldown, 'stop_cooldown_active', false) ? 'post-stop cooldown ' + dur(get(s.cooldown, 'stop_cooldown_remaining_secs', null)) : S(get(s.idle, 'text', '')) || 'entry blocked'); }
  function nextStop(pos) {
    // the number the operator lives on: premium (₹) distance to whatever will take him out next.
    // kind: 'hard' (hard SL) → 'be' (2.2 protective stop: exit_plan.be_lock once be_armed) → 'trail' (trail floor once armed)
    if (!pos) return null;
    const ep = pos.exit_plan || {};
    const armed = !!pos.trail_armed && isNum(pos.trail_floor);
    const be = !armed && ep.be_armed === true && isNum(ep.be_lock);
    const kind = armed ? 'trail' : be ? 'be' : 'hard';
    const stop = armed ? pos.trail_floor : be ? ep.be_lock : ep.hard_sl;
    const dist = (isNum(pos.last_premium) && isNum(stop)) ? pos.last_premium - stop : null;
    let mode = armed ? 'TRAILING' : be ? 'PROTECTED' : 'ARMING';
    if (kind === 'hard' && isNum(dist) && isNum(pos.entry_fill) && pos.entry_fill > 0 && dist / pos.entry_fill <= 0.04) mode = 'HARD-STOP NEAR';
    return { stop, dist, mode, armed, kind, name: armed ? 'trail floor' : be ? 'protective stop' : 'hard SL' };
  }
  // ---- 2.2: exchange-feed lateness + the entry "wait" guards ------------------------------------------
  // lateness of the NIFTY tick stream vs the exchange clock (ws.exch_lag_s; feed_lag.lag_s as a fallback). null in replay.
  function lagSecs(s) { const w = s.ws, f = s.feed_lag; return w && isNum(w.exch_lag_s) ? Math.max(0, w.exch_lag_s) : (f && isNum(f.lag_s) ? Math.max(0, f.lag_s) : null); }
  function lagClosed(s) { return !!(s.feed_lag && s.feed_lag.closed === true); }
  const WAIT = Object.assign(Object.create(null), { ENTRY_RETREATING: 1, REBUY_WAIT: 1, BREACH_TOO_DEEP: 1,
    GAMMA_CONFIRM_WAIT: 1, GAMMA_TOO_TIGHT: 1, RETRY_NEEDS_BREAK: 1 });   // 2.4 gamma-aware entry waits
  function waitGate(s) { const g = get(s.idle, 'gate', null); return g && WAIT[g] ? g : null; }
  // the plain-words reason for a wait gate, without the "WAIT" prefix (the engine's ready sentence wins)
  function waitWhy(s, gate) {
    const eb = get(s.arming, 'entry_block', null), g = s.guards || {};
    if (typeof eb === 'string' && /^WAIT\b/i.test(eb)) { const t = eb.replace(/^WAIT\s*[-—–:]*\s*/i, ''); if (t) return t; }
    if (gate === 'REBUY_WAIT') { const rw = g.rebuy_wait; return 're-buy needs NIFTY ' + (rw && isNum(rw.beyond) ? (rw.dir === 'DOWN' ? 'below ' : rw.dir === 'UP' ? 'above ' : 'beyond ') + n0(rw.beyond) : "beyond the last trade's best price"); }
    if (gate === 'ENTRY_RETREATING') return 'NIFTY is moving away from the level';
    if (gate === 'GAMMA_CONFIRM_WAIT') return 'low gamma — waiting for NIFTY to trade through the level';
    if (gate === 'GAMMA_TOO_TIGHT') return 'the option\'s stop is only a few points away';
    if (gate === 'RETRY_NEEDS_BREAK') return 'the last attempt on this side failed — waiting for the level to break';
    if (gate === 'BREACH_TOO_DEEP') return 'NIFTY is already far through the level' + (isNum(g.max_breach_pts) && g.max_breach_pts > 0 ? ' (more than ' + trim(g.max_breach_pts) + ' pts)' : '');
    return 'entry is waiting';
  }
  const dirGlyph = d => (d === 'UP' ? '▲' : d === 'DOWN' ? '▼' : '');
  function posLabel(pos) { return dirGlyph(pos.direction) + ' ' + K(pos.strike) + ' ' + S(pos.right); }
  function ticket(s) { const p = s.position || {}; return 'BUY NIFTY ' + expLabel(get(s.expiry, 'date', '')) + ' ' + K(p.strike) + ' ' + S(p.right) + ' · ' + (isNum(p.lots) ? p.lots + ' lots' : '— lots') + ' (' + (isNum(p.qty_total) ? n0(p.qty_total) : n0(p.qty_remaining)) + ') @ ' + n(p.entry_fill); }
  const STC = Object.assign(Object.create(null), { APPROACH_UP: 'ap-up', APPROACH_DOWN: 'ap-dn', BREACH_UP: 'br-up', BREACH_DOWN: 'br-dn', CONFIRM_UP: 'cf-up', CONFIRM_DOWN: 'cf-dn', 'N/A': 'na' });
  const stCls = st => STC[S(st)] || '';
  const stTxt = st => (S(st) || '—').replace('APPROACH_', 'APP ').replace('BREACH_', 'BRK ').replace('CONFIRM_', 'CNF ');
  const isUp = d => d === 1 || d === 'UP', isDn = d => d === -1 || d === 'DOWN';

  // =================================================================================================
  // HOT RENDERERS (every call)
  // =================================================================================================
  function rHeader(s) {
    const cap = s.capital || {}, ts = s.trades_summary || {}, ex = s.expiry || {}, rg = s.regime || {}, fh = s.feed_health, ws = s.ws, dec = s.decision || {};
    txt('hdr-mode', s.live_orders ? 'LIVE ORDERS' : 'paper'); cls('hdr-mode', s.live_orders ? 'live' : '');
    txt('ch-date', s.session_date || '—');
    txt('cap-equity', isNum(cap.equity) ? inr(cap.equity) : '—');
    txt('cap-pnl', isNum(cap.pnl) ? (inrS(cap.pnl) + '  ' + pct(cap.pnl_pct)) : '—');
    cls('cap-pnl', 'cap-pnl num ' + sgnCls(cap.pnl));
    txt('cap-sub', 'cash ' + inr(cap.cash) + ' · deployed ' + inr(cap.deployed) + (isNum(cap.dd_pct) && cap.dd_pct > 0 ? ' · dd ' + n(cap.dd_pct, 2) + '%' : ''));
    txt('ch-trades', 'trades ' + (isNum(ts.trades_today) ? ts.trades_today : (isNum(rg.entries_today) ? rg.entries_today : '—')));
    txt('ch-expiry', 'exp ' + (ex.date || '—') + ' · ' + S(ex.basis_mode || '—') + (ex.is_monthly ? ' · monthly' : ''));
    const isExp = !!(ex.is_expiry_day || rg.is_expiry_day);
    txt('ch-regime', isExp ? ('EXPIRY DAY' + (ex.shifted_from_holiday ? ' · shifted from ' + S(ex.shifted_from_holiday) : '')) : ('NON-EXPIRY · T-' + (isNum(rg.dte_trading) ? rg.dte_trading : '?') + (rg.band_method ? ' · ' + S(rg.band_method) : '')));
    cls('ch-regime', 'chip ' + (isExp ? 'exp' : 'nonexp'));
    txt('ch-routing', 'routing ' + S(dec.routing_mode || rg.name || '—'));
    if (fh) { txt('ch-feed', fh.ok ? 'FEED OK' : ('FEED ' + S(fh.reason || 'BAD') + (isNum(fh.spot_unchanged_secs) && fh.spot_unchanged_secs > 2 ? ' ' + Math.round(fh.spot_unchanged_secs) + 's' : ''))); cls('ch-feed', 'chip ' + (fh.ok ? (fh.reason === 'RECOVERING' ? 'warn' : 'ok') : 'bad')); }
    else { txt('ch-feed', 'FEED —'); cls('ch-feed', 'chip'); }
    if (ws) {
      show('ch-ws', true);
      const bad = !ws.connected || ws.silent;
      const age = isNum(ws.tick_age_s) ? (ws.tick_age_s < 10 ? n(ws.tick_age_s, 1) + 's' : Math.round(ws.tick_age_s) + 's') : '—';
      txt('ch-ws', 'WS ' + (ws.connected ? (ws.silent ? 'SILENT' : 'live') : (ws.reconnecting || ws.sdk_reconnecting ? 'RECONN' : 'DOWN')) + ' · ' + age);
      cls('ch-ws', 'chip ' + (bad ? 'bad' : (ws.reconnecting || ws.sdk_reconnecting ? 'warn' : 'ok')));
      attr('ch-ws', 'title', 'broker socket · tick age ' + age + ' · ticks ' + (ws.ticks || 0) + ' · bad ' + (ws.bad_ticks || 0) + ' · tokens ' + (ws.tokens || 0) + ' · connects ' + (ws.connects || 0) + ' · drops ' + (ws.drops || 0) + ' · reconnects ok ' + (ws.reconnects_ok || 0) + ' / failed ' + (ws.reconnects_failed || 0) + ' · sub errors ' + (ws.sub_errors || 0) + (ws.last_error ? ' · ' + S(ws.last_error) : ''));
    } else show('ch-ws', false);
    // 2.2 exchange-feed lateness: its own pill (NOT lagpill, which is engine→browser delivery) + the standing pause banner
    const lag = lagSecs(s), fl = s.feed_lag || {}, closed = lagClosed(s);
    if (isNum(lag)) { txt('exlagpill', 'feed ' + secs(lag) + ' s late'); cls('exlagpill', 'pill ' + (lag > 6 || closed ? 'xl-bad' : lag >= 2 ? 'xl-warn' : 'xl-ok')); }
    else { txt('exlagpill', closed ? 'feed LATE' : 'feed —'); cls('exlagpill', 'pill ' + (closed ? 'xl-bad' : 'xl-na')); }
    const wsx = ws ? ' · socket drops today ' + (isNum(ws.drops) ? ws.drops : 0) + (isNum(ws.backlog) ? ' · backlog ' + ws.backlog + ' packets' : '') : '';
    attr('exlagpill', 'title', (isNum(lag) ? 'NIFTY ticks arrive ' + secs(lag) + ' s behind the exchange clock' : 'NIFTY feed lateness vs the exchange clock: not measured (replay, or the socket is not reporting)')
      + wsx + (isNum(fl.limit_s) && fl.limit_s > 0 ? ' · new entries pause above ' + trim(fl.limit_s) + ' s' : '') + ' · (the "live … ms" pill is engine → browser delivery)');
    show('lagbanner', closed);
    if (closed) {
      txt('lagbanner-text', 'ENTRIES PAUSED: NIFTY feed is ' + (isNum(lag) ? secs(lag) + ' s' : 'running') + ' behind the exchange');
      txt('lagbanner-sub', (isNum(fl.limit_s) && fl.limit_s > 0 ? 'limit ' + trim(fl.limit_s) + ' s · ' : '') + (inTrade(s) ? 'the open position is still managed' : 'no new entries until the feed catches up') + (ws && isNum(ws.drops) && ws.drops > 0 ? ' · ' + ws.drops + ' socket drop' + (ws.drops === 1 ? '' : 's') + ' today' : ''));
    }
    // 2.1 machine power (live on Windows): visible only when the engine reports it; red on battery
    const pw = s.power;
    if (pw && (pw.on_battery === true || pw.on_battery === false)) {
      show('ch-power', true);
      const pct = isNum(pw.battery_pct) ? ' ' + Math.round(pw.battery_pct) + '%' : '';
      txt('ch-power', pw.on_battery ? 'ON BATTERY' + pct + ' · PLUG IN' : 'POWER mains' + pct);
      cls('ch-power', 'chip ' + (pw.on_battery ? 'bad' : 'ok'));
    } else show('ch-power', false);
    // clock
    const rt = s.round_ts, clk = isNum(rt) ? rt : (isNum(s.preopen_ts) ? s.preopen_ts : null);
    txt('clock', hms(clk));
    const closeAt = atHM(clk, get(rg.closing, 'session_close', null));
    if (isNum(closeAt) && isNum(clk)) { const left = (closeAt - clk) / 1000; txt('clock-sub', left > 0 ? 'close in ' + dur(left) : 'closed ' + dur(-left) + ' ago'); cls('clock-sub', 'clock-sub' + (left > 0 && left < 900 ? ' warn' : '')); }
    else { txt('clock-sub', isNum(rt) ? 'close —' : 'pre-open'); cls('clock-sub', 'clock-sub'); }
    txt('foot-ver', 'VAJRA ' + S(s.version || '2.0') + (s.boot_id ? ' · boot ' + S(s.boot_id) : ''));
    txt('foot-round', isNum(get(s.round, 'proc_ms')) ? 'round ' + n(s.round.proc_ms, 1) + ' ms' : '');
    txt('foot-tape', s.tape_errors ? 'TAPE ERRORS ' + s.tape_errors : '');
  }

  function rScream(s) {
    const a = s.arming || {}, pos = s.position, cd = s.cooldown || {}, nf_ = s.nifty || {}, rg = s.regime || {}, fh = s.feed_health || {};
    const rt = s.round_ts, D = dists(s);
    let st = 'preopen', glyph = '·', text = '', side = '', feedwarn = false, role = 'status';
    const kv = (k, v, hot) => { v = S(v); return '<span class="kv' + (hot ? ' hot' : '') + '"><span class="k">' + esc(k) + '</span><span class="v" title="' + esc(v) + '">' + esc(v) + '</span></span>'; };
    const lvlTxt = D.side ? D.side + (isNum(D.lvl) ? ' ' + n0(D.lvl) : '') : '';
    if (_wd.kind === 'frozen') { st = 'frozen'; glyph = '✕'; role = 'alert'; text = S(_wd.text) || 'DATA FROZEN'; side = _wd.detail ? kv('action', _wd.detail) : ''; if (pos && pos.open) side += kv('position', posLabel(pos) + ' · manage on ICICI', true); }
    else if (_wd.kind === 'closed') { st = 'closed'; glyph = '■'; text = S(_wd.text) || 'SESSION CLOSED'; }
    else if (!isNum(rt)) {
      st = 'preopen'; glyph = '◌';
      const seeded = isNum(nf_.upper) && isNum(nf_.lower);
      text = 'PRE-OPEN — engine live';
      side = kv('levels', seeded ? 'seeded from prev close' : 'UNSEEDED') + kv('first calc', '09:20') + kv('trading', '09:21');
      if (isNum(nf_.spot)) side += kv('spot', n(nf_.spot)) + (seeded ? kv('lower', n0(nf_.lower)) + kv('upper', n0(nf_.upper)) : '');
    }
    else if (pos && pos.open) {
      st = 'trade'; glyph = dirGlyph(pos.direction) || '▲'; role = 'alert';
      const ns = nextStop(pos), et = entryTs(s);
      feedwarn = !!(fh.ok === false);
      text = (feedwarn ? '⚠ FEED · ' : '') + 'IN TRADE ' + posLabel(pos) + (isNum(ns && ns.dist) ? ' · ₹' + n(ns.dist, 2) + ' to stop' : '') + (isNum(et) ? ' · ' + dur((rt - et) / 1000) : '');
      side = kv('to next stop', isNum(ns && ns.dist) ? '₹' + n(ns.dist, 2) : '—', true)
        + kv('P&L', inrS(pos.unrealized_pnl) + (isNum(pos.unrealized_pnl) && isNum(pos.cost) && pos.cost > 0 ? ' (' + pct(100 * pos.unrealized_pnl / pos.cost, 1) + ')' : ''), true)
        + kv(ns ? ns.mode.toLowerCase() : 'stop', isNum(ns && ns.stop) ? 'SL ' + n(ns.stop) : '—')
        + kv('premium', n(pos.last_premium) + ' / entry ' + n(pos.entry_fill));
      if (feedwarn) side += kv('feed', S(fh.reason || 'BAD') + ' — check ICICI');
      const flat = atHM(rt, get(rg.closing, 'flatten_before', null));
      if (isNum(flat)) { const left = (flat - rt) / 1000; if (left <= 600) side += kv('FLATTEN in', left > 0 ? dur(left) : 'NOW', true); }
    }
    else if (feedFrozen(s)) {
      st = 'feed'; glyph = '✕'; role = 'alert';
      text = 'FEED ' + S(fh.reason || 'FROZEN') + (isNum(fh.spot_unchanged_secs) ? ' ' + Math.round(fh.spot_unchanged_secs) + 's' : '') + ' — entries & re-anchors paused';
      side = kv('action', 'check ICICI price') + (lvlTxt ? kv('nearest', lvlTxt + (isNum(D.d) ? ' · ' + n(D.d, 1) + ' pts' : '')) : '');
    }
    else if (cd.stop_cooldown_active) {
      st = 'cooldown'; glyph = '⏸';
      text = 'POST-STOP COOLDOWN — ' + (isNum(cd.stop_cooldown_remaining_secs) ? dur(cd.stop_cooldown_remaining_secs) + ' remaining' : 'remaining time unknown');
      if (lvlTxt) side = kv('nearest', lvlTxt + (isNum(D.d) ? ' · ' + n(D.d, 1) + ' pts' : ''));
    }
    else {
      const A = S(a.state) || 'STAND_BY', allowed = buyAllowed(s), tg = a.target || {};
      const tgTxt = 'BUY ' + K(tg.strike) + ' ' + S(tg.right), wg = waitGate(s);
      if (A === 'BREACHED') {
        st = (D.side === 'LOWER' || (D.side == null && a.lean === 'DOWN')) ? 'breached-dn' : 'breached-up'; glyph = st === 'breached-dn' ? '▼' : '▲'; role = 'alert';
        const thru = D.side === 'UPPER' ? D.du : D.dd;
        text = 'BREACHED ' + lvlTxt + (isNum(thru) ? ' · ' + n(Math.abs(thru), 1) + ' pts through' : '') + (allowed ? ' · ' + tgTxt : wg ? ' · WAIT' : ' · NO TRADE');
        if (!allowed) side += kv(wg ? 'waiting' : 'no trade', noTradeReason(s));
      } else if (A === 'ABOUT_TO_BREACH') {
        st = 'about'; glyph = '⚠';
        text = 'APPROACHING ' + lvlTxt + (isNum(D.d) ? ' · ' + n(D.d, 1) + ' pts' : '') + (allowed ? ' · ' + tgTxt : wg ? ' · WAIT' : '');
        if (!allowed && (a.entry_block || cd.stop_cooldown_active || wg || lagClosed(s))) side += kv(wg ? 'waiting' : 'no trade', noTradeReason(s));
      } else {
        st = A === 'WATCHING' ? 'watching' : 'standby'; glyph = A === 'WATCHING' ? '◎' : '○';
        text = (A === 'WATCHING' ? 'WATCHING' : 'STAND-BY') + (lvlTxt ? ' · ' + lvlTxt + (isNum(D.d) ? ' in ' + n(D.d, 1) + ' pts' : '') : (isNum(nf_.spot) ? ' · no levels yet' : ''));
      }
      if (a.lean && A !== 'STAND_BY') side += kv('lean', a.lean);
    }
    cls('scream', 'scream st-' + st + (feedwarn ? ' feedwarn' : ''));
    attr('scream', 'role', role);
    txt('scream-glyph', glyph); txt('scream-text', text); html('scream-side', side);
  }

  function rWhyNoTrade(s) {
    const id = s.idle || {}, ts = s.trades_summary || {}, rg = s.regime || {}, rt = s.round_ts;
    const hasGate = !!s.idle && ('gate' in s.idle), gate = id.gate;
    let gtxt = S(id.text) || (hasGate && gate == null ? 'entry fired' : (isNum(rt) ? '—' : 'pre-open')), gc = '';
    if (gate === 'POST_STOP_COOLDOWN' || gate === 'POST_PROFIT_COOLDOWN') gc = 'gate-cool';
    else if (gate === 'FEED_UNHEALTHY' || gate === 'LEVELS_STALE' || gate === 'NO_LEVELS') gc = 'gate-bad';
    else if (gate === 'POSITION_ACTIVE') gc = 'gate-trade';
    else if (gate === 'BEFORE_TRADE_START' || gate === 'AFTER_LAST_ENTRY') gc = 'gate-calm';
    // 2.2: the three "wait" guards are a pause by design (styled like the cooldown, never as an error)
    if (gate && WAIT[gate]) { gtxt = 'WAIT — ' + waitWhy(s, gate); gc = 'gate-cool gate-wait'; }
    // 2.2: FEED_UNHEALTHY raised by the late-feed pause (the feed itself is alive, only late)
    else if (gate === 'FEED_UNHEALTHY' && lagClosed(s) && get(s.feed_health, 'ok', true) !== false) { const l = lagSecs(s), lim = get(s.feed_lag, 'limit_s', null); gtxt = 'entries paused — NIFTY feed ' + (isNum(l) ? secs(l) + ' s ' : '') + 'late' + (isNum(lim) && lim > 0 ? ' (limit ' + trim(lim) + ' s)' : ''); }
    txt('wnt-text', gtxt); attr('wnt-text', 'title', gtxt); cls('wnt-text', 'wnt-text ' + gc);
    txt('wnt-code', gate ? S(gate) : '');
    const prox = proxPts(s);
    if (isNum(id.nearest_dist)) {
      txt('wnt-nearest', S(id.nearest_side) + ' · ' + n(id.nearest_dist, 1) + ' pts' + (isNum(id.nearest_pct) ? ' (' + n(id.nearest_pct, 3) + '%)' : ''));
      cls('wnt-nearest', 'v num ' + (id.nearest_dist <= prox ? 'warn' : ''));
    } else { txt('wnt-nearest', '—'); cls('wnt-nearest', 'v num'); }
    txt('wnt-prox', isNum(prox) ? '±' + n(prox, 0) + ' pts' : '—');
    if (isNum(id.half_width)) {
      const lx = id.band_method === 'mtm' && isNum(id.legacy_half_width);
      html('wnt-band', '±' + n(id.half_width, 1) + (id.band_method ? ' ' + esc(id.band_method) : '') + (lx ? '<span class="lx"> · 1.x would be ±' + n(id.legacy_half_width, 1) + '</span>' : ''));
      attr('wnt-band', 'title', 'half-width of the pin band' + (lx ? ' · the 1.x (legacy) band would be ±' + n(id.legacy_half_width, 1) : ''));
    } else { html('wnt-band', '—'); attr('wnt-band', 'title', null); }
    if (isNum(id.next_reanchor_secs)) { txt('wnt-reanchor', id.next_reanchor_secs > 0 ? dur(id.next_reanchor_secs) : 'due'); cls('wnt-reanchor', 'v num ' + (id.next_reanchor_secs <= 60 ? 'warn' : '')); }
    else { txt('wnt-reanchor', '—'); cls('wnt-reanchor', 'v num'); }
    txt('wnt-trades', (isNum(ts.trades_today) ? ts.trades_today : (isNum(rg.entries_today) ? rg.entries_today : '—')) + (isNum(ts.closed) ? ' · ' + ts.closed + ' closed' : ''));
    const le = lastEntryTs(s);
    txt('wnt-lastentry', (le ? hms(le) : 'none') + (rg.last_entry ? ' · cutoff ' + S(rg.last_entry) : ''));
    txt('wnt-squareoff', S(get(rg.closing, 'flatten_before', null)) || '—');
  }

  // 2.2 the quiet guards line: which entry guards are switched on; the one that is holding an entry right now lights up
  function rGuards(s) {
    const g = s.guards, fl = s.feed_lag; rGamma(s);
    if ((!g || typeof g !== 'object') && (!fl || typeof fl !== 'object')) { show('guards', false); return; }
    const gate = get(s.idle, 'gate', null), out = [];
    const chip = (t, k, tip) => out.push('<span class="gchip' + (k ? ' ' + k : '') + '"' + (tip ? ' title="' + esc(tip) + '"' : '') + '>' + esc(t) + '</span>');
    if (g && typeof g === 'object') {
      if (isNum(g.retreat_secs) && g.retreat_secs > 0) {
        const mo = isNum(g.momentum_min_pts) && g.momentum_min_pts > 0, on = gate === 'ENTRY_RETREATING' ? 'on' : '';
        if (mo) chip('momentum: +' + trim(g.momentum_min_pts) + ' pts / ' + dur(g.retreat_secs), on, 'an entry waits until NIFTY has moved at least ' + trim(g.momentum_min_pts) + ' pts toward the level over the last ' + dur(g.retreat_secs));
        else chip('no-retreat ' + dur(g.retreat_secs) + (isNum(g.retreat_pts) && g.retreat_pts > 0 ? ' / ' + trim(g.retreat_pts) + ' pts' : ''), on,
          'an entry waits while NIFTY is moving away from the level' + (isNum(g.retreat_pts) && g.retreat_pts > 0 ? ' by more than ' + trim(g.retreat_pts) + ' pts' : '') + ' over the last ' + dur(g.retreat_secs));
      }
      if (g.rebuy) {
        const rw = g.rebuy_wait, tip = 'after a profitable exit, a same-side re-buy waits until NIFTY passes the previous trade\'s best price';
        if (rw && typeof rw === 'object' && isNum(rw.beyond)) chip('re-buy ' + dirGlyph(rw.dir) + ' needs NIFTY ' + (rw.dir === 'DOWN' ? 'below ' : rw.dir === 'UP' ? 'above ' : 'beyond ') + n0(rw.beyond), gate === 'REBUY_WAIT' ? 'on' : 'armed', tip);
        else chip('re-buy needs new high/low', '', tip);
      }
      if (isNum(g.max_breach_pts) && g.max_breach_pts > 0) chip('max breach ' + trim(g.max_breach_pts) + ' pts', gate === 'BREACH_TOO_DEEP' ? 'on' : '', 'no entry once NIFTY is already more than ' + trim(g.max_breach_pts) + ' pts through the level');
    }
    if (g && typeof g === 'object') {      // 2.4 gamma-aware entry rules
      if (isNum(g.gamma_anticipate_max_arm_pts) && g.gamma_anticipate_max_arm_pts > 0) chip('low gamma waits for the break (> ' + trim(g.gamma_anticipate_max_arm_pts) + ' pts to arm)', gate === 'GAMMA_CONFIRM_WAIT' ? 'on' : '',
        'when the option needs more than ' + trim(g.gamma_anticipate_max_arm_pts) + ' NIFTY points to arm the trail, the entry waits until NIFTY has traded ' + (isNum(g.gamma_confirm_breach_pts) ? trim(g.gamma_confirm_breach_pts) + ' pts ' : '') + 'through the level; with more gamma it still buys just before the level');
      if (isNum(g.retry_confirm_secs) && g.retry_confirm_secs > 0) chip('retry after a failed attempt needs the break' + (g.retry_side ? ' (' + (g.retry_side === 'UP' ? '▲' : '▼') + ' active)' : ''), gate === 'RETRY_NEEDS_BREAK' ? 'on' : (g.retry_side ? 'armed' : ''),
        'for ' + dur(g.retry_confirm_secs) + ' after a same-side trade that ended at the hard or protective stop, the next entry on that side waits until NIFTY has traded through the level');
      if (isNum(g.gamma_min_arm_pts) && g.gamma_min_arm_pts > 0) chip('no entry under ' + trim(g.gamma_min_arm_pts) + ' pts to arm', gate === 'GAMMA_TOO_TIGHT' ? 'on' : '', 'an option that arms the trail in fewer NIFTY points than this has its stop inside ordinary noise');
      if (g.strike_select === 'gamma') chip('strike: most gamma per rupee', '', 'the strike among ATM and the next out-of-the-money strikes with the most gamma per rupee of premium (delta, premium and spread permitting)');
    }
    const be = g && typeof g === 'object' ? g.breakeven : null;
    if (be && typeof be === 'object' && isNum(be.arm_pct) && isNum(be.lock_pct)) { const P = v => (v >= 0 ? '+' : '−') + trim(Math.abs(v * 100)) + '%'; chip('protective stop ' + P(be.arm_pct) + ' → ' + P(be.lock_pct), '', 'once a trade has been up ' + P(be.arm_pct) + ', it is stopped out if the premium comes back to ' + P(be.lock_pct) + ' (exit reason: protective stop)'); }
    if (fl && isNum(fl.limit_s) && fl.limit_s > 0) chip('late-feed pause ' + trim(fl.limit_s) + ' s', fl.closed === true ? 'bad' : '', 'new entries pause while the NIFTY feed is more than ' + trim(fl.limit_s) + ' s behind the exchange');
    show('guards', true);
    html('guards-list', out.length ? out.join('') : '<span class="gchip off">none switched on</span>');
  }

  // 2.4 GAMMA line: the option the next entry would buy, and what it asks of the index
  function rGamma(s) {
    const g = s.gamma;
    if (!g || typeof g !== 'object' || !isNum(g.strike) || !isNum(g.delta) || !isNum(g.gamma)) { show('gammaline', false); return; }
    show('gammaline', true);
    const conv = isNum(g.convexity) ? g.convexity : null;
    const lvl = conv == null ? '' : conv >= 1.15 ? 'HIGH GAMMA' : conv >= 1.07 ? 'SOME GAMMA' : 'LOW GAMMA';
    txt('gm-opt', K(g.strike) + ' ' + S(g.right) + (isNum(g.price) ? ' @ ' + n(g.price) : ''));
    txt('gm-delta', 'delta ' + n(Math.abs(g.delta), 2));
    txt('gm-gamma', 'gamma ' + n(g.gamma * 100, 3) + ' /100 pts');
    txt('gm-arm', isNum(g.arm_pts) ? 'arms after ' + n(g.arm_pts, 0) + ' pts' : '');
    txt('gm-stop', isNum(g.stop_pts) ? 'stop ' + n(g.stop_pts, 0) + ' pts away' : '');
    txt('gm-iv', isNum(g.iv) ? 'IV ' + n(g.iv * 100, 1) + '%' : '');
    txt('gm-level', lvl + (g.mode === 'confirm' ? ' · waits for the break' : g.mode === 'anticipate' ? ' · buys before the level' : ''));
    cls('gm-level', 'gm-level ' + (conv == null ? '' : conv >= 1.15 ? 'hi' : conv >= 1.07 ? 'mid' : 'lo'));
    attr('gammaline', 'title', 'the option the next entry on the nearer side would buy. delta: premium change per NIFTY point. gamma: how much delta grows per 100 points in our favour. ' +
      'arms after N pts: NIFTY points needed for the +10% that arms the trail; stop N pts away: adverse points to the hard stop. ' +
      (conv != null ? 'convexity ' + n(conv, 2) + ' = gain from a typical 10-minute move in our favour divided by the loss from the same move against us (1.00 = no gamma edge).' : ''));
  }

  function rHero(s) {
    const nf_ = s.nifty || {}, id = s.idle || {}, rt = s.round_ts, fh = s.feed_health || {}, trade = inTrade(s);
    const spot = nf_.spot, lo = nf_.lower, up = nf_.upper, D = dists(s);
    txt('spot', isNum(spot) ? n(spot) : '—');
    txt('spot-delta', isNum(nf_.delta) ? sg(nf_.delta, 2) : ''); cls('spot-delta', 'spot-delta num ' + sgnCls(nf_.delta));
    txt('hero-src', nf_.spot_source === 'CLOSING_PROXY' ? 'spot = parity proxy' + (isNum(nf_.index_spot) ? ' · indicative index ' + n(nf_.index_spot) : '') : (nf_.spot_source ? 'spot ' + S(nf_.spot_source).toLowerCase() : (nf_.preopen ? 'pre-open' : '')));
    attr('hero-src', 'title', nf_.spot_source === 'CLOSING_PROXY' ? 'closing session: the index is frozen / an indicative auction close; spot is the option put-call-parity forward' : null);
    show('hero-levels-stale', !!s.levels_stale);
    const ni = get(s.instruments, 'NIFTY', null);
    const tickStale = !!(ni && ni.stale) || (isNum(fh.spot_unchanged_secs) && fh.spot_unchanged_secs >= 15);
    show('hero-tick-stale', tickStale);
    txt('hero-tick-stale', 'TICK STALE' + (isNum(fh.spot_unchanged_secs) && fh.spot_unchanged_secs >= 15 ? ' ' + Math.round(fh.spot_unchanged_secs) + 's' : ''));
    // band
    const du = D.du, dd = D.dd;
    txt('band-lower', isNum(lo) ? n0(lo) : '—'); txt('band-upper', isNum(up) ? n0(up) : '—');
    const prox = proxPts(s);
    const dCls = d => !isNum(d) ? '' : (d < 0 ? 'bad' : d <= prox ? 'warn' : '');
    txt('band-dn', isNum(dd) ? (dd < 0 ? '▼ through by ' + n(-dd, 1) : '▼ ' + n(dd, 1) + ' pts') : '—'); cls('band-dn', 'd num ' + dCls(dd));
    txt('band-up', isNum(du) ? (du < 0 ? '▲ through by ' + n(-du, 1) : '▲ ' + n(du, 1) + ' pts') : '—'); cls('band-up', 'd num ' + dCls(du));
    let frac = 0.5;
    if (isNum(spot) && isNum(lo) && isNum(up) && up > lo) frac = (spot - lo) / (up - lo);
    // the marker is a track-wide wrapper translated by % of its own (= the track's) width: compositor-only motion
    style('band-spot', 'transform', 'translateX(' + clamp(10 + frac * 80, 1, 99).toFixed(2) + '%)');
    txt('band-spot-lbl', isNum(spot) ? n(spot, 1) : '');
    // danger (static; calm while in a trade — an entry is impossible then)
    let dk = 'd-safe', dt = 'NO LEVELS', mk = '';
    if (isNum(du) && isNum(dd)) {
      if (du < 0) { dk = 'd-breach-up'; dt = 'BREACH ▲ ' + n(-du, 1); mk = 'up'; }
      else if (dd < 0) { dk = 'd-breach-dn'; dt = 'BREACH ▼ ' + n(-dd, 1); mk = 'dn'; }
      else { const g = D.side === 'UPPER' ? '▲' : '▼';
        if (trade) { dk = 'd-trade'; dt = 'in trade · level ' + n0(D.lvl) + ' ' + g + ' ' + n(D.d, 1); }
        else if (D.d <= 0.4 * prox) { dk = 'd-imminent'; dt = '⚠ IMMINENT ' + g + ' ' + n(D.d, 1); mk = 'amb'; }
        else if (D.d <= prox) { dk = 'd-approach'; dt = '◆ APPROACH ' + g + ' ' + n(D.d, 1); mk = 'amb'; }
        else { dk = 'd-safe'; dt = 'clear ' + g + ' ' + n(D.d, 1) + ' pts'; } }
    } else if (!isNum(rt)) { dt = nf_.preopen ? 'pre-open' : 'waiting'; }
    txt('danger', dt); attr('danger', 'title', isNum(du) && isNum(dd) ? 'upper ' + n(du, 1) + ' pts · lower ' + n(dd, 1) + ' pts · entry proximity ±' + prox : null);
    cls('danger', 'danger ' + dk); cls('band-spot', 'band-spot ' + mk);
    // BUY callout (one colour, never red) — or the reason there is no trade on a breach/approach
    const a = s.arming || {}, A = S(a.state);
    if (buyAllowed(s)) { const tg = a.target; show('buy', true); txt('buy-k', 'BUY'); txt('buy-text', 'NIFTY ' + expLabel(get(s.expiry, 'date', '')) + ' ' + K(tg.strike) + ' ' + (tg.right === 'PE' ? '▼ PE' : tg.right === 'CE' ? '▲ CE' : S(tg.right)) + (tg.label && !/\d{5}/.test(S(tg.label)) ? ' · ' + S(tg.label) : '')); cls('buy', 'buy'); }
    else if (!trade && (A === 'BREACHED' || A === 'ABOUT_TO_BREACH')) { const wg = waitGate(s); show('buy', true); txt('buy-k', wg ? 'WAIT' : 'NO TRADE'); txt('buy-text', noTradeReason(s)); cls('buy', 'buy no' + (wg ? ' wait' : '')); }
    else show('buy', false);
    // stats
    txt('atm-strike', K(nf_.atm));
    txt('atm-ce', isNum(nf_.atm_ce) ? n(nf_.atm_ce) : '—'); txt('atm-pe', isNum(nf_.atm_pe) ? n(nf_.atm_pe) : '—');
    txt('fut-mode', nf_.future_mode ? '(' + S(nf_.future_mode).toLowerCase() + ')' : ''); txt('fut', isNum(nf_.future) ? n(nf_.future) : '—');
    txt('basis', isNum(nf_.basis) ? sg(nf_.basis, 2) + (nf_.basis > 0 ? ' prem' : nf_.basis < 0 ? ' disc' : '') : '—'); cls('basis', 'v num ' + sgnCls(nf_.basis));
    txt('vix', isNum(nf_.vix) ? n(nf_.vix) : '—');
    const mg = (isNum(nf_.max_oi_ce) || isNum(nf_.max_oi_pe)) ? 'CE ' + K(nf_.max_oi_ce) + ' · PE ' + K(nf_.max_oi_pe) : '—';
    txt('magnets', mg); attr('magnets', 'title', 'max open interest strikes: CE ' + K(nf_.max_oi_ce) + ' · PE ' + K(nf_.max_oi_pe));
    const calc = nf_.calc || {};
    const bm = S(id.band_method || calc.band_method);
    const bmTxt = (bm || '—') + (isNum(id.half_width) ? ' ±' + n(id.half_width, 1) : '') + (calc.narrowed ? ' · narrowed' : '');
    txt('band-method', bmTxt); attr('band-method', 'title', bmTxt + (bm === 'mtm' && isNum(id.legacy_half_width) ? ' · 1.x (legacy) band would be ±' + n(id.legacy_half_width, 1) : ''));
    // freshness
    const age = nf_.held_age_secs, hold = nf_.hold_secs;
    if (isNum(age) && isNum(hold) && hold > 0) {
      const f = clamp(age / hold, 0, 1);
      style('fresh-fill', 'transform', 'scaleX(' + f.toFixed(3) + ')'); cls('fresh-fill', 'fresh-fill' + (s.levels_stale ? ' bad' : f >= 0.9 ? ' warn' : ''));
      txt('fresh-text', 'held ' + dur(age) + ' / ' + dur(hold) + (s.levels_stale ? ' (stale)' : '')); cls('fresh-text', 'v num' + (s.levels_stale ? ' bad' : ''));
    } else { style('fresh-fill', 'transform', 'scaleX(0)'); txt('fresh-text', '—'); cls('fresh-text', 'v num'); }
    txt('fresh-reanchor', isNum(id.next_reanchor_secs) ? '· re-anchor in ' + dur(id.next_reanchor_secs) : '');
    txt('prov', (calc.ts || calc.trigger) ? ('levels @ ' + hms(calc.ts) + ' · ' + S(calc.trigger || '—') + ' · ITM CE ' + K(calc.k_ce) + ' @ ' + n(calc.vwap_ce) + ' · ITM PE ' + K(calc.k_pe) + ' @ ' + n(calc.vwap_pe) + (isNum(calc.basis) ? ' · basis ' + sg(calc.basis, 2) : '') + (calc.basis_mode ? ' ' + S(calc.basis_mode) : '') + (isNum(calc.anchor_spot) ? ' · anchor ' + n(calc.anchor_spot, 1) : '')) : 'levels: not calculated yet');
  }

  function rScreamPanel(s) {
    const a = s.arming || {}, c = a.confluence || {}, cd = s.cooldown || {}, trade = inTrade(s);
    const A = S(a.state) || (isNum(s.round_ts) ? 'STAND_BY' : 'PRE-OPEN');
    let sc = 'st-standby', label = A.replace(/_/g, ' ');
    if (trade) { sc = 'st-trade'; label = 'IN TRADE'; }
    else if (cd.stop_cooldown_active) { sc = 'st-cooldown'; label = 'POST-STOP COOLDOWN'; }
    else if (A === 'BREACHED') { sc = (a.side === 'LOWER' || a.lean === 'DOWN') ? 'st-breached-dn' : 'st-breached-up'; label = 'BREACHED ' + S(a.side); }
    else if (A === 'ABOUT_TO_BREACH') { sc = 'st-about'; label = 'ABOUT TO BREACH ' + S(a.side); }
    else if (A === 'WATCHING') { sc = 'st-watching'; label = 'WATCHING' + (a.side ? ' ' + S(a.side) : ''); }
    txt('sc-state', label); cls('sc-state', 'sc-state ' + sc);
    txt('sc-lean', a.lean ? 'lean ' + S(a.lean) : 'lean —'); cls('sc-lean', 'badge ' + (a.lean === 'UP' ? 'b-ok' : a.lean === 'DOWN' ? 'b-bad' : 'b-mut'));
    if (a.entry_block) { show('sc-block', true); txt('sc-block', S(a.entry_block)); cls('sc-block', 'badge ' + (a.entry_block === 'ENTERED' ? 'b-blu' : /COOLDOWN|^WAIT\b/i.test(S(a.entry_block)) ? 'b-vio' : 'b-warn')); }
    else show('sc-block', false);
    const msg = S(a.message) || (isNum(s.round_ts) ? '' : 'engine pre-open');
    txt('sc-msg', msg); attr('sc-msg', 'title', msg || null);
    txt('sc-verdict', c.verdict ? S(c.verdict) + (isNum(c.confidence) ? ' · conf ' + n(c.confidence, 2) : '') + (c.conflicted ? ' · conflicted' : '') : 'no read');
    const pb = c.pulls_by_instrument || {};
    const tags = (arr, k) => (Array.isArray(arr) && arr.length) ? arr.map(i => '<span class="tag ' + k + '">' + esc(i) + (isNum(pb[i]) ? '<span class="pull">' + n(pb[i], 2) + '</span>' : '') + '</span>').join('') : '<span class="mut">none</span>';
    html('sc-same', tags(c.same_side, 'up')); html('sc-opp', tags(c.opp_side, 'dn'));
    txt('sc-same-pull', isNum(c.same_pull) ? 'Σ ' + n(c.same_pull, 2) : ''); txt('sc-opp-pull', isNum(c.opp_pull) ? 'Σ ' + n(c.opp_pull, 2) : '');
    txt('sc-net', isNum(c.net_pull) ? sg(c.net_pull, 2) + ' rel' + (isNum(c.absolute_net_pull) ? ' · abs ' + sg(c.absolute_net_pull, 2) : '') : '—');
  }

  let _flatKey = null;
  function rPosition(s, heavy) {
    const pos = s.position, rt = s.round_ts, cap = s.capital || {};
    if (!pos || !pos.open) {
      ring.key = null; ring.t.length = ring.p.length = ring.s.length = 0;
      show('pos-open', false); show('pos-flat', true); show('pos-mode', false); show('pos-time', false);
      const j = Array.isArray(s.journal) ? s.journal : [];
      let lastT = null; for (let i = j.length - 1; i >= 0; i--) if (j[i] && j[i].status === 'CLOSED') { lastT = j[i]; break; }
      const key = lastT ? S(lastT.exit_ts) + '|' + S(lastT.pnl) : (isNum(rt) ? 'none' : 'pre');
      if (key !== _flatKey) {
        _flatKey = key;
        txt('pos-flat', lastT ? ('FLAT · last exit ' + hms(lastT.exit_ts) + ' — ' + dirGlyph(lastT.direction) + ' ' + K(lastT.strike) + ' ' + S(lastT.right) + ' · ' + exitReason(lastT.exit_reason, s)[0] + '\n' + inrS(lastT.pnl) + ' (' + pct(lastT.pnl_pct, 1) + ') · ' + n(lastT.entry_price) + ' → ' + n(lastT.exit_price)) : (isNum(rt) ? 'FLAT · no trade yet today' : 'FLAT · pre-open'));
      }
      return;
    }
    show('pos-flat', false); show('pos-open', true);
    const ns = nextStop(pos), et = entryTs(s);
    txt('pos-ticket', ticket(s));
    show('pos-mode', true); txt('pos-mode', ns.mode + (ns.armed ? '' : ' · trail not armed')); cls('pos-mode', 'badge ' + (ns.mode === 'TRAILING' || ns.mode === 'PROTECTED' ? 'b-ok' : ns.mode === 'HARD-STOP NEAR' ? 'b-bad' : 'b-blu'));
    if (isNum(et) && isNum(rt)) { show('pos-time', true); txt('pos-time', 'held ' + dur((rt - et) / 1000) + ' · in ' + hms(et)); } else show('pos-time', false);
    html('pos-dir', esc(posLabel(pos)) + '<small>' + (pos.direction === 'UP' ? 'long CE' : pos.direction === 'DOWN' ? 'long PE' : '') + (isNum(pos.qty_remaining) ? ' · ' + n0(pos.qty_remaining) + ' qty' : '') + '</small>');
    cls('pos-dir', 'pos-dir ' + (pos.direction === 'UP' ? 'up' : pos.direction === 'DOWN' ? 'dn' : ''));
    txt('pos-stopdist', isNum(ns.dist) ? '₹' + n(ns.dist, 2) : '—');
    const dfrac = isNum(ns.dist) && isNum(pos.entry_fill) && pos.entry_fill > 0 ? ns.dist / pos.entry_fill : 1;
    cls('pos-stopdist', 'v num ' + (dfrac <= 0.03 ? 'bad' : dfrac <= 0.06 ? 'warn' : ''));
    txt('pos-stopat', 'prem · ' + (ns.kind === 'hard' ? 'SL ' : ns.name + ' ') + (isNum(ns.stop) ? n(ns.stop) : '—'));
    const upnl = pos.unrealized_pnl, upct = isNum(upnl) && isNum(pos.cost) && pos.cost > 0 ? 100 * upnl / pos.cost : null;
    txt('pos-pnl', inrS(upnl)); txt('pos-pnlpct', isNum(upct) ? pct(upct, 2) + ' of entry' : ''); cls('pos-pnl', 'v num ' + sgnCls(upnl)); cls('pos-pnlpct', 's num ' + sgnCls(upnl));
    txt('pos-entry', n(pos.entry_fill)); txt('pos-peak', n(pos.peak_premium)); txt('pos-now', n(pos.last_premium));
    const given = isNum(pos.peak_premium) && isNum(pos.last_premium) ? pos.peak_premium - pos.last_premium : null;
    txt('pos-given', isNum(given) ? '₹' + n(given, 2) + (isNum(pos.peak_premium) && pos.peak_premium > 0 ? ' (' + n(100 * given / pos.peak_premium, 1) + '%)' : '') : '—'); cls('pos-given', 'v num ' + (isNum(given) && given > 0 ? 'warn' : ''));
    txt('pos-floor', ns.armed ? n(pos.trail_floor) : '—');
    txt('pos-qty', (isNum(pos.qty_remaining) ? n0(pos.qty_remaining) : '—') + (isNum(pos.qty_total) && pos.qty_total !== pos.qty_remaining ? ' / ' + n0(pos.qty_total) : '') + ' · ' + (isNum(pos.lots) ? pos.lots + ' lot' + (pos.lots === 1 ? '' : 's') : '—'));
    txt('pos-deployed', inr(isNum(cap.deployed) ? cap.deployed : pos.cost));
    const ep = pos.exit_plan || {};
    txt('pos-hsl', isNum(ep.hard_sl) ? n(ep.hard_sl) : '—'); cls('pos-hsl', 'v num ' + (ns.kind === 'hard' ? 'bad' : ''));
    txt('pos-tact', isNum(ep.trail_activate) ? n(ep.trail_activate) + (ns.armed ? ' ✓' : '') : '—');
    txt('pos-minp', isNum(ep.min_profit) ? n(ep.min_profit) : '—');
    txt('pos-real', isNum(pos.realized_pnl) && pos.realized_pnl !== 0 ? inrS(pos.realized_pnl) : '—');
    rLadder(pos, ns, dfrac);
    // ring buffer of premium vs stop (client side); redrawn only when it advances
    const key = S(pos.direction) + '|' + S(pos.strike) + '|' + S(pos.right) + '|' + S(et);
    if (ring.key !== key) { ring.key = key; ring.t.length = ring.p.length = ring.s.length = 0; ring.lastT = 0; ring.dirty = true; }
    if (isNum(rt) && isNum(pos.last_premium) && (ring.t.length === 0 || rt - ring.lastT >= RING_STEP)) {
      ring.lastT = rt; ring.t.push(rt); ring.p.push(pos.last_premium); ring.s.push(isNum(ns.stop) ? ns.stop : NaN);
      if (ring.t.length > RING_MAX) { ring.t.shift(); ring.p.shift(); ring.s.shift(); }
      ring.dirty = true;
    }
    if (ring.dirty && (heavy || ring.t.length <= 2)) { ring.dirty = false; drawPosSpark(pos); }
  }
  // 2.2 STOP LADDER: one premium scale — hard stop · entry · +floor · arm point · trail floor · peak · now.
  // Every node is static; markers are track-wide wrappers moved by translateX(% of the track), zones are
  // translateX+scaleX. Labels are placed greedily (priority order) so neighbours never overprint.
  const LAD_PX = 6.5 / 4.4;                                    // one 10.5-px mono character as % of a ~440-px track
  function rLadder(pos, ns, dfrac) {
    const ep = pos.exit_plan || {}, e = pos.entry_fill, now = pos.last_premium;
    if (!isNum(e) || e <= 0 || !isNum(now)) { show('ladder', false); return; }
    show('ladder', true);
    const sl = ep.hard_sl, mp = ep.min_profit, arm = ep.trail_activate, pk = pos.peak_premium, fl = ns.armed ? pos.trail_floor : null;
    const bea = isNum(ep.be_arm) ? ep.be_arm : null, bel = isNum(ep.be_lock) ? ep.be_lock : null, beOn = ep.be_armed === true;   // protective stop (null = rule off)
    let lo = e, hi = e;
    for (const v of [sl, mp, arm, pk, fl, now, bea, bel]) if (isNum(v)) { if (v < lo) lo = v; if (v > hi) hi = v; }
    let span = hi - lo; if (span < e * 0.02) span = e * 0.02;
    lo -= span * 0.10; hi += span * 0.10;
    const X = v => clamp((v - lo) / (hi - lo) * 100, 0, 100);
    const mv = (id, v) => { const ok = isNum(v); show(id, ok); if (ok) style(id, 'transform', 'translateX(' + X(v).toFixed(2) + '%)'); };
    const seg = (id, a, b) => { const ok = isNum(a) && isNum(b) && b > a; show(id, ok); if (ok) style(id, 'transform', 'translateX(' + X(a).toFixed(2) + '%) scaleX(' + ((X(b) - X(a)) / 100).toFixed(4) + ')'); };
    const pl = v => { const p = (v / e - 1) * 100; return (p >= 0 ? '+' : '−') + trim(Math.abs(p)) + '%'; };
    mv('lad-sl', sl); mv('lad-entry', e); mv('lad-minp', mp); mv('lad-arm', arm); mv('lad-floor', fl); mv('lad-bearm', bea); mv('lad-belock', bel); mv('lad-peak', pk); mv('lad-now', now);
    cls('lad-sl', 'lad-m sl' + (ns.kind === 'hard' ? ' act' : '')); cls('lad-belock', 'lad-m belock' + (ns.kind === 'be' ? ' act' : beOn ? ' done' : '')); cls('lad-bearm', 'lad-m bearm' + (beOn ? ' done' : '')); cls('lad-floor', 'lad-m floor' + (ns.armed ? ' act' : '')); cls('lad-arm', 'lad-m arm' + (ns.armed ? ' done' : ''));
    attr('lad-peak', 'title', isNum(pk) ? 'peak ' + n(pk) + ' (' + pl(pk) + ')' : null);
    seg('lad-risk', sl, e);
    seg('lad-lock', e, ns.kind !== 'hard' && isNum(ns.stop) && ns.stop > e ? ns.stop : null);
    seg('lad-gap', ns.stop, now);
    cls('lad-gap', 'lad-seg gap' + (dfrac <= 0.03 ? ' bad' : dfrac <= 0.06 ? ' warn' : ''));
    // bottom-row labels (scale ticks), greedy by priority; the top row carries only the live premium
    const placed = [];
    const put = (id, v, t) => {
      if (!isNum(v) || !t) { txt(id, ''); return; }
      const x = X(v), w = t.length * LAD_PX;
      for (const q of placed) if (Math.abs(q[0] - x) < (q[1] + w) / 2 + 1.5) { txt(id, ''); return; }
      placed.push([x, w]); txt(id, t);
    };
    put('lad-sl-l', sl, 'SL ' + n(sl));
    if (ns.kind === 'be') put('lad-belock-l', bel, 'protect ' + n(bel));          // the live protective stop outranks the entry tick beside it
    put('lad-entry-l', e, 'entry ' + n(e));
    put('lad-floor-l', fl, isNum(fl) ? 'trail ' + n(fl) : '');           // once armed, the live trail floor outranks the arm / +floor ticks
    put('lad-arm-l', arm, isNum(arm) ? pl(arm) + (ns.armed ? ' armed' : ' arm') : '');
    if (!beOn) put('lad-bearm-l', bea, isNum(bea) ? pl(bea) + ' protect' : ''); else txt('lad-bearm-l', '');
    put('lad-minp-l', mp, isNum(mp) ? pl(mp) + ' floor' : '');
    if (ns.kind !== 'be') put('lad-belock-l', bel, isNum(bel) && !ns.armed ? pl(bel) : '');
    attr('ladder', 'title', (isNum(bea) && isNum(bel) ? 'protective stop: goes live at ' + n(bea) + ' (' + pl(bea) + '), then stops at ' + n(bel) + ' (' + pl(bel) + ')' + (beOn ? ' — LIVE' : '') + ' · ' : '') + 'active stop: ' + ns.name + (isNum(ns.stop) ? ' ' + n(ns.stop) : ''));
    txt('lad-now-l', n(now) + ' (' + pl(now) + ')'); cls('lad-now', 'lad-m now ' + (now >= e ? 'up' : 'dn') + (X(now) > 80 ? ' rt' : X(now) < 20 ? ' lf' : ''));
    // caption: how far to the stop · what is locked / at risk if that stop is hit
    const qty = isNum(pos.qty_remaining) ? pos.qty_remaining : null;
    if (isNum(ns.dist) && isNum(ns.stop)) {
      txt('lad-cap-stop', ns.dist >= 0 ? '₹' + n(ns.dist) + (now > 0 ? ' (' + n(100 * ns.dist / now, 1) + '%)' : '') + ' above ' + ns.name + ' ' + n(ns.stop) : 'THROUGH the ' + ns.name + ' ' + n(ns.stop) + ' by ₹' + n(-ns.dist));
      const out = ns.stop - e, outT = (isNum(qty) ? inrS(out * qty) + ' ' : '') + '(' + pct(100 * out / e, 1) + ')';
      const nxt = !ns.armed && isNum(arm) && arm > now ? ' · trail arms in ₹' + n(arm - now) : '';
      if (ns.kind !== 'hard' && out >= 0) { txt('lad-cap-lock', 'locked ' + outT + (ns.kind === 'be' ? nxt : '')); cls('lad-cap-lock', 'up'); }
      else { txt('lad-cap-lock', 'at risk ' + outT + (ns.kind === 'hard' && !beOn && isNum(bea) && bea > now ? ' · protects in ₹' + n(bea - now) : nxt)); cls('lad-cap-lock', 'dn'); }
    } else { txt('lad-cap-stop', '—'); txt('lad-cap-lock', ''); cls('lad-cap-lock', ''); }
  }
  function drawPosSpark(pos) {
    const N = ring.p.length, W = 320, H = 64, pad = 4;
    if (N < 1) { html('pos-spark', ''); return; }
    let lo = Infinity, hi = -Infinity;
    const consider = v => { if (isNum(v)) { if (v < lo) lo = v; if (v > hi) hi = v; } };
    ring.p.forEach(consider); ring.s.forEach(consider); consider(pos.entry_fill); consider(pos.last_premium);
    if (!isFinite(lo) || !isFinite(hi)) { html('pos-spark', ''); return; }
    if (hi - lo < 1e-6) { hi += 1; lo -= 1; }
    const span = hi - lo; lo -= span * 0.08; hi += span * 0.08;
    const X = i => N === 1 ? W / 2 : pad + (i / (N - 1)) * (W - 2 * pad), Y = v => H - pad - ((v - lo) / (hi - lo)) * (H - 2 * pad);
    let pp = '', sp = '';
    for (let i = 0; i < N; i++) { pp += X(i).toFixed(1) + ',' + Y(ring.p[i]).toFixed(1) + ' '; if (isNum(ring.s[i])) sp += X(i).toFixed(1) + ',' + Y(ring.s[i]).toFixed(1) + ' '; }
    const eY = isNum(pos.entry_fill) ? Y(pos.entry_fill).toFixed(1) : null;
    let h = '<title>premium (blue) vs stop (red dashed) vs entry (dotted), last ' + dur((ring.t[N - 1] - ring.t[0]) / 1000) + '</title>';
    if (N > 1) h += '<polygon class="fillp" points="' + X(0).toFixed(1) + ',' + (H - pad) + ' ' + pp + X(N - 1).toFixed(1) + ',' + (H - pad) + '"/>';
    if (eY != null) h += '<line class="entry" x1="0" x2="' + W + '" y1="' + eY + '" y2="' + eY + '"/>';
    if (sp) h += '<polyline class="stop" points="' + sp.trim() + '"/>';
    h += N > 1 ? '<polyline class="prem" points="' + pp.trim() + '"/>' : '<circle cx="' + (W / 2) + '" cy="' + Y(ring.p[0]).toFixed(1) + '" r="2" fill="#4fa8ff"/>';
    html('pos-spark', h);
  }

  function rLean(s) {
    const a = s.arming || {}, c = a.confluence || {}, d = s.decision || {}, bb = Array.isArray(s.burst_board) ? s.burst_board : [];
    // engine sends absolute_net_pull null when there is no read → fall back to the bias EWMA
    const hasRead = !!c.verdict && isNum(c.absolute_net_pull);
    const v = hasRead ? c.absolute_net_pull : (isNum(d.bias_ewma) ? d.bias_ewma : null), src = hasRead ? 'net pull' : 'bias ewma';
    const thr = 0.05;
    const lbl = !isNum(v) ? 'NO READ' : v > thr ? 'BULLISH' : v < -thr ? 'BEARISH' : 'NEUTRAL';
    txt('lean-label', (lbl === 'BULLISH' ? '▲ ' : lbl === 'BEARISH' ? '▼ ' : '') + lbl); cls('lean-label', 'lean-label ' + (lbl === 'BULLISH' ? 'up' : lbl === 'BEARISH' ? 'dn' : ''));
    txt('lean-val', isNum(v) ? (v > 0 ? '▲ ' : v < 0 ? '▼ ' : '') + src + ' ' + sg(v, 3) + (isNum(d.bias_ewma) && hasRead ? ' · bias ' + sg(d.bias_ewma, 3) : '') + (isNum(d.conviction) ? ' · conviction ' + n(d.conviction, 2) : '') : 'no confluence read and no bias yet');
    txt('lean-decision', S(d.decision) || '—'); cls('lean-decision', 'badge ' + (d.decision === 'FIRE_UP' ? 'b-ok' : d.decision === 'FIRE_DOWN' ? 'b-bad' : 'b-mut'));
    show('lean-div', !!d.divergence); show('lean-rev', !!d.reversal_freeze); show('lean-gate', !!d.expiry_gate);
    const b = isNum(d.bias_ewma) ? d.bias_ewma : (isNum(v) ? v : 0);
    const scale = Math.max(Math.abs(isNum(d.threshold) ? d.threshold : 0.3) * 2, Math.abs(b), 0.01);
    // the fill is anchored at the centre (left 50%, width 50%); a negative scaleX grows it leftwards
    const w = clamp(Math.abs(b) / scale, 0, 1);
    style('bias-fill', 'transform', 'scaleX(' + (b >= 0 ? w : -w).toFixed(3) + ')'); cls('bias-fill', 'bias-fill ' + (b < 0 ? 'dn' : ''));
    const pb = c.pulls_by_instrument || {};
    const mk = rows => rows.length ? rows.map(r => '<span class="tag ' + (r.breached === 'UP' ? 'up' : 'dn') + '">' + esc(r.instrument) + (isNum(pb[r.instrument]) ? '<span class="pull">' + n(pb[r.instrument], 2) + '</span>' : (isNum(r.strength) ? '<span class="pull">' + n(r.strength, 2) + '</span>' : '')) + '</span>').join('') : '<span class="mut">none</span>';
    html('lean-up', mk(bb.filter(r => r && r.breached === 'UP' && !r.stale && r.instrument !== 'NIFTY')));
    html('lean-dn', mk(bb.filter(r => r && r.breached === 'DOWN' && !r.stale && r.instrument !== 'NIFTY')));
    txt('lean-reason', (S(d.reason) || '—') + (Array.isArray(d.channels) && d.channels.length ? ' · channels ' + d.channels.map(S).join(', ') : '') + (d.lead ? ' · lead' : ''));
  }

  // ---------- keyed rows (the big tables / lists) ---------------------------------------------------
  // A keyed container creates one element per key ONCE (from a static cell skeleton) and afterwards
  // writes only the cells whose value changed (per-cell change gate; the cache is updated after the DOM
  // write, like txt/cls/html). Elements are created / moved / dropped only when the key sequence changes.
  // Clock cells (ago()/dur()) are registered per table and re-driven by a 1-s ticker, so a row is never
  // rebuilt because time passed. No layout reads anywhere.
  const KT = [];
  function ktable(id, tag, skel) { const T = { id, tag, skel, rows: new Map(), order: [], ph: null, clk: [], init: false }; KT.push(T); return T; }
  function ktRow(T) {
    const el = document.createElement(T.tag); el.innerHTML = T.skel;
    const rec = { el, td: Array.prototype.slice.call(el.children), q: Object.create(null), v: Object.create(null) };
    const qs = el.querySelectorAll('[data-q]'); for (let i = 0; i < qs.length; i++) rec.q[qs[i].getAttribute('data-q')] = qs[i];
    return rec;
  }
  // placeholder row (empty table); T.ph != null ⇔ no keyed rows are mounted
  function ktEmpty(T, h) { const body = $(T.id); if (!body || T.ph === h) return; T.rows.clear(); T.order.length = 0; T.clk.length = 0; body.innerHTML = h; T.ph = h; T.init = true; }
  // make the body's children match `keys` (in order); returns the row records in that order
  function ktSync(T, keys) {
    const body = $(T.id); if (!body) return null;
    const n = keys.length, out = new Array(n); T.clk.length = 0;
    let same = T.init && T.ph == null && n === T.order.length;
    if (same) for (let i = 0; i < n; i++) if (T.order[i] !== keys[i]) { same = false; break; }
    if (same) { for (let i = 0; i < n; i++) out[i] = T.rows.get(keys[i]); return out; }
    const keep = new Set(keys);
    for (const [k, rec] of T.rows) if (!keep.has(k)) { T.rows.delete(k); rec.el.remove(); }
    if (T.ph != null || !T.init) { body.textContent = ''; T.ph = null; T.init = true; }      // also drops index.html's static placeholder row
    const frag = document.createDocumentFragment();
    for (let i = 0; i < n; i++) { const k = keys[i]; let rec = T.rows.get(k); if (!rec) { rec = ktRow(T); T.rows.set(k, rec); } frag.appendChild(rec.el); out[i] = rec; }
    body.appendChild(frag); T.order = keys.slice();
    return out;
  }
  // unique keys (a duplicate gets an ordinal suffix so two identical rows never share one element)
  function ktKeys(arr, fn) { const seen = Object.create(null), out = new Array(arr.length); for (let i = 0; i < arr.length; i++) { let k = fn(arr[i], i); const c = seen[k] || 0; seen[k] = c + 1; if (c) k += '#' + c; out[i] = k; } return out; }
  // per-cell gated writers (rec.v is the row's value cache)
  function kT(rec, el, k, s) { if (rec.v[k] === s) return; el.textContent = s; rec.v[k] = s; }
  function kC(rec, el, k, c) { if (rec.v[k] === c) return; el.className = c; rec.v[k] = c; }
  function kH(rec, el, k, h) { if (rec.v[k] === h) return; el.innerHTML = h; rec.v[k] = h; }
  function kA(rec, el, k, name, v) { if (rec.v[k] === v) return; if (v == null) el.removeAttribute(name); else el.setAttribute(name, v); rec.v[k] = v; }
  function kW(rec, el, k, v) { if (rec.v[k] === v) return; el.style.width = v; rec.v[k] = v; }
  // clocks: 'ago' → ago(round_ts, ts); 'hold' → m:ss since ts (the open trade's hold time); 'fresh' → dur(age) extrapolated from round_ts (falls back to the reported age)
  function kClock(T, rec, el, k, kind, ts, age) { T.clk.push({ rec, el, k, kind, ts, age }); }
  function ktTick(T, rt) {
    const L = T.clk;
    for (let i = 0; i < L.length; i++) { const c = L[i]; kT(c.rec, c.el, c.k, c.kind === 'ago' ? ago(rt, c.ts) : c.kind === 'hold' ? mmss(isNum(rt) && isNum(c.ts) ? (rt - c.ts) / 1000 : NaN) : dur(isNum(rt) && isNum(c.ts) ? (rt - c.ts) / 1000 : c.age)); }
  }
  function tickClocks() { const rt = _last ? _last.round_ts : null; for (let i = 0; i < KT.length; i++) ktTick(KT[i], rt); }
  setInterval(() => { try { tickClocks(); } catch (e) { } }, 1000);

  // =================================================================================================
  // HEAVY RENDERERS (≈1 Hz)
  // =================================================================================================
  const VDT =Object.assign(Object.create(null), { STRONG: 'weighted bloc agrees', MILD: 'some of the bloc agrees', LONE: 'NIFTY alone', CONFLICTED: 'both sides pulling' });
  const vdTag = (v, tip) => v ? '<span class="vd ' + esc(v) + '" title="' + esc((VDT[S(v)] || '') + (tip ? ' · ' + tip : '')) + '">' + esc(v) + '</span>' : '';

  const JR = ktable('jr-body', 'tr', '<td></td><td><span data-q="ex"></span></td><td class="r"></td><td class="b"></td><td></td><td class="r"></td><td class="r"></td><td class="r"></td><td class="r b"></td><td class="r b"></td><td class="r"></td><td class="r"></td><td></td><td class="cxs"></td><td></td>');
  // 2.2 the moment of entry (journal row .ctx) as small chips; `prev` = the trade before it (same side → "re-buy")
  function ctxChips(r, prev) {
    const c = r.ctx; if (!c || typeof c !== 'object') return '';
    const out = [], tip = [], se = c.secs_since_exit;
    const chip = (t, k) => out.push('<span class="cx' + (k ? ' ' + k : '') + '">' + esc(t) + '</span>');
    if (c.after === 'profit' && (!isNum(se) || se <= 900)) { const same = !!prev && prev.direction === r.direction; chip((same ? 're-buy ' : prev ? 'flip ' : '') + (isNum(se) ? dur(se) + ' ' : '') + 'after profit', same ? 'warn' : ''); }
    else if (c.after === 'stop' && (!isNum(se) || se <= 900)) chip((isNum(se) ? dur(se) + ' ' : '') + 'after stop', 'vio');
    if (isNum(c.exch_lag_s) && c.exch_lag_s >= 2) chip('feed ' + Math.round(c.exch_lag_s) + 's late', c.exch_lag_s > 6 ? 'bad' : 'warn');
    if (c.breached === true) chip('breached', 'blu');
    if (isNum(c.move_30s) && c.move_30s <= -2) chip('moving away ' + sg(c.move_30s, 1), 'warn');
    if (c.after === 'profit' || c.after === 'stop') tip.push((isNum(se) ? dur(se) + ' ' : '') + 'after a ' + (c.after === 'profit' ? 'profitable exit' : 'stop'));
    if (isNum(c.move_30s) || isNum(c.move_120s)) tip.push('NIFTY toward the trade: ' + (isNum(c.move_30s) ? sg(c.move_30s, 1) + ' pts in 30s' : '') + (isNum(c.move_30s) && isNum(c.move_120s) ? ', ' : '') + (isNum(c.move_120s) ? sg(c.move_120s, 1) + ' pts in 2m' : ''));
    if (isNum(c.exch_lag_s)) tip.push('feed ' + secs(Math.max(0, c.exch_lag_s)) + ' s behind the exchange');
    tip.push(c.breached === true ? 'level already breached at entry' : 'entered on the approach');
    return '<span class="cxw" title="' + esc('at entry — ' + tip.join(' · ')) + '">' + (out.length ? out.join('') : '<span class="cx off">clean</span>') + '</span>';
  }
  let _eqKey = null;
  function rJournal(s) {
    const j = Array.isArray(s.journal) ? s.journal.filter(Boolean).reverse() : [], ts = s.trades_summary || {}, rt = s.round_ts;
    if (!j.length) ktEmpty(JR, '<tr><td colspan="15" class="mut">no trades yet</td></tr>');
    else {
      const recs = ktSync(JR, ktKeys(j, r => S(r.entry_ts) + '|' + S(r.strike) + S(r.right) + S(r.direction)));
      if (recs) for (let i = 0; i < j.length; i++) {
        const r = j[i], R = recs[i], td = R.td, live = r.status === 'OPEN', er = exitReason(r.exit_reason, s);
        const tip = (Array.isArray(r.same_side) && r.same_side.length ? 'same: ' + r.same_side.map(S).join(', ') : '') + (Array.isArray(r.opp_side) && r.opp_side.length ? ' · opp: ' + r.opp_side.map(S).join(', ') : '');
        kC(R, R.el, 'row', live ? 'live' : '');
        kT(R, td[0], '0', hms(r.entry_ts));
        kC(R, R.q.ex, 'exc', live ? 'blu' : ''); kT(R, R.q.ex, 'ex', live ? 'LIVE' : hms(r.exit_ts));
        if (live && isNum(r.entry_ts)) kClock(JR, R, td[2], 'h', 'hold', r.entry_ts);
        else kT(R, td[2], 'h', isNum(r.entry_ts) && isNum(r.exit_ts) ? mmss((r.exit_ts - r.entry_ts) / 1000) : '—');
        kC(R, td[3], '3c', (r.direction === 'UP' ? 'up' : r.direction === 'DOWN' ? 'dn' : '') + ' b'); kT(R, td[3], '3', dirGlyph(r.direction) + ' ' + S(r.direction));
        kT(R, td[4], '4', K(r.strike) + ' ' + S(r.right));
        kT(R, td[5], '5', (isNum(r.qty) ? n0(r.qty) : '—') + (isNum(r.lots) ? ' · ' + r.lots + 'L' : ''));
        kT(R, td[6], '6', n(r.entry_price)); kT(R, td[7], '7', n(r.exit_price));
        kC(R, td[8], '8c', 'r ' + sgnCls(r.pnl_pct) + ' b'); kT(R, td[8], '8', pct(r.pnl_pct, 1));
        kC(R, td[9], '9c', 'r ' + sgnCls(r.pnl) + ' b'); kT(R, td[9], '9', inrS(r.pnl));
        kC(R, td[10], '10c', 'r ' + (isNum(r.peak_pct) && r.peak_pct > 0 ? 'up' : 'mut')); kT(R, td[10], '10', pct(r.peak_pct, 1));
        kC(R, td[11], '11c', 'r ' + (isNum(r.low_pct) && r.low_pct < 0 ? 'dn' : 'mut')); kT(R, td[11], '11', pct(r.low_pct, 1));
        kC(R, td[12], '12c', er[1] === 'rs-be' ? (r.pnl > 0 ? 'rs-trail' : r.pnl < 0 ? 'rs-sl' : 'b') : er[1]); kA(R, td[12], '12t', 'title', S(r.exit_reason) || null); kH(R, td[12], '12', live ? '<span class="mut">open</span>' : esc(er[0]));
        kH(R, td[13], '13', ctxChips(r, j[i + 1]));
        kH(R, td[14], '14', vdTag(r.verdict, tip) + (r.conflicted && r.verdict !== 'CONFLICTED' ? ' <span class="amb" title="conflicted">⚡</span>' : ''));
      }
      ktTick(JR, rt);
    }
    // day summary (numbers from trades_summary; every cell is change-gated)
    const nOpen = j.reduce((a, r) => a + (r.status === 'OPEN' ? 1 : 0), 0), hasC = isNum(ts.closed) && ts.closed > 0;
    txt('jr-n', j.length ? j.length + ' trade' + (j.length === 1 ? '' : 's') + (nOpen ? ' · ' + nOpen + ' open' : '') : '');
    txt('jr-real', isNum(ts.realized_pnl) ? inrS(ts.realized_pnl) : '—'); cls('jr-real', 'v num ' + sgnCls(ts.realized_pnl));
    txt('jr-closed', isNum(ts.closed) ? String(ts.closed) : '—');
    txt('jr-wl', hasC && (isNum(ts.wins) || isNum(ts.losses)) ? (ts.wins || 0) + ' / ' + (ts.losses || 0) : '—');
    txt('jr-wr', hasC && isNum(ts.win_rate_pct) ? n(ts.win_rate_pct, 0) + '%' : '—');
    txt('jr-streak', hasC && isNum(ts.streak) && ts.streak !== 0 ? (ts.streak > 0 ? ts.streak + ' win' + (ts.streak === 1 ? '' : 's') : -ts.streak + ' loss' + (ts.streak === -1 ? '' : 'es')) : '—'); cls('jr-streak', 'v num ' + (hasC ? sgnCls(ts.streak) : ''));
    txt('jr-avgw', hasC && isNum(ts.avg_win) && ts.avg_win ? inr(ts.avg_win) : '—'); txt('jr-avgl', hasC && isNum(ts.avg_loss) && ts.avg_loss ? inr(ts.avg_loss) : '—');
    // equity sparkline: cumulative realized P&L per closed trade; rebuilt ONLY when the closed-trade set changes
    const closed = Array.isArray(s.journal) ? s.journal.filter(r => r && r.status === 'CLOSED' && isNum(r.pnl)) : [];
    const ek = closed.length + '|' + (closed.length ? S(closed[closed.length - 1].exit_ts) + '|' + S(closed[closed.length - 1].pnl) : '');
    if (ek === _eqKey) return;
    _eqKey = ek;
    const W = 260, H = 40, pad = 4;
    if (closed.length < 1) { html('jr-spark', '<line class="z" x1="0" x2="' + W + '" y1="' + (H / 2) + '" y2="' + (H / 2) + '"/>'); return; }
    let cum = 0; const pts = [0]; closed.forEach(r => { cum += r.pnl; pts.push(cum); });
    const lo = Math.min.apply(null, pts), hi = Math.max.apply(null, pts), rng = (hi - lo) || 1;
    const X = i => pad + (i / (pts.length - 1)) * (W - 2 * pad), Y = v => H - pad - ((v - lo) / rng) * (H - 2 * pad);
    const zy = Y(0).toFixed(1), line = pts.map((v, i) => X(i).toFixed(1) + ',' + Y(v).toFixed(1)).join(' ');
    let h = '<title>cumulative realized P&amp;L ' + esc(inrS(cum)) + ' over ' + closed.length + ' closed trade' + (closed.length === 1 ? '' : 's') + '</title>'
      + '<polygon class="a ' + sgnCls(cum) + '" points="' + X(0).toFixed(1) + ',' + zy + ' ' + line + ' ' + X(pts.length - 1).toFixed(1) + ',' + zy + '"/>'
      + '<line class="z" x1="0" x2="' + W + '" y1="' + zy + '" y2="' + zy + '"/><polyline class="l ' + sgnCls(cum) + '" points="' + line + '"/>';
    if (closed.length <= 40) for (let i = 0; i < closed.length; i++) h += '<circle class="d ' + sgnCls(closed[i].pnl) + '" cx="' + X(i + 1).toFixed(1) + '" cy="' + Y(pts[i + 1]).toFixed(1) + '" r="1.9"/>';
    html('jr-spark', h);
  }

  const BB = ktable('burst-body', 'tr', '<td class="mut"></td><td class="b"></td><td class="r"></td><td><span class="st" data-q="st"></span><span class="age hid" data-q="ag"></span></td><td class="r"></td><td class="r"></td><td><span class="heat" data-q="ht"><i data-q="hi"></i></span><span data-q="hv"></span><span data-q="hd"></span></td><td class="r mut"></td>');
  function rBurst(s) {
    const bb = Array.isArray(s.burst_board) ? s.burst_board.filter(Boolean) : [], rt = s.round_ts;
    txt('burst-n', bb.length ? bb.length + ' ranked' : '');
    if (!bb.length) { ktEmpty(BB, '<tr><td colspan="8" class="mut">—</td></tr>'); return; }
    const recs = ktSync(BB, ktKeys(bb, r => S(r.instrument))); if (!recs) return;
    for (let i = 0; i < bb.length; i++) {
      const r = bb[i], R = recs[i], td = R.td, q = R.q, heat = clamp(isNum(r.heat) ? r.heat : 0, 0, 1);
      kC(R, R.el, 'row', r.stale ? 'stale' : '');
      kT(R, td[0], '0', String(i + 1));
      kH(R, td[1], '1', esc(r.instrument) + (r.stale ? ' <span class="st stale">STALE</span>' : ''));
      kT(R, td[2], '2', isNum(r.spot) ? n(r.spot, r.spot >= 1000 ? 1 : 2) : '—');
      kC(R, q.st, 'stc', 'st ' + stCls(r.state)); kT(R, q.st, 'st', stTxt(r.state));
      const showAge = !!r.breached && !r.stale && isNum(r.breach_ts);
      kC(R, q.ag, 'agc', showAge ? 'age' : 'age hid'); if (showAge) kClock(BB, R, q.ag, 'ag', 'ago', r.breach_ts); else kT(R, q.ag, 'ag', '');
      kC(R, td[4], '4c', 'r ' + (isNum(r.dist_up) && r.dist_up < 0 ? 'up b' : '')); kT(R, td[4], '4', isNum(r.dist_up) ? sg(r.dist_up, 1) : '—');
      kC(R, td[5], '5c', 'r ' + (isNum(r.dist_dn) && r.dist_dn < 0 ? 'dn b' : '')); kT(R, td[5], '5', isNum(r.dist_dn) ? sg(r.dist_dn, 1) : '—');
      kA(R, q.ht, 'htt', 'title', 'heat ' + n(heat, 2)); kC(R, q.hi, 'hic', heat >= 0.8 ? 'hot' : heat < 0.25 ? 'cool' : ''); kW(R, q.hi, 'hiw', (heat * 100).toFixed(0) + '%');
      kT(R, q.hv, 'hv', n(heat, 2));
      kC(R, q.hd, 'hdc', isUp(r.dir) ? 'up' : isDn(r.dir) ? 'dn' : ''); kT(R, q.hd, 'hd', isUp(r.dir) ? ' ↑' : isDn(r.dir) ? ' ↓' : '');
      kT(R, td[7], '7', isNum(r.weight) ? n(r.weight, 2) : '—');
    }
    ktTick(BB, rt);
  }

  const BR = ktable('breach-list', 'div', '<span class="ic"></span><span class="m"></span><span class="t"></span><span class="n"></span>');
  function rBreach(s) {
    const ba = Array.isArray(s.breach_alerts) ? s.breach_alerts.filter(Boolean) : [], rt = s.round_ts;
    txt('breach-n', ba.length ? ba.length + ' live' : '');
    if (!ba.length) { ktEmpty(BR, '<div class="mut">no live breaches</div>'); return; }
    const recs = ktSync(BR, ktKeys(ba, r => S(r.instrument) + '|' + S(r.breached) + '|' + S(r.breach_ts))); if (!recs) return;
    for (let i = 0; i < ba.length; i++) {
      const r = ba[i], R = recs[i], td = R.td, up = r.breached === 'UP';
      const by = up ? (isNum(r.dist_up) ? -r.dist_up : null) : (isNum(r.dist_dn) ? -r.dist_dn : null);
      const m = S(r.instrument) + ' ' + (up ? 'UPPER' : 'LOWER') + ' breached' + (isNum(by) ? ' by ' + n(Math.abs(by), 1) + (by < 0 ? ' (back inside)' : '') : '') + (isNum(r.strength) ? ' · str ' + n(r.strength, 2) : '');
      kC(R, R.el, 'row', 'li ' + (up ? 'br-up' : 'br-dn')); kA(R, R.el, 'tt', 'title', m + ' · ' + hms(r.breach_ts));
      kT(R, td[0], 'ic', up ? '▲' : '▼');
      kH(R, td[1], 'm', '<span class="by">' + esc(r.instrument) + '</span> ' + (up ? 'UPPER' : 'LOWER') + ' breached' + (isNum(by) ? ' by <span class="by">' + n(Math.abs(by), 1) + '</span>' + (by < 0 ? ' (back inside)' : '') : '') + (isNum(r.strength) ? ' · str ' + n(r.strength, 2) : ''));
      kT(R, td[2], 't', hms(r.breach_ts));
      kClock(BR, R, td[3], 'n', 'ago', r.breach_ts);
    }
    ktTick(BR, rt);
  }

  const EV = Object.assign(Object.create(null), { FEED_GAP: ['✕', 'ev-bad'], ANOMALY: ['!', 'ev-warn'], APPROACH_ALERT: ['◆', 'ev-warn'], ENTRY_REJECTED: ['✕', 'ev-bad'], EXIT_REJECTED: ['✕', 'ev-bad'], REVERSAL_WATCH: ['↺', 'ev-warn'], FEED_HEALTH: ['♥', 'ev-warn'], SOUND: ['♪', 'ev-blu'], RESTORED: ['↻', 'ev-ok'], REGIME: ['▣', 'ev-mut'], SESSION_ERROR: ['✕', 'ev-bad'], ENTRY: ['▶', 'ev-blu'], EXIT: ['■', 'ev-blu'] });
  function rEvents(s) {
    const ev = Array.isArray(s.events) ? s.events.slice().reverse() : [];
    txt('events-n', ev.length ? ev.length + ' recent' : '');
    if (!ev.length) { html('events-list', '<div class="mut">no events</div>'); return; }
    let h = '';
    for (const e of ev) {
      if (!e) continue;
      const m = EV[S(e.type)] || ['·', 'ev-mut'];
      let k = m[1]; if (e.type === 'FEED_HEALTH' && /RECOVER/i.test(S(e.summary))) k = 'ev-ok';
      h += '<div class="li ' + k + '" title="' + esc(S(e.type) + ' · ' + S(e.summary)) + '"><span class="ic">' + m[0] + '</span><span class="t">' + hms(e.ts) + '</span><span class="m"><b>' + esc(e.type) + '</b> ' + esc(e.summary) + '</span>' + (isNum(e.count) && e.count > 1 ? '<span class="n">×' + e.count + '</span>' : '') + '</div>';
    }
    html('events-list', h);
  }

  const CH = ktable('chain-body', 'tr', '<td class="r cside"><span class="oi ce" data-q="co"><span class="bar" data-q="cb"></span><span class="t" data-q="ct"></span></span></td><td class="r cside mut"></td><td class="r cside mut"></td><td class="r cside ce b"></td><td class="strike"></td><td class="pside pe b"></td><td class="pside mut"></td><td class="pside mut"></td><td class="pside"><span class="oi pe" data-q="po"><span class="bar" data-q="pb"></span><span class="t" data-q="pt"></span></span></td>');
  function rChain(s) {
    const ch = Array.isArray(s.nifty_chain) ? s.nifty_chain.filter(Boolean) : [], nf_ = s.nifty || {}, spot = nf_.spot;
    txt('chain-n', ch.length ? ch.length + ' strikes' + (isNum(nf_.atm) ? ' · ATM ' + K(nf_.atm) : '') : '');
    if (!ch.length) { ktEmpty(CH, '<tr><td colspan="9" class="mut">—</td></tr>'); return; }
    let maxOi = 0; for (const r of ch) { if (isNum(r.ce_oi) && r.ce_oi > maxOi) maxOi = r.ce_oi; if (isNum(r.pe_oi) && r.pe_oi > maxOi) maxOi = r.pe_oi; }
    const oiT = v => isNum(v) ? (v >= 1e5 ? n(v / 1e5, 2) + 'L' : n0(v)) : '—';
    const oiW = v => (maxOi && isNum(v) ? (100 * v / maxOi).toFixed(0) : 0) + '%';
    const recs = ktSync(CH, ktKeys(ch, r => K(r.strike))); if (!recs) return;
    for (let i = 0; i < ch.length; i++) {
      const r = ch[i], R = recs[i], td = R.td, q = R.q;
      const k = r.strike, itmCE = isNum(spot) && isNum(k) && k < spot, itmPE = isNum(spot) && isNum(k) && k > spot;
      const mg = (isNum(nf_.max_oi_ce) && k === nf_.max_oi_ce ? '<span class="mg ce" title="max CE open interest">CE OI</span>' : '') + (isNum(nf_.max_oi_pe) && k === nf_.max_oi_pe ? '<span class="mg pe" title="max PE open interest">PE OI</span>' : '');
      kC(R, R.el, 'row', (r.is_atm ? 'atm ' : '') + (itmCE ? 'itm-ce ' : '') + (itmPE ? 'itm-pe' : ''));
      kA(R, q.co, 'cot', 'title', isNum(r.ce_oi) ? n0(r.ce_oi) : '—'); kW(R, q.cb, 'cbw', oiW(r.ce_oi)); kT(R, q.ct, 'ct', oiT(r.ce_oi));
      kT(R, td[1], '1', isNum(r.ce_vol) ? n0(r.ce_vol) : '—'); kT(R, td[2], '2', n(r.ce_vwap));
      kA(R, td[3], '3t', 'title', 'bid ' + n(r.ce_bid) + ' · ask ' + n(r.ce_ask)); kT(R, td[3], '3', n(r.ce_ltp));
      kH(R, td[4], '4', mg + K(k) + (r.is_atm ? ' <span class="mg">ATM</span>' : ''));
      kA(R, td[5], '5t', 'title', 'bid ' + n(r.pe_bid) + ' · ask ' + n(r.pe_ask)); kT(R, td[5], '5', n(r.pe_ltp));
      kT(R, td[6], '6', n(r.pe_vwap)); kT(R, td[7], '7', isNum(r.pe_vol) ? n0(r.pe_vol) : '—');
      kA(R, q.po, 'pot', 'title', isNum(r.pe_oi) ? n0(r.pe_oi) : '—'); kW(R, q.pb, 'pbw', oiW(r.pe_oi)); kT(R, q.pt, 'pt', oiT(r.pe_oi));
    }
  }

  const UN = ktable('uni-body', 'tr', '<td class="b"></td><td class="r mut"></td><td class="r"></td><td class="r"></td><td class="r"><span data-q="fu"></span><span class="age hid" data-q="bm"></span></td><td class="r"></td><td class="r dn"></td><td class="r up"></td><td class="r"></td><td class="r"></td><td><span class="st" data-q="st"></span><span class="age hid" data-q="ag"></span></td><td></td><td class="r"></td><td class="r"></td>');
  function rUniverse(s) {
    const ins = s.instruments && typeof s.instruments === 'object' ? s.instruments : {}, rt = s.round_ts;
    const names = Object.keys(ins);
    txt('uni-n', names.length ? names.length + ' instruments' : '');
    if (!names.length) { ktEmpty(UN, '<tr><td colspan="14" class="mut">—</td></tr>'); return; }
    names.sort((a, b) => { if (a === 'NIFTY') return -1; if (b === 'NIFTY') return 1; const A = ins[a] || {}, B = ins[b] || {}; if (!!A.is_index !== !!B.is_index) return A.is_index ? -1 : 1; return (B.weight || 0) - (A.weight || 0) || a.localeCompare(b); });
    const prox = proxPts(s);
    const dc = d => !isNum(d) ? 'mut' : d < 0 ? 'bad b' : d <= prox ? 'warn b' : d <= prox * 3 ? 'warn' : '';
    const recs = ktSync(UN, names); if (!recs) return;
    for (let i = 0; i < names.length; i++) {
      const k = names[i], r = ins[k] || {}, R = recs[i], td = R.td, q = R.q;
      const hasL = isNum(r.lower) && isNum(r.upper);
      kC(R, R.el, 'row', (r.stale ? 'stale' : '') + (k === 'NIFTY' ? ' live' : ''));
      kH(R, td[0], '0', esc(k) + (r.is_index ? '' : ' <span class="st stock">STOCK</span>') + (r.stale ? ' <span class="st stale">STALE</span>' : ''));
      kT(R, td[1], '1', isNum(r.weight) ? n(r.weight, 2) : '—');
      kT(R, td[2], '2', isNum(r.spot) ? n(r.spot, r.spot >= 1000 ? 1 : 2) : '—');
      kC(R, td[3], '3c', 'r ' + sgnCls(r.delta)); kT(R, td[3], '3', isNum(r.delta) ? sg(r.delta, 2) : '—');
      kT(R, q.fu, 'fu', isNum(r.future) ? n(r.future, 1) : '—');
      const bm = r.basis_mode ? S(r.basis_mode).slice(0, 3).toLowerCase() : ''; kC(R, q.bm, 'bmc', bm ? 'age' : 'age hid'); kT(R, q.bm, 'bm', bm);
      kC(R, td[5], '5c', 'r ' + sgnCls(r.basis)); kT(R, td[5], '5', isNum(r.basis) ? sg(r.basis, 2) : '—');
      kT(R, td[6], '6', hasL ? n(r.lower, 1) : 'N/A'); kT(R, td[7], '7', hasL ? n(r.upper, 1) : 'N/A');
      kC(R, td[8], '8c', 'r ' + (hasL ? dc(r.dist_up) : 'mut')); kT(R, td[8], '8', hasL && isNum(r.dist_up) ? sg(r.dist_up, 1) : 'N/A');
      kC(R, td[9], '9c', 'r ' + (hasL ? dc(r.dist_dn) : 'mut')); kT(R, td[9], '9', hasL && isNum(r.dist_dn) ? sg(r.dist_dn, 1) : 'N/A');
      kC(R, q.st, 'stc', 'st ' + stCls(r.state)); kT(R, q.st, 'st', stTxt(r.state));
      const fresh = !r.stale && isNum(r.freshness_age_secs);
      kC(R, q.ag, 'agc', fresh ? 'age' : 'age hid');
      if (fresh) kClock(UN, R, q.ag, 'ag', 'fresh', isNum(rt) ? rt - r.freshness_age_secs * 1000 : NaN, r.freshness_age_secs); else kT(R, q.ag, 'ag', '');
      kC(R, td[11], '11c', isUp(r.dir) ? 'up' : isDn(r.dir) ? 'dn' : 'mut'); kT(R, td[11], '11', isUp(r.dir) ? '↑' : isDn(r.dir) ? '↓' : '·');
      kT(R, td[12], '12', isNum(r.strength) ? n(r.strength, 2) : '—');
      kC(R, td[13], '13c', 'r ' + (isNum(r.vol_ratio) && r.vol_ratio >= 2 ? 'warn' : '')); kT(R, td[13], '13', isNum(r.vol_ratio) ? n(r.vol_ratio, 1) + '×' : '—');
    }
    ktTick(UN, rt);
  }

  function rLevelHistory(s) {
    const lh = Array.isArray(s.level_history) ? s.level_history.slice().reverse() : [];
    txt('lvl-n', lh.length ? lh.length + ' recomputes' : '');
    if (!lh.length) { html('lvl-body', '<tr><td colspan="14" class="mut">no level calcs yet</td></tr>'); return; }
    let h = '';
    for (const r of lh) {
      if (!r) continue;
      const hw = isNum(r.upper) && isNum(r.lower) ? (r.upper - r.lower) / 2 : null;
      const lx = r.band_method === 'mtm' && isNum(r.legacy_upper) && isNum(r.legacy_lower);
      h += '<tr><td>' + hms(r.ts) + '</td><td class="mut">' + esc(r.trigger || '—') + '</td><td class="r">' + (isNum(r.anchor_spot) ? n(r.anchor_spot, 1) : '—') + '</td>'
        + '<td class="r dn b">' + (isNum(r.lower) ? n(r.lower, 1) : '—') + '</td><td class="r up b">' + (isNum(r.upper) ? n(r.upper, 1) : '—') + '</td>'
        + '<td class="r">' + (isNum(hw) ? n(hw, 1) : '—') + (r.narrowed ? '<span class="age">narrowed</span>' : '') + (lx ? '<span class="age" title="the 1.x (legacy) band would have been">1.x ±' + n((r.legacy_upper - r.legacy_lower) / 2, 1) + '</span>' : '') + '</td>'
        + '<td class="r">' + K(r.atm) + '</td><td class="r ce">' + K(r.k_ce) + '</td><td class="r">' + n(r.vwap_ce) + '</td>'
        + '<td class="r pe">' + K(r.k_pe) + '</td><td class="r">' + n(r.vwap_pe) + '</td>'
        + '<td class="r ' + sgnCls(r.basis) + '">' + (isNum(r.basis) ? sg(r.basis, 2) : '—') + '</td><td class="mut">' + esc(r.basis_mode || '—') + '</td><td class="mut">' + esc(r.band_method || '—') + '</td></tr>';
    }
    html('lvl-body', h);
  }

  // =================================================================================================
  function safe(fn, s, h) {
    try { fn(s, h); }
    catch (e) {
      const key = (fn.name || '?') + '|' + (e && e.message), now = Date.now();
      if (!_errAt[key] || now - _errAt[key] > 60000) { _errAt[key] = now; if (window.console) console.error('VAJRA_UI.' + (fn.name || '?'), e); }
    }
  }
  let _inTrade = null;
  function render(s, heavy) {
    if (!s || typeof s !== 'object') s = {};
    _last = s;
    const it = inTrade(s);
    if (it !== _inTrade) { try { document.body.classList.toggle('in-trade', it); _inTrade = it; } catch (e) { } }
    // HOT (every coalesced round): the glance-critical sections only — header chips, SCREAM, WHY-NO-TRADE,
    // band hero, scream panel, position, lean. All writes are change-gated; no table is touched.
    safe(rHeader, s); safe(rScream, s); safe(rWhyNoTrade, s); safe(rGuards, s); safe(rHero, s); safe(rScreamPanel, s); safe(rPosition, s, !!heavy); safe(rLean, s);
    // HEAVY (≤1 Hz, when a FULL frame arrived): keyed tables / lists — rows persist, only changed cells are written.
    if (heavy) { safe(rJournal, s); safe(rBurst, s); safe(rBreach, s); safe(rEvents, s); safe(rChain, s); safe(rUniverse, s); safe(rLevelHistory, s); }
  }
  function watchdog(st) {
    _wd = st && typeof st === 'object' ? st : { kind: 'ok', text: '', detail: '' };
    if (_last) safe(rScream, _last);
  }
  window.VAJRA_UI = { render, watchdog };
})();
