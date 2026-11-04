# 04 — Alert sound system (SoundBus / ApproachEpisode / Web Audio player): teardown
Verdict: Mostly ready for Monday and clearly better than 1.0: the constant "ting-ting" is gone (8 / 29 / 13 cues a day). One HIGH bug remains: after an engine restart, if the page is not reloaded, the first trade cue is silently lost. Fix it before Monday (about 5 lines).
Score (your area): 1.0 = 3/10, 2.0 = 7/10

Method: I replayed all three recorded days through the 2.0 engine. To fit the box's memory and CPU limits I streamed only the NIFTY rows (sisters and stocks feed only the advisory confluence; gates and sizing never read them, `execution.py:170-178,184`). `SoundBus.emit` was monkey-patched to log every request, whether accepted or suppressed, along with every ENTRY/EXIT order row. Scripts are in `/home/claude/teardown/scratch/04/`. The 15 Oct 09:41–09:58 window matches the CHANGELOG's own Chromium claim exactly: 8 cues. A full-universe cross-check of the same window was OOM-killed on the shared box, so NIFTY-only equivalence rests on reading the code, not on a reproduction. I tested the browser player in headless Chromium (Playwright) against the real `build_app()` HTML/SSE, fed by a fake `latest_state`.

## Findings (most severe first)

### [HIGH] Engine restart without a page reload swallows the first batch of sound events (often approach plus ENTRY) (dashboard.py:576-578) — reproduced
- `consumeSounds` treats `maxSeq < _sndSeq` as "engine restarted" and primes the cursor **without playing anything**. A restarted engine starts at seq=1. The page's old cursor (e.g. 8) survives because EventSource reconnects by itself, so the operator has no reason to reload. The new engine's first emit (seq 1–2, which is approach+entry in the same round, see below) is therefore dropped. The next event plays. Mid-day restarts are a documented flow (`run_vajra.ps1 -SkipVerify`).
- Playwright (`pw_sounds.py`, scenario A3/A4):
  ```
  A3 after ENGINE RESTART, first approach+entry -> plays: []  cursor= 2
  A4 next event (exit_loss) -> plays: ['exit_loss(webaudio)']
  ```
  The operator, who mirrors trades by hand, hears the **exit of a trade he never heard enter**.
- Fix: put a per-process `boot_id` (e.g. `uuid4().hex` at Orchestrator init) into `latest_state`. In `consumeSounds`, if `s.boot_id !== _bootId`, set `_bootId = s.boot_id`. On first page load prime as today; on a boot change, set `_sndSeq = 0` so events ≤ 20 s old play. Never use `maxSeq < cursor` as the restart test.

### [MEDIUM] "approach" cue gives zero lead time; it duplicates the entry cue or rings when no trade can follow (orchestrator.py:678-683 vs 711-723) — reproduced
- The 2.0 entry has no arming latch and fires on the **same round** that spot comes within 7 pts (`_approach_entry`). The approach cue is emitted from the same `entry` dict just before. Replay results: **15 of 16 approach cues have the exact same ms timestamp as an ENTRY.** The only exception (15 Oct 13:45:45, "upper level 23055 (7 pts)") rang 5 min into the 11-min post-stop cooldown, with no trade possible.
  ```
  13:40:50 exit_loss  EXIT SL_HIT -22,873
  13:45:45 approach   NIFTY upper level 23055 (7 pts)     <- gate POST_STOP_COOLDOWN, no entry follows
  ```
- In "all cues" mode every trade therefore sounds as tick+marimba back-to-back (≈0.3 s + 120 ms gap). The only approach that carries information is one that **cannot** lead to a trade. That is exactly the "unnecessary sound" that rule 8 forbids. A 1.0 user who had sound on is migrated to `'all'`, not to `'trades'` (`dashboard.py:537-538`), so this is the operator's default.
- The emit condition also ignores the cooldown, `LEVELS_STALE` and expiry gates. It checks only `health_ok` and `!position_active`.
- Fix: either (a) emit the approach only when `gate is None` would hold apart from proximity, AND at a wider radius (e.g. `prox + 8 pts`), so it is a real advance warning; or (b) drop it and default 1.0 migrants to `'trades'`. At minimum, skip the approach on rounds where `fire` is true or a cooldown gate is closed.

### [MEDIUM] Paper exits booked by `finalize()` (Ctrl-C / run-loop crash / stop) make no sound (orchestrator.py:961-964) — reproduced
- `finalize → executor.force_flatten` books an END_OF_SESSION exit, but there is no `sounds.emit` on that path. In my 09:15–10:00 window replay there were 3 EXIT rows and only 2 exit cues; the third was `09:59:59 EXIT END_OF_SESSION +10,647`, which was silent. In live, this exit also happens while the process is going away, so the operator gets neither a cue nor a final dashboard paint.
- Full-day replays: no day ended with an open position (14 Oct closed at 15:25 by EXPIRY_FLATTEN, which *does* sound), so this only bites on an abnormal stop.
- Fix: emit `exit_profit/exit_loss` from `finalize()` and print a loud stderr line "PAPER POSITION FLATTENED AT SHUTDOWN: …".

### [LOW] Player queue cap of 4 drops cues: shift-click audition never plays feed_alarm (dashboard.py:555-556) — reproduced
- `if(_q.length<4) _q.push(k)`: the first cue is shifted into playback, then 4 are queued and the 6th is dropped.
  ```
  A6 shift-click audition (6 cues requested) -> plays: [entry, approach_UP, approach_DOWN, exit_profit, exit_loss]   (no feed_alarm)
  A7 6 trade cues in one round -> plays: 5
  ```
  The README promises "shift-click to hear all five cues". In real trading, more than 4 queued cues within about 1 s does not occur (maximum seen: 2 per round), so the only practical impact is the audition.
- Fix: exempt `force` from the cap, or make the cap 8.

### [LOW] Clicking the pill always plays the *entry* cue as a confirmation (dashboard.py:568)
- off→trades and trades→all both play `entry.mp3`. For an operator who mirrors trades on the entry cue, a test sound identical to the real one is a trap. Fix: confirm with a neutral cue (e.g. approach_UP), or show text only.

### [LOW] Events missed while locked or disconnected are lost without any notice (dashboard.py:543-551, 579-583) — reproduced/plausible
- After a reload with the mode remembered, the AudioContext is `suspended` and the pill correctly shows `🔒 sound LOCKED — click` (B1: reproduced). This is a real improvement over 1.0. An event that arrives while locked goes to the HTMLAudio fallback. Headless Chromium let that play (B2), but desktop Chrome/Edge without a gesture will reject it (plausible: the autoplay MEI never builds up for sub-second cues). The cursor then advances, so the cue is gone for good. The same happens to any cue more than 20 s old after an SSE gap.
- Fix: keep a "missed cues" counter or a badge on the pill (e.g. `🔒 LOCKED — 1 missed: ENTRY 09:45:55`). On unlock, play the newest missed trade cue if it is ≤ 60 s old.

### [LOW] Background-tab behaviour is fine but depends on browser tab-sleeping (plausible)
- Sound consumption happens in `_onState`, driven by SSE events and not by rAF (`dashboard.py:1117`), and Web Audio keeps playing in hidden tabs. Timer clamping to 1 s in hidden tabs only delays queue draining (the 120 ms gap and the 1500 ms safety timer). This is correct and much better than 1.0's 250 ms `setInterval` poll, which throttles to 1/s (or 1/min) in background tabs.
- Residual risk: Edge "sleeping tabs" (default 2 h) and Chrome Memory Saver can discard or freeze a background tab that is not *currently* playing audio, which means no sounds at all. Fix: tell the operator in RUN_LIVE to exclude `localhost:8766` from sleeping tabs / Memory Saver.

### [INFO / fine] Ring of 30 cannot overflow; 20 s staleness skip works; refresh never replays
- The busiest round emitted 2 events, the busiest minute 3, and 29 per day at most. A 30-slot ring is ample (overflow would need 30 cues between two consumed states). A5: an event 21 s old was skipped (reproduced). A1: a page load with 3 recent events in the ring played nothing (prime works, no duplicates on refresh). SSE reconnect keeps the cursor, so there are no duplicates on reconnect.
- Trade cues bypass the cooldown and the global gap (`alerts.py:48-56`), so **no ENTRY or EXIT in the full-day replays went without a cue** (entries 2/9/4 = entry cues 2/9/4; exits 2/9/4 = exit cues 2/9/4). Only the finalize path (above) misses one.
- `exit_profit` is chosen on `pnl >= 0`, so a zero P&L exit chimes "green". This is trivial.

## Simulated sounds per recorded day (2.0 engine, full-day NIFTY-stream replay)
| day | approach | entry | exit_profit | exit_loss | feed_alarm (suppressed) | total audible ("all") | "trades only" |
|---|---|---|---|---|---|---|---|
| Wed 14 Oct | 2 | 2 | 0 | 2 | 2 (2) | 8 | 6 |
| Thu 15 Oct | 10 | 9 | 8 | 1 | 1 | 29 | 19 |
| Fri 16 Oct | 4 | 4 | 1 | 3 | 1 | 13 | 9 |
- The feed alarm rang every day at 15:15–15:16 (the known Breeze cash-feed freeze). On 14 Oct a position was open, so it was useful there. On 15/16 Oct the engine was flat, so it was informative but not actionable. No feed alarm fired before 15:15 (no pre-open false alarm in the tapes).
- 1.0 for comparison: the CHANGELOG claims the sister sound alone rang 28/241/112 times a day. **Not verified by me** (it needs a browser-diff replay of 1.0). From the code, that magnitude is plausible: `dashboard.py(1.0):976-980` rings on every new member of `breach_alerts`, and flicker re-arms it.

## What 2.0 does better than 1.0 (evidence)
- The engine decides when a sound is due and the page plays each `seq` once. 1.0 diffed the state in the browser on a 250 ms poll. 1.0's ENTRY cue keyed on `a.entry_block==='ENTERED'`, which is true for **one engine round** only (`1.0 dashboard.py:973-974`), so a slow repaint or a throttled background tab missed it. That explains "sounds never played the way he wanted".
- Sister/stock sounds are removed entirely; the counts above are 8–29 a day.
- Web Audio with a no-overlap queue. 1.0 did `a.currentTime=0; a.play()` on shared elements, so cues cut each other off.
- The pill shows LOCKED honestly (1.0 said "sound on" while blocked) and offers 3 modes plus per-kind config switches.
- Files: the new cues are 0.30–0.94 s, mono 44.1 kHz at about 128 kbps, peaks −2.7…−6.6 dBFS, onset at 0–1 ms. The old ones were 2.4–6.0 s long; `trade_taken.mp3` has **735 ms of leading silence** and peaks reach −0.5 dBFS, with a 13 LU loudness spread (sister_breach). (ffprobe/ebur128 output in "Tests".) The approach ticks are very short (≈190 ms audible, tau 45 ms). They are quiet but distinct, and acceptable.

## What 2.0 does worse than 1.0 (evidence)
- Engine restart: 1.0's journal-based exit cue primed on `exit_ts` and would not lose a trade cue the same way. 2.0's `maxSeq<cursor` prime is a new way to lose cues (HIGH above).
- The approach cue lost its lead time. 1.0 rang on ABOUT_TO_BREACH/BREACHED state (a linger band, earlier than the entry). 2.0 rings at the entry instant.

## Tests / checks you ran
- `python3 -B scratch/04/snd_replay.py --day <d> --out day_<d> --nifty-only` for 14/15/16 Oct (139 / 190 / 89 s). Results summarised in the table, raw logs in `scratch/04/day_<d>/sounds_<d>.json`.
- Window replay 15 Oct 09:15–10:00: `approach 3, entry 3, exit_profit 2; orders ENTRY 3 EXIT 3` (the third exit is a silent END_OF_SESSION).
- Playwright `scratch/04/pw_sounds.py` against `fake_server.py` (real `build_app`, port 8904). Outputs:
  ```
  A1 load with 3 recent events in ring -> plays: []          | pill: 🔔 all cues
  A2 same-round approach+entry -> plays: ['approach_UP','entry']
  A3 after ENGINE RESTART -> plays: []  cursor= 2
  A5 event 21 s old -> plays: []
  A6 shift-click audition -> 5 of 6 (no feed_alarm)
  B1 reload, no gesture -> pill: 🔒 sound LOCKED — click  ctx: suspended
  B3 after a click anywhere -> ctx: running  pill: 🔔 trades only
  ```
- `ffprobe` and `ffmpeg ebur128` / onset scan on all 6 new and 6 old mp3s, e.g. `entry.mp3 0.65 s mono 44.1k peak −4.8 dBFS onset 0 ms`; `trade_taken.mp3 (1.0) 4.99 s onset 735 ms`; `stoploss.mp3 (1.0) 6.03 s`.
