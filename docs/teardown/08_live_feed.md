# 08 — Live feed & Breeze wrapper Monday-readiness: teardown
Verdict: **Ready for Monday, and better than 1.0.** 2.0's live path ran end to end against a fake Breeze SDK replaying real ticks: subscribe, build snapshots, run engine rounds, write all tapes, and stop cleanly at 15:30. I found no blocker. The residual risks are the same as in 1.0 (no reconnect if the broker drops, and the process exits if the websocket won't connect).
Score (your area): 1.0 = 6/10, 2.0 = 7.5/10

## How I tested (fake SDK + simulated Monday clock)
* `scratch/08/fake/breeze_connect.py` is a fake `BreezeConnect` with `generate_session`, `ws_connect`, `subscribe_feeds`, `get_names`, `get_option_chain_quotes`, `on_ticks`, `ws_disconnect` and `place_order`. It streams a recorded `tick_tape.parquet` and calls `on_ticks` once per Breeze-shaped tick dict:
  * cash ticks carry `stock_name` (e.g. "NIFTY 50"), `last`, `bPrice` and `sPrice`;
  * option ticks carry `product_type:"Options"`, `strike_price`, `right:"Call"/"Put"`, `expiry_date:"19-Oct-2026"`, `OI`, `ttq` and `avgPrice`;
  * futures ticks are `Futures`;
  * India VIX is sent **divided by 100**, the way Breeze streams it.
  * It only emits ticks for contracts that were actually subscribed, with the exact stock code, expiry string, strike and right. It uses made-up ISEC codes from `get_names`. Option chains return "No Data" before 09:15.
* `scratch/08/drive.py` runs `vajra.app.main(["--mode","live","--token","TEST","--no-dashboard","--config",<copy>])`. It uses a simulated clock: `time.time`/`time_ns`/`sleep`/`perf_counter` and `datetime.now` are patched so that **today = Mon 2026-10-19**. Tapes are written to `scratch/08/run*/` (never into `tapes/`), and the prior day `2026-10-16/tick_tape.parquet` is symlinked in for the seed. No network was used, and `credentials.py` values were neither printed nor stored.
* Limitation: the box ran at load average about 22 on 2 CPUs, so round periods and stalls measured here are not representative. Use the CPU-time figures instead.

## Findings (most severe first)

### [MEDIUM] A failed websocket connect kills the process; there is no retry beyond about 20 s (breeze.py:129-153, app.py:131, app.py:210-214) — *reproduced*, same as 1.0
- Scenario: `FAKE_FAIL_WS=1 python3 -B drive.py <2.0> "2026-10-19 09:10:00" 20 ...`
  - Result: `rc: EXC ... vajra.breeze.BreezeConnectionError: Breeze connect failed after 5 attempt(s): Exception('Failed to connect to live stream')`.
  - The event tape gets a `SESSION_ERROR`, then the interpreter exits with a traceback.
  - `run_vajra.ps1` does not loop, so nothing restarts it.
- This happened for real on 10-16. 1.0's event tape shows three such deaths: 10:24:40, 10:28:19 and 10:46:11 (`breeze_connect_failed ... 'Failed to connect to live stream'`). The engine only came up at 11:10:43, so the morning was lost.
- 2.0 did not change this code (`diff vajra/breeze.py` against 1.0 is identical).
- Fix: in `run_vajra.ps1`, wrap the live `python` call in a loop: on a non-zero exit before 15:25, wait 30 s and relaunch with `-SkipVerify`, and print a loud line. Alternatively, in `_connect_breeze`, retry with capped back-off for up to about 10 min, and keep the session if `generate_session` succeeded but `ws_connect` failed.

### [MEDIUM] A silent websocket stall is detected only through NIFTY spot; there is no reconnect or resubscribe (breeze.py:247-261, feed.py:421-469) — *reproduced*
- Scenario: the fake stops all ticks from 09:21:00 to 09:22:30 (`FAKE_STALL_AT/FAKE_RESUME_AT`, `runS2`/`runS1`).
  - **2.0**: one event, `09:21:30 FEED_GAP {"stage":"feed_sanity","reason":"SPOT_FROZEN","spot_unchanged_secs":30.2}`. Entries and re-anchors are paused, which is good.
  - The heartbeat kept printing `24 instr` throughout. `snapshot_raw()` keeps serving the last ticks forever, so all 23 other instruments' chains go stale with no event.
  - **1.0**: no event at all (it has no feed-sanity gate).
- If Breeze drops the socket for real, neither version reconnects. 2.0 then stays gated (no entries) until the operator restarts.
- Fix: in `_on_ticks`, stamp `self._last_tick_wall = time.time()` (and a per-key time). In `rounds()`, raise a `FEED_GAP {"stage":"ws_silent"}` after 10 s with no ticks at all. After 30 s, call `ws_disconnect()` and `ws_connect()` and resubscribe spot, future, VIX and `self._subscribed_options`.

### [LOW] The prev-close seed can pick a frozen-spot row; its basis filter does not catch a freeze (feed.py:254-276) — *reproduced*
- Tail of 10-16 NIFTY (from my scan of the parquet): spot is **23119.0 unchanged from 15:15:30 to 15:20:00**, then jumps to 23358 with basis −246.
- `_seed_from_parquet` picks `15:20:09 spot 23119.0 basis −14.0`, which is a 5-minute-old frozen spot with 15:20 option prices. Monday's run printed `pre-open seed from live_tape_parquet:2026-10-16`, with seed band 23184 / 23019.
- 10-15: it picks `15:29:59 22997.35 basis −39.0`, which is still inside the bad tail; the options-implied price was about 22958.
- Impact: the levels shown before 09:20 only. `process_round` ignores everything before `calc_start` (orchestrator.py:531), and the 09:20 calculation re-anchors.
- Fix: skip rows whose spot equals the spot from 30 s earlier, or use the options-implied future (`spot - basis`) as the seed spot.

### [LOW] The seed depends entirely on a cleanly closed prior-day parquet in this folder (feed.py:184-252) — *read in code / reproduced*
- `vajra/data/` contains only `real_nifty_day.csv`; there are no `real_session_*.jsonl` files, so fallback (2) always returns None.
- The jsonl fallback (1b) is something the manual tells the operator to delete (OPERATOR_MANUAL.md:221-223).
- So if 2.0 is unpacked into a new folder without `tapes/2026-10-16/`, or if Friday's process was killed without `tape.close()` (the parquet then has no footer), Monday logs `SESSION_ERROR prev_close_seed CRITICAL` and shows no levels before 09:20. 1.0 *always* does this today: `runA1` printed `CRITICAL: NO prev-close seed`.
- Fix: in the docs, run 2.0 in place (`D:\ClaudePinRiskImproved`) or copy `tapes\2026-10-16\tick_tape.parquet`. In code, if the newest day fails, try the next older day.

### [LOW] Four stocks still use the step "self-healing" that 2.0's own changelog calls unreliable (vajra_config.yaml:34-55, feed.py:143, feed.py:359) — *read in code + tapes*
- IDFCFIRSTB, BSE, SHRIRAMFIN and BAJAJFINSV are not in the table. They still go through chain resolution (which fails before the open, so they use 5.0 until the 90 s retry) and through the observed-grid "correction".
- On 10-16, 1.0 used SHRIRAMFIN 5 → 20 that way.
- The tapes give their real grids, with every strike streaming: SHRIRAMFIN 20 and BAJAJFINSV 20 (10-14 and 10-16), BSE 50, IDFCFIRSTB 1.
- Fix: add these 4 lines to `strike_step:`. Then nothing auto-changes on Monday. They affect only bias voice, not NIFTY execution.

### [LOW] An `HH:MM:SS` close time crashes the round loop (timeutil.py:25) — *reproduced*
- Setting `expiry.session_close: "09:23:30"` (my test value) gave `ValueError('too many values to unpack (expected 2)')` from `parse_hhmm` on the first round. The result was `SESSION_ERROR run_loop`, then finalize, then exit: a clean stop, but a dead session.
- 1.0 behaves the same. The yaml values are all `HH:MM` today, so this is a latent trap only.
- Fix: `parse_hhmm` should accept an optional `:SS`.

### [LOW] Every start opens with 24 × `MISSING_ATM_QUOTE` (feed.py:401) — *reproduced*, same as 1.0
- Options are subscribed lazily on the first round that has a spot, so that round never has option quotes. `runB` shows 24 FEED_GAP rows at 15:21:59.531.
- This is harmless but clutters the event strip. Fix: pre-subscribe the ATM±6 grid as soon as the first spot tick arrives, or suppress these events for the first 5 s.

### [LOW] Monday-relevant docs are stale (RUN_LIVE.md §4 note; OPERATOR_MANUAL.md:86-88) — *read*
- RUN_LIVE says "a normal Monday is NOT a weekly-expiry day ... floor 45, no ITM fallback". **19 Oct is an expiry day**: the engine logs `EXPIRY_RESOLVED is_expiry_day:true`, regime `EXPIRY (T-0)`, NIFTY options `19-Oct-2026`.
- The manual tells the operator to expect `STEP_RESOLVED_LATE` and `STRIKE_STEP_CORRECTED` for many names. With 2.0's table, only the 4 unlisted names can produce these.
- Fix: update both docs before Monday.

### [LOW] Breeze wrapper details, unchanged from 1.0 — *read in code*
- `_on_ticks` carries forward any *falsy* field (breeze.py:257-260). A real 0 bid, ask or volume is replaced by the previous value, so stale quotes stick.
- Subscriptions are never unsubscribed. The set only grows as ATM drifts. Whether that hits a Breeze token limit on a big trend day is *plausible* only. 2.0 starts with 697 subscriptions versus 1,039 for 1.0, because it no longer subscribes wrong-step grids.

## Things I checked that are fine
- **End to end on a simulated Monday** (`runA`, 2.0, 09:14:30 → close set to 09:26, speed 2, 10-15 ticks shifted −27.5 min). Result `rc: ok`, exit 0.
  - Subscriptions: `sub_calls 697 {cash 25, futures 24, options 648}`.
  - Expiries: NIFTY options `19-Oct-2026`; BANKNIFTY, FINNIFTY, all stocks and all futures `27-Oct-2026`. This is correct for Monday.
  - Tick tape: 874 rounds from 09:20:00.085, **all 24 instruments every round, each with 26 quotes (full ATM±6 CE+PE)**.
  - VIX is restored ×100: the tick tape has `vix 14.64` from a fake 0.1464.
  - `place_order` was never called, consistent with paper only.
- **Clean 15:30 stop** (`runB`, 2.0, 15:21:30 → real `session_close 15:30`).
  - `SHUTDOWN session_end` at 15:29:59.333, then the process exited 0.
  - Every parquet row count equals its jsonl count: tick 14,088, level 14,089, basis 14,088, decision 588. The parquet footers were written.
  - The feed-sanity gate fired on the 10-15 garbage tail (`SPOT_IMPLAUSIBLE basis −150.8`).
- **`--token TEST`** goes to env `VAJRA_SESSION_KEY` and wins in `breeze_secret` (config.py:140-151, app.py:239-241). `--date` is refused in live mode.
- **Strike-step table against real streamed grids.** I counted which strikes ever ticked in each 1.0 tape (`scratch/08/grid*.py`):
  - The 2.0 table matches every grid where it can be checked. All 13 strikes streamed for: FEDERALBNK at 5 (10-16), AXISBANK and BAJFINANCE at 10 (10-14), BANKBARODA at 5 (10-14), SBILIFE at 20 (10-14), INDUSINDBK at 10 (10-16), ITC at 5. In `runA`, on 10-15 ticks, every one of the 24 instruments got its full 26 quotes on 2.0's steps.
  - 1.0's runtime "corrections" were demonstrably wrong: AUBANK →40 (10-14 and 10-16) although its 20-grid streams fully on 10-15; FEDERALBNK →10 on 10-14 although 5 streams fully on 10-16; SBILIFE →40 on 10-16.
  - On 10-16, 1.0 lost ITC for the **whole day**. It resolved 2.5, so the ATM strike never streamed: 1,013 tick rows, first at 15:21. 2.0 fixes ITC at 5.
- `tests/test_feed_live.py` and `tests/test_breeze_codes.py`: 11 passed.

## What 2.0 does better than 1.0 (evidence)
- **Prev-close seed works.** 2.0 printed `pre-open seed from live_tape_parquet:2026-10-16` (1.6 s CPU, 175 MB RSS). 1.0 printed `CRITICAL: NO prev-close seed` on the same setup, because it reads only the jsonl.
- **No strike-step churn.** On the same 10-15 ticks, 1.0 (`runA1`) emitted 15 `STRIKE_STEP_CORRECTED` events and ran its first rounds on wrong 5-pt grids (1,039 subscriptions). 2.0 emitted none and was on the right grid from the first round (697 subscriptions).
- **Stall detection.** 2.0 raises `SPOT_FROZEN` 30 s into a feed stall; 1.0 raises nothing.
- **Startup is easier.** `--token` means no daily edit of a `.py` file. Console printing can't crash on a non-UTF-8 console (`_say`), and the replay never writes into `tapes/`.

## What 2.0 does worse than 1.0 (evidence)
- Nothing in the live feed or Breeze path. `breeze.py` is byte-identical to 1.0, and the `feed.py` changes are additive.
- The parquet seed adds about 1.6 s CPU at startup, which is negligible.

## Tests / checks I ran
- `python3 -B drive.py <repo> "<sim start>" <speed> <tape day> <tod shift> <out> [--token TEST]` with env options `DRIVE_CLOSE`, `FAKE_FAIL_WS`, `FAKE_STALL_AT/FAKE_RESUME_AT`:
  - `runA` (2.0, open), `runA1` (1.0, open), `runB` (2.0, 15:30 stop), `runS2`/`runS1` (stall, 2.0 and 1.0), `runF` (connect failure), `runS3` (HH:MM:SS crash).
  - Logs are in `/home/claude/teardown/scratch/08/*.log`.
- `grid.py` / `grid2.py`: per-instrument streamed strike grids and per-strike tick counts on 10-14 and 10-16.
- A tail scan of the NIFTY spot and basis from 15:05 to 15:30 on all three days, plus timing of `_seed_from_parquet`.
- `pytest tests/test_feed_live.py tests/test_breeze_codes.py`: 11 passed.
- `diff` of feed.py, breeze.py, app.py, config.py, tape.py and the yaml between 1.0 and 2.0.
