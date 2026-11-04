# 18 — Alert-sound policy owner: findings

Scope: every path in `vajra/dashboard.py` that can play audio (lines 454–474, 968–989), the engine state it
keys off (`vajra/orchestrator.py` `_compute_alarm` / `_update_view`), `vajra/sounds/*.mp3`, and a round-by-round
replay of the sound rules over the three recorded tapes (`scratch/18/sim_sounds.py`, `sister_bands.py`).

## 0. The numbers (what the dashboard WOULD have rung, as built, per recorded day)

| day | span | trades | approach rings | sister-breach rings | rings/hour | max rings in any 60 s | sister episodes < 5 s |
|---|---|---|---|---|---|---|---|
| 10-14 | 13:26–15:29 (2.05 h) | 1 | 1 (at 15:28, post-cutoff) | **28** (18 of them in the 15:15–15:25 frozen/garbage-feed window) | 15 | 15 | 3 |
| 10-15 | 09:41–15:29 (5.80 h) | 2 | 0 | **241** — ITC 109, BHARTIARTL 104, BANKBARODA 19 | **42** | 17 | 149 (median episode 3.0 s) |
| 10-16 | 11:11–15:25 (4.25 h) | 2 | 2 (both spurious, see F3) | **112** — BANKBARODA 67, BHARTIARTL 30, FEDERALBNK 8 | 28 | 13 | 57 (median 4.8 s) |

Total over three days: 5 entry sounds, 5 exit sounds, 3 approach sounds, **381 sister-breach sounds** (each one
the 4.0-second `sister_breach.mp3`, a wood-block sequence). The "ting ting ting" is >95 % the sister rule; the
approach sound is almost silent in practice (F2). The proposed `bloc STRONG` chime (|weighted net pull| ≥ 0.35,
edge-triggered, 5-min cooldown) would have rung **0 times** on all three days — i.e. a confluence chime would be
quiet rather than noisy, so it is safe to offer as an opt-in.

## 1. Correctness defects (most severe first)

### [HIGH] Sister-breach sound rings on every flicker of 23 jittery, 3-min re-anchored bands  (dashboard.py:976–981, orchestrator.py:359–361, 1132–1136)  certainty: confirmed-by-running
- What happens: `breach_alerts` is rebuilt every round from `"BREACH" in sig.state or "CONFIRM" in sig.state`;
  the JS rings for every instrument that enters the set (`nb.forEach(i=>{if(!_breached.has(i)) play})`). An
  episode ends the moment spot comes back inside `break_buffer` (0.03 % of spot = **Rs 0.07 for BANKBARODA**,
  **Rs 0.08 for ITC**), so a stock oscillating around its level produces one 4-second knock per crossing. 10-15:
  241 rings, median episode 3.0 s, 149 of 241 episodes shorter than 5 s; 73 of them began within 60 s of that
  instrument's re-anchor (the 3-min re-anchor re-centres the band on spot, clears the breach, and the next
  0.1-pt drift re-breaches it — exactly the "fresh breach → new ring" loop).
- Root causes compound: (a) `level_round_step: 5` is applied to EVERY instrument (`trigger_engine.py:84–87`) — for
  ITC at Rs 264 the 1-ITM VWAP ≈ 5 → `round(5.5, 5) = 5` → `upper = (ATM−5)+5 = ATM = lower`: **ITC's band on
  10-15 had half-width 0.00 all day** (`sister_bands.py`), so every tick beyond Rs 0.08 was a "breach" (109 rings).
  BANKBARODA's half-width is Rs 2.50 (1 rounding quantum) on a stock whose p95 tick move is Rs 0.12. (b) The
  break buffer is a flat 0.03 % with no tick-size floor. (c) No minimum episode duration, no per-instrument
  cooldown, no stale/N-A guard on the sound side beyond the `stale` flag.
- Why it matters: it is the operator's complaint verbatim; it also drowns the five sounds that matter (a sister
  knock and a profit chime fire into the same second — the exit at 10:08:19 on 10-15 sits inside a BHARTIARTL
  ring burst).
- Fix: remove the sister sound from the default set (policy §3B). If any bloc sound is kept, drive it from the
  weighted confluence (`absolute_net_pull`, already in `arming.confluence`), not from per-instrument set diffs.
  Independently (owner: band/levels reviewers): make `level_round_step` and `break_buffer` tick-size-aware for
  stocks (e.g. `round_step = max(strike_step/10, 2 ticks)`, `break_buffer = max(0.03 %, 3 ticks)`), and treat a
  band with `upper − lower < 2·round_step` as `ok=False` (MISSING_VWAP-class gap), never as a tradable/alertable band.

### [HIGH] The trade-entered sound depends on catching a single 251 ms window — and is lost when the operator alt-tabs to ICICI  (dashboard.py:974–975, orchestrator.py:582, 605, 1040)  certainty: confirmed-by-reading + tape
- What happens: `self._entry_block = None` is reset at the top of every round and is `"ENTERED"` only on the
  round that fired; `arming.entry_block` is therefore `"ENTERED"` for exactly one round (measured 251–282 ms on
  all 5 recorded entries). The JS keys the trade sound on `a.entry_block==='ENTERED' && p`. The poll is
  `setInterval(tick, 250)` with an un-awaited fetch, so in a foreground tab it catches the window most of the time;
  but Chrome/Firefox throttle background-tab timers to **1/s** (and Chrome's intensive throttling to 1/min after
  5 min hidden). The operator's documented workflow is to switch to the ICICI terminal to mirror the order
  (OPERATOR_MANUAL §2.4) — exactly then the one sound he needs most has a ~75 % chance of never playing, and
  the `ENTERED` badge is never shown either. Exit sounds are immune (journal-based, F-ok).
- Fix: key the trade sound on the journal (`(s.journal||[]).find(t=>t.status!=='CLOSED')?.entry_ts`), or —
  the real fix — the backend `sound_events` queue (§3B.6), which is cursor-based and cannot drop an event.

### [MEDIUM] Spurious approach ring on every exit round: the alarm evaluates the OLD band after going flat  (orchestrator.py:649, 670, 981–1032)  certainty: confirmed-by-running
- What happens: on the exit round `_recompute_nifty_band` replaces `st.band`, but `_compute_alarm(round_ts,
  nifty_band, …)` is still called with the pre-exit local `nifty_band`. `in_trade` is now False, spot is at/through
  the old level (a TRAIL_SL exit means the move extended), so state flips `IN_TRADE → BREACHED` for one round →
  `side` edge → `approach_upper/lower.mp3` plays on top of `profit.mp3`. Both approach rings on 10-16 (15:22:17
  and 15:25:00) are exactly this; next round the new band makes it `WATCHING` again.
- Fix: `alarm = self._compute_alarm(round_ts, self.states["NIFTY"].band, …)` (use the refreshed band), and in the
  sound layer never arm the approach episode on a round where a trade closed.

### [MEDIUM] The "advance warning" approach sound structurally cannot warn in advance  (dashboard.py:970–973, orchestrator.py:554–609, 1000–1007)  certainty: confirmed-by-running
- What happens: entry is instantaneous on the first round inside the 7-pt zone (no latch, no veto), and
  `executor.enter` runs BEFORE `_compute_alarm`, so that round's state is `IN_TRADE` (side = null). The approach
  sound therefore plays only for NON-tradeable approaches: pre-09:21, inside cooldowns, premium/capital rejects,
  and 15:25–15:30 after the entry cutoff (10-14's only ring: 15:28:55, 4 pts above the lower level, no trade
  possible). Zero of the three days' five real entries were preceded by an approach sound. README.md:7–8
  describes a sound that does not exist in practice.
- Fix (policy §3B.1): make the approach episode real — ring at `dist ≤ prox + lead` (lead = 8 pts, i.e. 15 pts
  out) while flat and tradeable, once per episode with hysteresis; or accept that with instant entry the trade
  sound IS the warning and drop the two approach files.

### [MEDIUM] Hysteresis-free side edge: `_lastSide` clears on every non-approach tick  (dashboard.py:972–973)  certainty: confirmed-by-reading
- `side` becomes null whenever `state ∉ {ABOUT_TO_BREACH, BREACHED}`; one tick at 7.2 pts then 6.8 pts = two
  rings; UP→DOWN flip on a narrow expiry-day band (±25–35 pts, zone 7) rings again. The recorded non-expiry days
  barely exercised it (band ±90–130 pts), but on an expiry day with spot hugging a level this is the ting-ting.
  Fix: §3B.1 hysteresis (release at prox+8 = 15 pts) + 120 s re-arm; the simulation with h8/r120 keeps the
  3 genuine episodes and removes nothing legitimate.

### [MEDIUM] No sound for the failures that matter most: frozen feed, engine dead, exit rejected, levels stale  (dashboard.py:481–508, 731–734)  certainty: confirmed-by-reading
- `applyWatchdog` turns the page red and dims it but is silent; `FEED_GAP`/`EXIT_REJECTED` during a trade add a
  🚨 prefix, silently. All three recorded 15:15–15:20 cash-feed freezes (lead's A2) would have passed without a
  sound, while 18 sister knocks (10-14) rang on the garbage ticks that followed. Fix: `feed_problem` sound,
  edge-triggered on (frozen ≥ 5 s | DATA FROZEN | EXIT_REJECTED | spot-vs-implied-future implausible), 60 s cooldown,
  and it must be the one sound exempt from quiet mode.

### [LOW] Overlap, loudness and length of the six clips violate the README's own 0.3–1.0 s rule  (vajra/sounds/README.md:22; ffprobe)  certainty: confirmed-by-running
| file | length | audible | integrated loudness | note |
|---|---|---|---|---|
| approach_lower.mp3 | 2.42 s | 2.15 s | −10.6 LUFS | loudest file; 48 kHz stereo 256 kbps |
| approach_upper.mp3 | 2.85 s | 2.04 s | −14.3 LUFS | 0.8 s trailing silence |
| trade_taken.mp3 | 4.99 s | starts after **0.73 s of silence** | −18.0 LUFS | quietest, and the one used as the unlock/confirm ping (dashboard.py:470) — the click appears to do nothing for ¾ s |
| sister_breach.mp3 | 4.02 s | 2.35 s | −16.0 LUFS, LRA 13 | a 4-second multi-knock for a 3-second event |
| profit.mp3 | 3.50 s | 3.18 s | −11.6 LUFS, peak −0.9 dBFS | |
| stoploss.mp3 | 6.03 s | 5.01 s | −12.7 LUFS | 6 s of "miss" after a loss |
- 7.4 LU spread between files at a single `volume=0.85`; `playSnd` restarts the same element (`currentTime=0`) so
  a 4 s clip retriggered every 3 s never finishes, and different elements overlap freely (approach + trade +
  sister + profit can all sound in one second). Fix: §3C sound set (0.3–1.0 s, peak-normalised), per-kind
  volume, and a global 1-sound-per-1.5 s gate.

### [LOW] Poll responses can arrive out of order; no round_ts monotonic guard  (dashboard.py:638–641)  certainty: plausible
- Two in-flight fetches (250 ms interval, no await) can resolve newest-first; the sound block then sees a side
  edge or breach-set change that never happened. Fix: `if (s.round_ts != null && _wdLastRts != null && s.round_ts < _wdLastRts) return;` — or the cursor-based queue, which is order-safe by construction.

### [LOW] Schema enum lacks `post_cooldown`  (schema.py:25, orchestrator.py:565, 573)  certainty: confirmed-by-running (level_tape 10-16 contains it)
- Already noted by the lead (A3); listed because the sound-events tape (§3B.6) must not repeat the mistake:
  declare every `sound_kind` in `schema.py` and let `TapeWriter.event()` enforce it.

## 2. Improvements / design proposals (ranked)
1. **Backend decides WHEN, frontend only plays** — a `sound_events` ring buffer in `latest_state` with monotonic
   `seq`, drained by cursor (§3B.6). Removes every edge/diff/aliasing bug above in one move, makes the sounds
   replayable from tape, and survives background-tab throttling.
2. Shrink the default set to five sounds (§3C); sister breach and bloc confluence become opt-in.
3. Rate limiting as a first-class policy object in `vajra_config.yaml` (`sounds:` block, §3B.3), not JS constants.
4. Quiet mode / mute-until-time / test-sounds button (§3B.4–5).
5. Fix the stock band quantisation (ITC half-width 0) — not a sound bug, but it is what the sound exposed.

## 3. Recommendations specific to the operator asks

### A (non-expiry levels) — one sound-relevant note only
Whatever narrowing rule the band owners choose, the approach lead distance in §3B.1 must scale with it: use
`lead = max(8, 0.1 × half_width)` so a 100-pt band warns at ~17 pts out and a 30-pt expiry band at 15.

### B — THE VAJRA 2.0 SOUND POLICY

**B.1 Events that deserve a sound (default ON), and the exact trigger**
| kind | trigger (backend) | once-per | re-arm / cooldown | volume |
|---|---|---|---|---|
| `TRADE_ENTERED` | order_tape ENTRY row written | trade | none needed | 1.00 |
| `EXIT_PROFIT` | EXIT row with pnl_realized ≥ 0 (TRAIL_SL, green flatten) | trade | none | 0.90 |
| `EXIT_LOSS` | EXIT row with pnl_realized < 0 (SL_HIT, red flatten) | trade | none | 0.90 |
| `NIFTY_APPROACH` | flat AND tradeable (in session, no cooldown, gate ok) AND `dist ≤ prox + lead` (7+8 = 15 pts) toward one side | episode | episode ends only when `dist > prox + 2·lead` (23 pts) OR side flips; **min 120 s between approach rings**; never on an exit round | 0.70 |
| `FEED_PROBLEM` | edge of: no NIFTY round for ≥ 5 s · spot unchanged ≥ 30 s while implied future moves · spot-vs-implied-future jump > 60 pts in one round · EXIT_REJECTED · DATA FROZEN watchdog | episode | 60 s cooldown; **exempt from quiet mode and the global gate** | 1.00 |

Default OFF (opt-in toggles): `BLOC_STRONG` (|absolute_net_pull| crosses ±0.35, edge on sign, 300 s cooldown —
0 rings on all three tapes, so it is cheap to offer), `SISTER_BREACH` (if ever re-enabled: only for instruments
with weight ≥ 0.03 i.e. BANKNIFTY/FINNIFTY/HDFCBANK/ICICIBANK/RELIANCE/BHARTIARTL/LT/INFY/SBIN/AXISBANK,
episode must last ≥ 10 s before it may ring, per-instrument cooldown 300 s, global cap 6/hour). Never a sound for:
WATCHING/STAND_BY transitions, cooldown start/end (the violet banner is enough), level recomputes, REVERSAL_WATCH,
FALSE_BREAK, session close.

**B.2 Hysteresis for the approach episode** — arm at 15 pts, release at 23 pts or on side flip; while in a trade
the episode is suspended, and the first round after an exit is ignored (F3). Simulated on the tapes (h8/r120):
10-14 1 ring, 10-15 0, 10-16 2 → 1 once the exit-round artefact is removed. Nothing legitimate is lost.

**B.3 Rate limits** — global: **max 1 sound per 1.5 s** (later events queue, duplicates of the same kind collapse);
per kind: approach 120 s, feed 60 s, bloc 300 s, sister 300 s/instrument; hard ceiling **12 sounds/hour** for
everything except TRADE_ENTERED/EXIT_*/FEED_PROBLEM, which are never suppressed. On the recorded days this policy
yields 10-14: 3 sounds, 10-15: 4, 10-16: 5 (vs 31 / 245 / 118 as built).

**B.4 Quiet mode & mute-until** — header pill cycles `🔔 all → 🔕 trades-only → 🔇 off`; "trades-only" = TRADE_ENTERED,
EXIT_*, FEED_PROBLEM only. Right-click / long-press the pill → "mute until HH:MM" (presets: 5 m, 15 m, until 15:25)
stored in `localStorage` (`vajra_mute_until` epoch ms) and shown as a countdown in the pill; FEED_PROBLEM still
plays. Pre-09:20 and after 15:30 the page is silent except FEED_PROBLEM.

**B.5 Test-sounds button** — a `🎧 test` control next to the pill plays the five default sounds in sequence 1.5 s
apart (and the two optional ones if enabled), lights each name as it plays, and also serves as the audio unlock
(replacing the 5-second `trade_taken` ping at dashboard.py:470 — use the 0.38 s `trade_entered` instead). Also
exposes a backend `GET /api/sound_test` that enqueues a synthetic `TEST` event so the queue path itself is exercised.

**B.6 Backend-driven `sound_events` queue (the design)**
```python
# orchestrator.py — one place decides WHEN; deterministic, replayable, taped
@dataclass
class SoundEvent: seq:int; ts:int; kind:str; detail:str; priority:int   # priority 0 = never suppress

class SoundPolicy:
    KINDS = {"TRADE_ENTERED":0, "EXIT_PROFIT":0, "EXIT_LOSS":0, "FEED_PROBLEM":0,
             "NIFTY_APPROACH":1, "BLOC_STRONG":2, "SISTER_BREACH":2, "TEST":1}
    def __init__(self, cfg):  # cooldowns/limits from vajra_config.yaml `sounds:`; ring buffer maxlen 64
        ...
    def offer(self, ts, kind, detail):   # returns True if enqueued (applies per-kind cooldown, hourly cap, global 1.5 s gate)
        ...
# emit sites: _emit_orders (ENTRY/EXIT), approach-episode tracker in process_round (hysteresis lives HERE,
# not in JS), feed watchdog (new, backend-side), confluence sign edge. Every enqueued event is ALSO written to
# event_tape as event_type "SOUND" (declare in schema.EVENT_TYPES) so a replay reproduces the day's audio log.
latest_state["sound_events"] = list(self._sounds.buffer)          # [{seq, ts, kind, detail}], newest last
latest_state["sound_seq"]    = self._sounds.seq                   # monotonic
```
```js
// dashboard.py JS — the frontend has NO trading logic left; it drains a cursor
let _sndCursor = Number(localStorage.getItem('vajra_snd_cursor')||0), _lastPlay=0;
function drainSounds(s){
  const evs=(s.sound_events||[]).filter(e=>e.seq>_sndCursor);
  if(!evs.length) return;
  if(_sndCursor===0 && evs.length) { _sndCursor=evs[evs.length-1].seq; save(); return; }  // fresh load: no retro burst
  for(const e of evs){ _sndCursor=e.seq; if(muted(e.kind)) continue;
    const gap=Date.now()-_lastPlay; if(gap<1500 && e.priority>0) continue;             // global gate
    playSnd(SND[e.kind], VOL[e.kind]); _lastPlay=Date.now(); }
  save();
}
```
Properties: idempotent under re-polls and reorders (seq), no event lost under 1 Hz throttling (buffer holds 64 ≈
minutes of events), a page reload does not replay history, and the tape (`SOUND` events) is the audit of what rang.

**B.7 Config block (proposed)**
```yaml
sounds:
  enabled: true
  default_kinds: [TRADE_ENTERED, EXIT_PROFIT, EXIT_LOSS, NIFTY_APPROACH, FEED_PROBLEM]
  optional_kinds: {BLOC_STRONG: false, SISTER_BREACH: false}
  global_min_gap_secs: 1.5
  hourly_cap_nonpriority: 12
  approach: {lead_pts: 8, release_extra_pts: 8, rearm_secs: 120}
  feed_problem: {cooldown_secs: 60, frozen_after_secs: 5, spot_jump_vs_implied_pts: 60}
  bloc_strong: {pull_threshold: 0.35, cooldown_secs: 300}
  sister: {min_weight: 0.03, min_episode_secs: 10, per_instrument_cooldown_secs: 300, hourly_cap: 6}
  volume: {TRADE_ENTERED: 1.0, EXIT_PROFIT: 0.9, EXIT_LOSS: 0.9, NIFTY_APPROACH: 0.7, FEED_PROBLEM: 1.0, BLOC_STRONG: 0.6, SISTER_BREACH: 0.5}
```

### C — Final sound set, keep/retire, download plan
| file (new name) | character | length | source |
|---|---|---|---|
| `trade_entered.mp3` | crisp two-note rising "go" (marimba/glass) | 0.3–0.5 s | **retire `trade_taken.mp3`** (5 s, 0.73 s silent lead-in) |
| `exit_profit.mp3` | short 3-note major arpeggio / coin | 0.5–0.7 s | **retire `profit.mp3`** (3.5 s) or trim to its first 0.7 s with `ffmpeg -t 0.7 -af afade=out:st=0.55:d=0.15` |
| `exit_loss.mp3` | single low dull thud, no melody | 0.3–0.5 s | **retire `stoploss.mp3`** (6 s) |
| `nifty_approach.mp3` | two even mid taps — urgent but neutral (one file for both sides; the banner already shows ▲/▼; the up/down pair added nothing but two more loud files) | 0.3–0.4 s | **retire `approach_upper.mp3` / `approach_lower.mp3`** |
| `feed_problem.mp3` | dissonant beating drone / low buzzer, unmistakably "not a market sound" | 0.8–1.0 s | new |
| `bloc_strong.mp3` (optional) | soft three-note chime | 0.5–0.6 s | new, opt-in |
| `sister_breach.mp3` | — | — | **retire** (opt-in only; if kept, trim to 0.6 s) |
All files: mono, 44.1 kHz, peak-normalised to −3 dBFS, integrated loudness within ±2 LU of each other, ≤ 1.0 s.

Download plan (CC0 / no-attribution), in order of preference:
1. **Mixkit** (mixkit.co/free-sound-effects/notification, /game, /interface) — Mixkit License, direct .wav, no signup;
   good "correct answer", "software interface" and "message pop" one-shots.
2. **Pixabay Sound Effects** (pixabay.com/sound-effects/search/notification, …/error, …/coin) — Pixabay Content
   License; filter duration < 1 s.
3. **Freesound** (freesound.org) with the licence filter set to **Creative Commons 0** only; search "ui blip", "marimba
   hit", "thud", "alarm buzzer short".
4. **Fallback: generate programmatically** — `scratch/18/gen_tones.py` (numpy, stdlib `wave`) already produces the
   six WAVs at 0.38–0.90 s, peak −3 dBFS, no network needed; convert with `ffmpeg -i x.wav -b:a 96k x.mp3` or serve
   the WAVs directly (`StaticFiles` already serves any extension; just change the names in `SND`).
Acceptance check before dropping into `vajra/sounds/`: `ffprobe` duration ≤ 1.0 s; `ffmpeg -af ebur128` within
±2 LU of the set; the five default files must be distinguishable with eyes closed in the `🎧 test` sequence.

## 4. Things that are fine and should NOT be changed
- Exit sounds keyed on the journal (`dashboard.py:983–988`) — robust, once per trade, survives throttling; keep the
  pattern (it is the model for the queue).
- The "prime on first load, no retro sound" guard (`_lastExit===null || exit_ts<_lastExit`) — keep in the cursor design.
- `soundOn` persisted in `localStorage` and the amber pulsing pill when muted during market hours — good UX, keep.
- Sister/stock breaches stay on the burst board and breach-alerts panel VISUALLY; only the audio goes.
- The silent post-profit cooldown and the non-audible violet post-stop banner — the operator asked for silence there.

## 5. Tests I would add
1. `test_sound_policy_rate_limits`: feed 50 NIFTY_APPROACH offers 250 ms apart → exactly 1 enqueued; a second after
   120 s → 2; TRADE_ENTERED inside the 1.5 s gate is never suppressed.
2. `test_approach_episode_hysteresis`: spot path 20→14→16→14→26→14 pts from upper → rings at 14 (first), not at
   the 16→14 re-entry, rings again only after >23 pts and ≥120 s.
3. `test_no_approach_sound_on_exit_round`: replay the 10-16 15:22:17 exit through `process_round` → `sound_events`
   contains EXIT_PROFIT only.
4. `test_trade_entered_event_survives_slow_poll`: enqueue ENTRY, advance 20 rounds, assert the event is still in
   the buffer with its seq (the one-round `entry_block` window is gone).
5. `test_sound_events_are_taped_and_declared`: every enqueued kind appears in event_tape as `SOUND` and
   `TapeWriter.event` rejects an undeclared kind; replay of a recorded day reproduces the identical SOUND sequence.
6. `test_degenerate_stock_band_is_not_alertable`: ITC-like VWAP 5 / step 5 → `compute_band(...).ok is False`
   (or the sister policy refuses an instrument whose half-width < 2 round steps).
7. `test_feed_problem_sound_on_frozen_spot`: 30 s of identical NIFTY spot with a moving implied future → one
   FEED_PROBLEM event, none for the next 60 s, and it is enqueued even in quiet mode.

Scripts: `/home/claude/vajra2_review/scratch/18/sim_sounds.py` (per-round sound replay + policy counts, output
`sim_sounds_out.json`), `sister_bands.py` (band half-width vs tick jitter), `gen_tones.py` (numpy fallback set in
`scratch/18/tones/`).
