# 24 — Docs vs code: teardown
Verdict: The docs are accurate on most mechanics (sizing, exits, cooldowns, timings, feed gate, sound files, SSE path, CLI guards), but some headline claims are false. "Expiry day is unchanged" is wrong in practice, the test count is wrong, the latency numbers come from a tool that does not run, and the evidence links point to files that do not exist. Monday's procedure works step by step on paper, with three traps: the folder path, the `.token` file encoding, and a misleading note that "Monday is not expiry". Worse than 1.0's docs on accuracy of headline claims; better on coverage.
Score (your area): 1.0 = 6/10, 2.0 = 5/10

## Findings (most severe first)

### [HIGH] "Expiry day is unchanged" is false. Only the NIFTY band formula is unchanged (CHANGELOG.md "Non-expiry trading", "Not changed"; README banner; OPERATOR_MANUAL §1a) — *reproduced*
- The band call itself is exact 1.x on expiry day (`orchestrator.py:263-266` calls `compute_band` with the same kwargs; pad differs only if `pad_floor < 0.10`). But these 2.0 changes are **not** gated by regime and do run on Monday's expiry day:
  - buy the current ATM (`orchestrator.py:740-747`);
  - `hold_while_in_trade` skips the 20-min interval re-anchor mid-trade (`:414`);
  - feed-sanity gate blocks entries and re-anchors (`:411`, `:880`);
  - re-anchor on cooldown end happens before the entry (`:660-668`);
  - sister/stock levels round to 0.1×step (`:271-273`);
  - the new sound logic.
- Run: replay the bundled 15 Oct fixture through 1.0 and 2.0 with `is_expiry_day=True` forced (`scratch/24/exp_cmp.py`, `cmp.py`):
```
1.0 recomputes: 09:41:56 open, 10:01:57 interval (mid-trade), 10:08:19 post_exit, ... 15:10:00 interval, 15:25:00 post_exit
1.0 orders: 10:00:15 ENTRY 22750 CE @196.0 -> 10:08:19 TRAIL_SL +6,233 ; 15:20:12 ENTRY 22950 CE @106.0 -> 15:25 +3,022
2.0 recomputes: 09:41:56 open, 10:08:16 post_exit, 10:08:31 post_cooldown, 15:11:00 interval
2.0 orders: 10:00:15 ENTRY 22850 CE @134.4 -> 10:08:16 TRAIL_SL +10,618   (no 15:20 trade)
```
  On the same data, an expiry day gives a different strike, different re-anchors, a different trade count and different P&L.
- The CHANGELOG's "Not changed (operator-locked): 20-minute NIFTY re-anchor" is also false while a trade is open (1.0 re-anchored at 10:01:57; 2.0 did not).
- Impact: the operator will judge Monday as "1.0 behaviour" and it is not. These changes may well be improvements, but they are undisclosed changes to the locked expiry day.
- Fix: rewrite the claim as "expiry-day **band formula** unchanged; these 2.0 changes also apply on expiry: current-ATM strike, no mid-trade interval re-anchor, feed gate, cooldown-end re-anchor order, sister rounding". Or gate them on `not regime.is_expiry_day` if the operator wants Monday to be exactly 1.0. The operator has to make that call before Monday.

### [MEDIUM] Latency numbers come from a tool that cannot run, and the repo's own artifact contradicts them (CHANGELOG "Live data", `tools/latency_bench.py:27`, `lat_v2_1000.json`) — *reproduced*
- `python3 -c "import ast;ast.parse(open('tools/latency_bench.py').read())"` gives `SyntaxError` at line 27: `pq.ParquetFile("str(Path(__file__)...parent / "tapes" / ...")")` is a broken string. The claimed "38/68/264 → 8/18/20 ms, p99/worst 10/82 ms" cannot be regenerated with the shipped tool.
- The bench never calls `gc.freeze()` (only `app.py:159-162` does, live mode only), so it cannot be the source of the "GC frozen" round-time numbers.
- `lat_v2_1000.json`, shipped in the repo (root = this 2.0 tree, 3000 rounds), reports wall p99 160.6 ms, max 8,223 ms, 12 rounds over 250 ms and GC max 314.6 ms. That contradicts "engine round p99/worst 10/82 ms".
- I patched the path and port into a scratch copy and ran 60 s each on this box (load average 21 on 2 CPUs):
  - 2.0 SSE: 0% rounds unseen; delivery p50/p95/max 23/70/249 ms; 5.9 KB payload; engine p99 289 ms.
  - 1.0 poll: 42.4% unseen; 112/231/1284 ms; 18.1 KB; engine p99 443 ms.
- So the **direction** is TRUE: push beats poll, nothing is missed and the payload is about 5 KB. The **absolute numbers** are UNVERIFIABLE.
- Fix: repair line 27 (use the `ROOT` argument), add `gc.freeze()` to the bench to match live, and regenerate the numbers. Otherwise state them as "measured on dev box X".

### [MEDIUM] Monday procedure: `cd D:\ClaudePinRiskImproved` is a new folder, so there is no prev-close seed (OPERATOR_MANUAL §2.2) — *plausible (path) / read in code (consequence)*
- The 1.0 manual says `cd D:\ClaudePinRisk`; the 2.0 manual says `cd D:\ClaudePinRiskImproved`. The tape root is repo-relative (`config.py:124-127`). In a fresh folder, `tapes/2026-10-16/` does not exist, so `_seed_from_parquet` returns nothing (`feed.py:254-276`). The jsonl fallback also finds nothing, and `vajra/data/` has no `real_session_*.jsonl`. Result: a CRITICAL `SESSION_ERROR prev_close_seed` and an unseeded pre-open, which contradicts RUN_LIVE §3 "you'll see levels before 09:20".
- No doc step says to copy `tapes/` (or at least `tapes/2026-10-16/tick_tape.parquet`) and `vajra/credentials.py` into the new folder. Point `tape.root_dir` at the old folder instead if preferred.
- When the tape is present, the seed works. Against the real recorded 16 Oct tape: `seed live_tape_parquet:2026-10-16 spot 23119.0` (`scratch/24/monday.py`).
- Fix: add a "first run of 2.0" step: copy `tapes\2026-10-16\` and `vajra\credentials.py`, or set `tape.root_dir: D:\ClaudePinRisk\tapes`.

### [MEDIUM] `vajra\.token` breaks if created the usual Windows way (RUN_LIVE §1, OPERATOR_MANUAL §2.1, `config.py:147-151`) — *reproduced (decode)*
- `read_text(encoding="utf-8")`:
  - A PowerShell 5.1 `echo X > vajra\.token` writes UTF-16LE. Result: `UnicodeDecodeError: 'utf-8' codec can't decode byte 0xff`. It is raised outside the `try` in `breeze_secret`, so the app crashes on connect.
  - A UTF-8-with-BOM file (`Out-File -Encoding utf8` on PS 5.1, Notepad "UTF-8 BOM") gives the token `'\ufeff12345678'`, so Breeze rejects the session ("token stale" misdiagnosis).
- The `-Token` path is fine.
- Fix: `read_text(encoding="utf-8-sig")` with a UTF-16 fallback, plus `.strip("\ufeff")`. Document `Set-Content -Encoding ascii vajra\.token XXXXXXXX`.

### [MEDIUM] RUN_LIVE's Monday note is wrong for this Monday; the readiness claim is about 1.x (RUN_LIVE §4 last line, header) — *reproduced (calendar)*
- "NOTE: a normal Monday is NOT a weekly-expiry day → floor 45, no ITM fallback." On 19 Oct, the calendar resolves `2026-10-19 Mon expiry 2026-10-19 is_expiry_day True IMPLIED | EXPIRY T-0 legacy` (Tue 20 is a holiday). So the premium floor is 30, the ITM fallback is active (`execution.py:200`), the dashboard pill reads "⚡ EXPIRY DAY", and none of the "NON-EXPIRY · mtm band / narrower levels" text applies.
- "READINESS (last confirmed 2026-07-01): the live path is PROVEN … 54 trades on 06-30" describes 1.x. The 2.0 live-path changes have never been run against Breeze: SSE, tape thread, `gc.freeze`, feed gate, token sources.
- Fix: add a boxed "Mon 19 Oct = EXPIRY (Tue holiday)" note listing what to expect, and mark the readiness note as 1.x.

### [LOW] Test count is 101, not 102 (CHANGELOG "Tests: 77 → 102", README, OPERATOR_MANUAL §6) — *reproduced*
- `pytest --collect-only -q` gives 1.0: `77 tests collected`; 2.0: `101 tests collected`. The full run gives `101 passed, 1 warning in 156.67s`.

### [LOW] Evidence the docs point to does not exist (CHANGELOG header and "Result on the recorded days"; README banner; OPERATOR_MANUAL §1a) — *reproduced*
- `docs/REVIEW_DOSSIER.md` and `docs/review/` do not exist (`ls docs` gives "No such file"). "Result on the recorded days" contains only a placeholder ("see the comparison section added at release"), and no such section exists. Manual §1a says "Evidence: see the replay table in CHANGELOG.md", but there is no table.

### [LOW] Sound claims are true in the engine but not fully in the browser (CHANGELOG "Sounds", `dashboard.py:549-556`, `alerts.py`) — *reproduced (node) / read in code*
- **Shift-click "audition all cues"** plays only 5 of 6: the queue cap `if(_q.length<4)` drops `feed_alarm`. Node harness running the verbatim `_playNow/_drain/playKind`: `auditioned: entry,approach_up,approach_dn,exit_profit,exit_loss`.
- **"Trade cues are never dropped"** is true in `SoundBus` (trade kinds bypass the gap and cooldown, `alerts.py:48-56`). In the browser, a 4-deep queue cap and the `>20 s` skip can still drop one.
- **"3 s gap between cues"** applies only to approach and feed cues; trade cues bypass it by design, and the browser queue prevents overlap.
- **Approach cue rings during the 11-min post-stop cooldown and the 15-s pause.** The emit condition (`orchestrator.py:678-682`) checks only health and "no position", not cooldown, stale levels or the expiry gate. The code comment says "only when a trade is actually possible", which is false. That is an unnecessary sound under rule 8.
- **TRUE:** durations 0.34–0.94 s (ffprobe: 0.340/0.340/0.653/0.549/0.940/0.784 s); peaks −2.7 to −6.6 dBFS; the old jingles were 2.42–6.03 s; five kinds; sister sound gone; approach 120 s and feed 300 s cooldowns; pill cycles off → trades → all; LOCKED label.
- Fix: queue cap of 6 (never drop trade kinds), and gate the approach emit on `gate is None or gate == NO_APPROACH`.

### [LOW] "Never closer than 25 pts" is not guaranteed (CHANGELOG, Manual §1a, `trigger_engine.py:174-180`) — *read in code*
- The floor applies to the MTM leg before `min(legacy, mtm)` and before `round_to(S+up, 5)`. Example: S = 24012.4, up = 25, so 24037.4 rounds to 24035, which is 22.6 pts away. If the legacy leg is under 25, narrow-only wins. Fix: round away from spot.

### [LOW] Prev-close seed "skips garbage rows" but still seeds from the frozen feed (CHANGELOG "Operations", `feed.py:264-270`) — *reproduced*
- On 16 Oct the seed row is 15:20:09, spot 23119.0. The NIFTY spot sat frozen at 23119.0 from 15:15:30 to 15:20:00 (basis_tape), but |basis| ≤ 45, so the row passed the filter. Impact is small: the spot is consistent with the future, and levels re-anchor at 09:20. Fix: also skip rows after the last spot change before a long freeze, or simply use rows before 15:15.

### [LOW] Gate 7's "15:15 frozen cash feed tripped the gate" check is weak (`verify_vajra.py:400`) — *reproduced*
- The fixture is spliced from 09:41–10:15 and 15:10–15:30 (`tools/make_real_fixture.py:15`). The gate first trips at **15:10:00 SPOT_JUMP** on the splice gap; SPOT_FROZEN follows at 15:16:17. A trade is held from 10:06:52 to 15:12:11 across the 5-hour hole. The assertion passes on *any* feed_sanity event. Fix: assert `reason == SPOT_FROZEN` with ts ≥ 15:15, and add a gap guard to the fixture replay.

### [LOW] Stale or contradictory doc and comment text (each *read in code*)
- README: "NIFTY/BANKNIFTY/FINNIFTY + **10** heavyweight constituents". The config has 21 stocks (`load_config` gives 3 indices + 21 stocks).
- README architecture: `regime.py` "sizing / caps". CHANGELOG: WHY NO TRADE shows "trades against the cap". In fact `max_trades` is always 0 (`regime.py:56,62`), so `dashboard.py:766` never renders it. `RISK_STANDDOWN` is still declared in `schema.py:59` and never emitted. This is dead residue of the removed caps.
- `vajra_config.yaml:138-141`: "'trade_small' = take paper trades at **reduced size** off expiry". False: sizing is all-in (`capital_fraction=1.0`).
- `orchestrator.py:99`: "NEW entries stop at last_entry (15:10)". The yaml and code give 15:25 (rule 3 is OK).
- Manual §2.4: amber APPROACHING "within ~5 pts". It is now `proximity_pts` (7) and "critical" at 2.8 (`dashboard.py:681-684`).
- Manual §4/§9: "currently TCS" missing a weight. TCS now has a row (`nifty_weights.csv:34`, UNVERIFIED weight 2.06).
- RUN_LIVE §3: "Events strip shows STARTUP_CONFIG + EXPIRY_RESOLVED". These (and REGIME) are written with `tape.event` directly (`app.py:52-53`, `orchestrator.py:182`) and never reach `_recent_events` (`:321`), the only source of the strip. Expiry is visible in the header pills instead.
- "Code defaults match the yaml" is TRUE for the four listed values (09:21/660/15/1200, `orchestrator.py:97-103,233`). Two others still differ: `sister_hold_min_secs` default 600 vs yaml 180 (`:234`), and `trail_give_pct_wide/mid` defaults 0.12/0.09 vs 0.06 (`execution.py:118-119`).
- Manual §2.3: "expect STEP_RESOLVED_LATE / STRIKE_STEP_CORRECTED within 60 s". Now this happens only for the 4 unlisted stocks (`feed.py:358`, `_fixed_steps`).

## Claims verified TRUE (evidence)
- **Sizing:** `lots = floor(capital × 1.0 / (prem × 65))` (`execution.py:177-178`), and capital compounds (`:281`). No trade cap or daily loss stop (`regime.py:56-63`; tests `test_v2.py:55-59,238`).
- **Exits and cooldowns:** −12% / +10% / +7% / 6% exits and 660 s / 15 s cooldowns in the yaml, with code defaults equal (except the trail tiers, noted above).
- **Timings:** 09:20 calc, 09:21 trade, 15:25 last entry and flatten, 15:30 self-stop (`feed.py:424-429`).
- **Monday calendar:** Mon 19 → `EXPIRY T-0 legacy IMPLIED`, near-month 27 Oct; Tue 20 not trading; 14/15/16 = NON_EXPIRY T-3/2/1.
- **Feed-gate thresholds match the clean tapes:** max unchanged 4.0/9.8/16.0 s, max |basis| 19.2/18.8/29.8, max jump 9.6/22.2/7.4 (09:15–15:15). Gate run over the recorded basis tapes: no fault before 15:15; first faults at 15:15:45 / 15:16:16 / 15:15:33. One nuance: on 14 Oct the gate re-opens three times (10–38 s each) between 15:20 and 15:23.
- **MTM band narrower:** on the fixture, half-widths ~45–88 vs legacy 102–128 pts. Never wider than legacy.
- **CLI guards:** `--date` in live mode is refused. Live mode on a weekend aborts ("2026-10-17 is NOT a trading day"). `--token` works through env precedence (`config.py:143-151`).
- **Banner:** survives cp437 (1.0 raises `UnicodeEncodeError` on '⚡').
- **Replay:** `replay.py --out <tapes root>` is refused. Replay streams (`iter_batches`). `--mode replay` writes to `tapes_replay/`. The manual's §3.1 expiry one-liner runs (`2026-08-11 False False IMPLIED`).
- **Launcher:** `run_vajra.ps1` is pure ASCII (0 non-ASCII bytes).
- **Prev-close seed:** reads the parquet (1.0 needed the jsonl, which no longer exists for 16 Oct).
- **Dashboard:** SSE hot/full/beat at 1 s / 2 s, rAF coalescing, heavy tables ≤1/s, polling fallback with in-flight guard, `/docs` off, `/api/health` carries feed state, NaN cleaned, lag pill.
- **Universe and schema:** 17 listed stock steps + 4 runtime-resolved. New schema event types REGIME and FEED_HEALTH. 1.0 tick tapes replay through 2.0.
- **Verify gate:** `verify_vajra.py` passes all 7 gates (76 s wall / 8.7 s CPU on this loaded box, so "~20 s" is plausible).

## What 2.0 does better than 1.0 (evidence)
- Documentation covers the new mechanics in detail (manual §1a/§1b, WHY NO TRADE, feed chip) and the mechanics match the code.
- `--token` removes the daily `.py` edit. The parquet seed works where 1.0's jsonl seed would find nothing on Monday.
- Measured push delivery beats 1.0 polling under identical load: 0% vs 42% rounds missed, 5.9 vs 18.1 KB.

## What 2.0 does worse than 1.0 (evidence)
- 1.0's test-count claim (77) was right; 2.0's (102) is wrong.
- 2.0 makes large unverifiable or false headline claims: expiry unchanged, latency numbers from a broken tool, a nonexistent dossier and replay table.
- The documented path change (`D:\ClaudePinRiskImproved`) adds a Monday setup trap that 1.0 did not have.

## Tests / checks you ran
- `python3 -B -m pytest -p no:cacheprovider -q`: 101 passed. Collect-only: 1.0 = 77, 2.0 = 101.
- `python3 -B verify_vajra.py`: ALL GATES PASS.
- Forced-expiry fixture replay 1.0 vs 2.0 (`scratch/24/exp_cmp.py`, `cmp.py`).
- Sound events on the fixture (`snd.py`).
- Latency bench (patched copy `scratch/24/bench.py`), SSE on 2.0 and poll on 1.0, 60 s each.
- `monday.py`: calendar for 14–27 Oct and `_seed_from_parquet` against `/home/claude/vajra2/tapes` as of 19 Oct.
- `fh.py` / `clean.py` / `seedchk.py`: feed gate and clean-feed stats over the three recorded basis tapes.
- ffprobe / ffmpeg volumedetect on the 2.0 and 1.0 sounds.
- Node harness of the dashboard sound queue.
- Live-mode guard runs (`--mode live` on Saturday, `--mode live --date`); `_say` under cp437.
- `.token` decode test (UTF-16LE, UTF-8-BOM); `ast.parse` over the tools.
- `replay.py --out <recorded root>` refusal; the manual §3.1 command.
