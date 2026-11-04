# 20 — Sound-asset curator: findings

Scope: the six MP3s in `vajra/sounds/`, `sounds/README.md`, and the dashboard audio JS (`vajra/dashboard.py:454-474`, `968-989`). Tools: ffprobe/ffmpeg 6.1 (ebur128, volumedetect), numpy/scipy. Originals copied to `scratch/20/old/` and never modified. Not done: I could not listen to anything, so timbre claims for the old files come from spectrograms (`scratch/20/old_overview.png`). The new cues are verified by measurement only, and the operator should audition them. I did not replay the tapes to count how often each sound would have fired.

## 1. Defects

### [HIGH] Every old file is a multi-second jingle, 2.4-6.0 s, against the README's own 0.3-1.0 s spec  (confirmed by running)
- The old files are 2.4 to 6.0 s long. A repeated "ting" is *inside* the files as well as across events: onset detection finds 4 onsets in `approach_lower`, 3 in `approach_upper`, 7 in `stoploss`. `sister_breach` is a single hit with a 3.9 s ring-out.
- `trade_taken.mp3` has **0.73 s of digital silence** before the first sound. Entry feedback is delayed by about 0.75 s on top of the poll interval.
- `stoploss.mp3` is 6 s long and sounds like a novelty "wah-wah" jingle (inferred from its spectrogram). It plays at the worst moment, on a loss.
- Fix: retire all six. Replacement set in section 3.

### [HIGH] Loudness is uneven and hot  (confirmed by running)
| old file | dur s | sr/ch | integrated LUFS | true peak dBFS | onsets | audible content ends | verdict |
|---|---|---|---|---|---|---|---|
| approach_upper.mp3 | 2.85 | 44.1k/2 | -14.3 | -3.4 | 3 | ~2.0 s | RETIRE |
| approach_lower.mp3 | 2.42 | 48k/2 | **-10.6** | -3.2 | 4 | ~2.1 s | RETIRE (loudest) |
| trade_taken.mp3 | 4.94 | 44.1k/2 | -18.0 | -3.6 | 1 (at 0.73 s) | ~4.4 s | RETIRE |
| sister_breach.mp3 | 3.98 | 44.1k/1 | -16.0 | **-0.5** | 1 (3.9 s ring-out) | ~2.5 s | RETIRE |
| profit.mp3 | 3.50 | 24k/2 | -11.6 | **-0.9** | 2 | ~3.0 s | RETIRE |
| stoploss.mp3 | 6.03 | 44.1k/2 | -12.7 | **-0.8** | 7 | ~5.1 s | RETIRE |
- The spread is 7.4 LU, from -10.6 to -18.0 LUFS. The dashboard then multiplies by a fixed `volume=0.85`, so the loudest cue (`approach_lower`) is the one fired most often on a down-approach.
- Four files peak above -1 dBFS. After MP3 decode they sit within about 1 dB of clipping, and nothing is normalised.
- Sample rates are mixed (24k, 44.1k, 48k) and so are channel counts (mono and stereo). `profit.mp3` is 24 kHz and so band-limited.
- Provenance:
  - `trade_taken.mp3` carries ID3 tags `www.voicy.network` (title/artist/copyright), a meme-soundboard site with **no licence evidence**.
  - The other five have no tags and no provenance. Treat all six as licence-unknown.
  - The README claims "free, no attribution needed" but names Mixkit/Pixabay, whose licences are not CC0 (they are their own "free licences").

### [HIGH] No audible alarm for feed problems  (confirmed by reading, `dashboard.py:968-989`)
- `FEED_GAP`, `EXIT_REJECTED` and the DATA FROZEN watchdog (`applyWatchdog`, :481-508) are visual only. The brief's A2 incident (cash feed frozen 15:15-15:20, garbage resume, entry on a broken feed) would have been **silent** while three other sounds fire on the fiction trade.
- Fix: new `feed_alarm` cue and policy in section 3.

### [MEDIUM] Sound policy produces the "ting ting ting"  (confirmed by reading)
- The sister cue (:979-981) rings once per *instrument* that newly enters `breach_alerts`. With 13 non-NIFTY instruments (BANKNIFTY, FINNIFTY, stocks) that is up to 13 rings per episode, and every clear/re-breach rings again. Jittery stocks re-arm continuously. Several rings in one 250 ms tick restart the same element.
- Approach edge (:970-973): `ABOUT_TO_BREACH` is `triggered` (orchestrator.py ~1005-1030), which is the same 7-pt proximity as the entry trigger. Approach and entry are therefore the *same instant*, so each trade plays approach, then trade, then possibly sister. No hysteresis: a flicker at the 7-pt edge sets `_lastSide=null` and re-rings the approach (the same flicker class as A3's FALSE_BREAK, 12 times in 23 s).
- `_lastEntry` is not primed on page load (unlike `_lastExit`). Refreshing the page while in a trade replays "trade taken".
- `pnl>=0 ? profit : stoploss` rings "stoploss" for any net-negative exit, including a TRAIL_SL exit after costs and a forced 15:25 flatten. The semantic is "loss", not stop.
- Fix: collapse the sounds as in section 3. Sketch below.

### [MEDIUM] Autoplay unlock is wrong on touch/Safari and fragile elsewhere  (confirmed by reading; browser behaviour from specs, not tested)
- `dashboard.py:473` unlocks only `SND.trade` and only on `pointerdown`. Touch `pointerdown` is not an activation-triggering event (touch activates on `pointerup`/`touchend`), so iOS/Android never unlock. `{once:true}` then removes the handler for good.
- If `soundOn` is false at the first pointerdown, the one-shot handler is also spent doing nothing. The sound pill's own `pointerdown` always fires before `click` toggles `soundOn`.
- iOS/Safari unlocks per element, so five of the six `Audio` objects are never unlocked. Desktop Chrome/Edge use sticky page activation, so the operator's Windows setup works today.
- Fix: use the Web Audio API. Create one `AudioContext`, call `ctx.resume()` from the pill's `click` (and `pointerup`/`keydown`), `fetch`+`decodeAudioData` each cue once, play via `AudioBufferSourceNode` through a master `GainNode`. This gives no per-element unlock, no seek glitch, and near-zero start latency.

### [LOW] Missing-file and storage handling
- A 404 is swallowed by `.play().catch(()=>{})` (:462). There is no `error` listener, so a missing `profit.mp3` fails silently and the operator never learns the profit cue is dead. StaticFiles with `os.makedirs` (:27-29) also creates an empty folder if the path is wrong.
- `localStorage.getItem` at :461 is unguarded. If storage is blocked (private mode, sandboxed), it throws and **kills the whole dashboard script**. `setItem` at :469 is the same.
- `sounds/README.md` says "Drop **four** audio files" and then lists six, and the pill tooltip (:244) lists four cues.
- The volume is a hard-coded 0.85 with no UI control.
- Fix: wrap storage in try/catch. On load, `fetch` each cue with `HEAD` or `decodeAudioData` and, on failure, turn the pill red with "sound: X missing". Add a persisted volume slider (default 0.7). Have app.py log a warning if a required file is absent.

## 2. Network and sourcing log (CC0 attempt)
- **Blocked, 403 on CONNECT** (egress policy; recorded by the proxy status endpoint): kenney.nl (`/assets/interface-sounds`, `/assets/ui-audio`, `/media/pages/assets/interface-sounds/`), freesound.org, cdn.freesound.org, pixabay.com, cdn.pixabay.com, mixkit.co, assets.mixkit.co, opengameart.org, archive.org, cdn.jsdelivr.net, unpkg.com, sounds.com, www.zapsplat.com, notificationsounds.com, www.google.com.
- **Failed:** `github.com/<o>/<r>/raw/...` and `api.github.com/repos/...` (403, no linked GitHub account). `github.com/` (400).
- **Worked:** `raw.githubusercontent.com` and `git clone` over github.com for public repos.
  - Cloned `https://github.com/Calinou/kenney-interface-sounds` (Kenney "Interface Sounds" 1.0, 100 WAVs, `LICENSE.txt` = **CC0 1.0**), at `scratch/20/dl/kenney-interface-sounds/`.
  - Survey (44.1k WAV): nearly all are UI clicks of 0.02-0.2 s with peaks near -1 dBFS and no normalisation. Only the `confirmation_00x` (0.29-0.56 s), `question_001` (0.5 s) and `error_006` (0.52 s) files fall in the 0.3-1.0 s range.
  - They are viable alternates (`confirmation_002` for entry, `confirmation_004` for profit, `error_006` for loss) but need trimming and normalising. I did not audition them or ship them.
- Because the roles need exact semantic shape (rising two-note, soft major third, thud, buzzer) and zero licence ambiguity, I shipped **synthesised originals**: no third-party material, so they can be dedicated CC0 by the operator.

## 3. VAJRA 2.0 sound set and policy

Files in `scratch/20/sounds_v2/` (WAV 44.1k mono 16-bit, plus 128k MP3 twin). Loudness is measured as momentary-max (400 ms) LUFS and as integrated LUFS after zero-padding to 1.2 s. True peak is ≤ -2.2 dBFS for all.

| new file | role | dur s | M-max LUFS | integr. (padded) | true peak dBFS | design | source/licence |
|---|---|---|---|---|---|---|---|
| entry | trade entered | 0.62 | -17.0 | -19.4 | -4.4 | two-note rising marimba E5→A5 | synthesised, CC0 |
| exit_profit | exit net ≥ 0 | 0.90 | -17.0 | -21.5 | -5.8 | soft major-third bell C6+E6, lowpass 5 kHz | synthesised, CC0 |
| exit_loss | exit net < 0 | 0.50 | -17.0 | -19.6 | -2.4 | muted low thud, 140→80 Hz, lowpass 700 Hz | synthesised, CC0 |
| approach_up | NIFTY nearing UPPER | 0.30 | -19.0 | -19.0 | -2.7 | single soft wood tick, D6 | synthesised, CC0 |
| approach_dn | NIFTY nearing LOWER | 0.30 | -19.0 | -19.0 | -2.2 | same tick, G5 (lower) | synthesised, CC0 |
| feed_alarm | feed stalled / exit rejected / DATA FROZEN | 0.75 | -14.0 | -16.1 | -6.1 | two low buzz pulses (196 Hz saw, lowpass 1.4 kHz), 10 ms attack | synthesised, CC0 |
| bloc_confluence | optional: ≥N weighted bloc instruments break the same side | 0.30 | -20.0 | -20.0 | -3.1 | soft wood knock 520 Hz | synthesised, CC0 |

- Every file starts and ends at exactly 0, has attack ≥ 3 ms, and has |DC| < 1e-3. All were checked numerically. Timbres differ by construction: pitched mallet, bell, noise-thud, tick, saw buzz, knock.
- The feed alarm is deliberately the loudest (-14 M) and the only non-musical timbre.
- Old file → verdict:

| old | verdict |
|---|---|
| approach_upper, approach_lower | retire; replaced by approach_up / approach_dn |
| trade_taken | retire (0.73 s lead silence, voicy.network provenance); replaced by entry |
| profit, stoploss | retire; replaced by exit_profit / exit_loss |
| sister_breach | retire; replaced by the optional bloc_confluence |

**Policy (rules the lead should adopt):**
1. **NIFTY approach.** Ring approach_up or approach_dn only on the transition `WATCHING→ABOUT_TO_BREACH`, and only while flat. Add hysteresis: re-arm only after the distance exceeds proximity + 4 pts or 60 s have elapsed. Skip the approach cue on the same tick as an entry, because the entry sound supersedes it.
2. **Entry.** Prime on load, so a refresh never rings. Key on the journal entry id, not strike+right+price.
3. **Exit.** One cue per closed round trip, by net P&L sign. Never ring on a fiction trade (feed-unhealthy flag) or on a forced flatten with |pnl| below costs.
4. **Bloc.** Replace per-stock sister rings with a single optional ring at the moment the weighted same-side pull crosses `strong_pull`. Maximum one per 5 min. It is off by default. Without this, delete the sister sound entirely.
5. **Feed alarm.** Ring on the onset of NIFTY tick stale > 5 s, `FEED_GAP` or `EXIT_REJECTED` while in a trade, spot frozen while options tick (A2), or the watchdog turning FROZEN. Repeat at most 3× at 20 s spacing, then stay silent until recovery. It also sounds while flat, since a dead feed matters then too.
6. **Global.** One queue with a 400 ms minimum gap, so cues never overlap. Priority: alarm > exit > entry > approach > bloc. Master gain default 0.7, persisted, with a slider.

**Dashboard code sketch (replaces :454-474; policy bodies at :968-989):**
```js
let ctx=null; const buf={}, NAMES=['entry','exit_profit','exit_loss','approach_up','approach_dn','feed_alarm','bloc_confluence'];
let gain=null, vol=+(()=>{try{return localStorage.getItem('vajra_vol')}catch(e){return null}})()||0.7;
async function sndInit(){ ctx=ctx||new (window.AudioContext||window.webkitAudioContext)(); await ctx.resume();
  gain=gain||Object.assign(ctx.createGain(),{}); gain.gain.value=vol; gain.connect(ctx.destination);
  for(const n of NAMES) if(!buf[n]) try{ buf[n]=await ctx.decodeAudioData(await (await fetch('/sounds/'+n+'.wav')).arrayBuffer()); }
    catch(e){ missing.push(n); } }
let _lastPlay=0; function playSnd(n){ if(!soundOn||!buf[n]||ctx.state!=='running') return;
  const now=performance.now(); if(now-_lastPlay<400) return; _lastPlay=now;
  const s=ctx.createBufferSource(); s.buffer=buf[n]; s.connect(gain); s.start(); }
// sndbtn click handler: await sndInit() inside the click (valid user gesture); also resume on pointerup/keydown.
```

## 4. Fine as is (do not change)
- Edge-triggering rather than timers, and persisting `soundOn` across reloads.
- The amber pulsing pill when muted during market hours: a muted cockpit must not look armed.
- The `_lastExit` priming logic (no retro sound on reload).
- The visual DATA FROZEN banner. It just needs sound added.

## 5. Tests to add
- Static check, run in CI: every `NAMES` entry exists in `vajra/sounds/`, duration ≤ 1.0 s, true peak ≤ -1.5 dBFS, M-max within ±3 LU, no lead silence over 30 ms. The script is `scratch/20/analyze.py` plus the ebur128 parse in `scratch/20/synth.py`.
- Headless-browser test, with a fake `AudioContext` that counts `start()` calls and a synthetic `/api/state` sequence:
  - 40 flickers at the 7-pt edge produce 1 approach.
  - 13 sister breaches produce 0 or 1 sound.
  - A page reload mid-trade produces 0.
  - A 15:15-15:20 frozen feed produces ≤ 3 alarms.
- Replay-harness metric: sounds per session, using the same state-sequence logic on the three tapes. The goal is under about 10 per day, split into entries, exits, and a handful of approaches.

## Artifacts
`scratch/20/sounds_v2/*.wav|mp3` (14 files), `scratch/20/synth.py` (generator), `scratch/20/analyze.py` and `plot_old.py`, `scratch/20/old_overview.png`, `scratch/20/synth_measure.json`, `scratch/20/dl/kenney-interface-sounds/` (CC0 source pack, untouched).
