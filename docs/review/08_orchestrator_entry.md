# 08 — Orchestrator entry path & session lifecycle: findings

Scope: `vajra/orchestrator.py` end to end (process_round ordering, `_approach_entry`, session window,
cooldown edge trackers, post-exit re-anchor, decision_tape write-after-action, `_emit_orders`/journal,
`finalize`, `run()` seed path, universe carry-forward, `_levels_stale`, heartbeat) plus the executor
and feed code it depends on. Evidence scripts: `/home/claude/vajra2_review/scratch/08/`
(`tape_summary.py`, `close_window.py`, `entry_flags.py`, `ordering_repro.py`). All line numbers refer
to `/home/claude/vajra2/vajra/orchestrator.py` unless stated.

**Headline (from the tapes, confirmed by running):** of the 5 recorded trades, exactly ONE (10-15 10:00,
dist 6.9 pts, basis +0.25) was a legitimate approach. The other four were all triggered by the 15:20
garbage cash tick (|basis| 115–204 pts, spot jumps of 103–199 pts in one 250 ms round). The engine's own
ABRUPT-basis detector fired on 3 of those 4 rounds (`basis_tape.abrupt_flag=True`) and was ignored by the
entry path. The 10-16 second trade (15:24:47, a 23300 PE that the option market priced **192 pts ITM**,
Rs 100,464 deployed, flattened 13 s later) exists only because the post-exit re-anchor at 15:22:17 was
computed on a garbage spot (basis −188). Three independent lifecycle gaps compounded: no feed-sanity
gate, re-anchor on unsanitised inputs, and no last-entry cutoff.

---

## 1. Correctness defects (most severe first)

### [CRITICAL] No feed-sanity gate: entry fires on a spot the engine itself knows is impossible  (`_approach_entry` 901–925, `process_round` 577–579)  certainty: confirmed-by-running
- What happens: `fire` = approach ∧ gate ∧ band ∧ flat ∧ no-cooldown. Nothing looks at the spot's
  plausibility. On all three days the cash feed froze 15:15:0x→15:20:0x (286 s / 264 s / 306 s unchanged
  while the implied future kept ticking) then resumed with a jump (10-14: −133.6 pts over 20 rounds,
  basis +115.7; 10-15: +198.9 pts in 0.25 s, basis −204; 10-16: +103 pts in 0.25 s, basis −117). Each
  jump put spot through/near a held level → `APPROACH_ENTRY` within one round. The basis monitor flagged
  2 of the 3 jump rounds `ABRUPT=True`, and 829 / 2,007 / 1,348 rounds per day had |basis| > 50 — a value
  NIFTY cannot have (normal |basis| < 25 off expiry; it is 0–15 on these tapes before 15:15).
- Why it matters: 4 of 5 recorded trades and ~Rs 9k of the +8,459 "P&L" on 10-16 are fiction; the
  post-15:20 spot stays 60–250 pts off the implied forward for the rest of every session.
- Fix (v2, parameters to be measured by the lead): add `FeedSanity` to the NIFTY accumulators and AND
  it into `fire` and into every re-anchor (`_recompute_nifty_band`, interval recompute):
  1. **Frozen spot**: spot unchanged for `frozen_spot_secs: 20` while ≥ N option ticks changed → state
     `SPOT_FROZEN` (no entry; hold position management on option quotes only; emit FEED_GAP once).
  2. **Implausible basis**: `|basis| > basis_sanity_pts: 40` (NIFTY) → `SPOT_IMPLAUSIBLE`. Use the
     option-implied forward as truth; the cash print is the suspect.
  3. **Single-round jump**: `|Δspot| > jump_pts: 40` per round while `|Δimplied_future| < 10` →
     `SPOT_JUMP`; require `recover_rounds: 8` (2 s) of |basis| ≤ 40 before clearing.
  4. While any state is active: `fire=False` with typed reason `FEED_SUSPECT`, no re-anchor, dashboard
     badge "FEED SUSPECT (basis −204)". All three recorded entries would have been blocked by rule 2
     alone; rule 1 would have armed 4.5 min earlier.

### [CRITICAL] No last-entry cutoff: entries allowed until 15:24:59.999, flatten at 15:25:00  (552–554, `execution.py` 226)  certainty: confirmed-by-reading + tapes
- What happens: `in_session = trade_start ≤ t < flatten_before`; `self.entry_cutoff` is literally
  `execution.flatten_before`. Recorded: entry 15:24:47.461 → EXPIRY_FLATTEN 15:25:00.108 (13 s, −68);
  entries 15:20:03/15:20:11/15:20:09 → flattened at 15:25 (5 min). The trailing stop needs +10% peak to
  arm; a 5-min window gives it no chance, so a late entry is a coin flip with Rs 1 lakh.
- Fix: new config `execution.last_entry: "15:10"` (≥ 15 min before flatten; on expiry day consider
  14:55, i.e. before the 15:00–15:30 settlement-average window the spec §7 actually names).
  `in_session = hhmm_reached(trade_start) and not hhmm_reached(last_entry)`. When an approach exists
  outside the window, record `Reason.OUTSIDE_ENTRY_WINDOW` (new) instead of `NO_APPROACH` (662).

### [HIGH] Post-exit / post-cooldown re-anchor uses the current (unsanitised) spot and ATM  (`_recompute_nifty_band` 720–750, called 565/573/649)  certainty: confirmed by tape
- What happens: after the 15:22:17 TRAIL_SL on 10-16 the band was recomputed from spot ≈ 23304
  (implied forward 23116, basis −188): ATM 23300, band [23228, 23483]; 15 s later `post_cooldown`
  repeated it. The garbage spot then drifted to 23227 → "4.5 pts above lower" → bought 23300 PE @ 220.8
  (192 pts ITM per the option market). Same pattern (harmless only because no entries after 15:25) on
  10-14 15:25 (basis −63) and 10-15 15:25/15:25:15 (basis −74/−78). Also every recompute **resets the
  20-min interval clock** (`mark_recomputed`), so the schedule silently drifts after each exit.
- Fix: route all re-anchors through the sanity gate above (refuse with `FEED_GAP/SPOT_IMPLAUSIBLE`,
  keep held band, `mark_attempt`); skip the post-exit recompute entirely when
  `hhmm_reached(ts, last_entry)` (nothing can trade on it); anchor the ATM on the **implied forward**
  (`round_to(br.future, step)` = `pin.atm_by_future`, which `locate_pin` already computes and nobody
  uses) rather than the cash spot, for both the band and `Executor.select_strike`.

### [HIGH] Paper position, cooldowns, capital and journal do not survive a restart  (`__init__` 124–166, `app.py` 118)  certainty: confirmed-by-reading (grep: no persistence anywhere)
- What happens: 10-16 had 4 `STARTUP_CONFIG` events (10:24, 10:28, 10:46 failed Breeze connects; 11:10
  succeeded); 10-14 started 13:25, 10-15 at 09:41. On a restart while in a trade the new `Executor`
  is flat with capital 1,00,000, `_last_stop_ms=0` (an active 11-min stop cooldown vanishes),
  `journal=[]`, `_level_history=[]`; the engine will happily open a *second* paper position while the
  human still mirrors the first. The manual (§2.4) documents this as "reconcile manually", which is an
  operator-facing hazard, not a design. The `.bak` parquet rename also means a restarted day's
  `order_tape.parquet` lacks the earlier session's rows (jsonl has them) — replay/analysis of a
  restarted day under-counts trades unless the `.bak` is merged.
- Fix: `tapes/<date>/state.json` written atomically (tmp+rename) on every ENTRY/EXIT and on every change
  of `_last_stop_ms/_last_profit_ms`, `executor.capital`, `journal`: `{position: Position|null,
  capital, last_stop_ms, last_profit_ms, journal, level_anchor}`. On start, if the file's date is today:
  restore, emit `STATE_RESTORED` with the open position, resume `manage()` on the first good round.
  Also, if a prior `order_tape.jsonl` shows an unmatched ENTRY, refuse to start unless restored.

### [HIGH] Open position is not managed (no stop, no 15:25 flatten) on any round where NIFTY's basis fails  (622–630)  certainty: confirmed-by-running
- What happens: `elif position_active: if nifty_band is not None and nifty_snap is not None: manage()`.
  `nifty_band` is the local from `_process_instrument`, which returns `{}` when `compute_basis` fails
  (`MISSING_ATM_QUOTE`, `NONFINITE_SPOT`) or the instrument throws — so `manage()` is skipped although
  the held strike's quote may be perfectly present. Repro: position open, 15:25:00 round with NIFTY spot
  but no ATM quotes → no EXIT/REJECT row, reason `POSITION_ACTIVE`; a −12% premium crash in that round
  is also missed. A 24-row-per-day `FEED_GAP basis` count exists on every tape (mostly stocks), so the
  precondition is routine for the universe; for NIFTY it happens whenever the garbage ATM jumps
  outside the subscribed ±6 window (a +300 pt jump would do it).
- Fix: manage on `nifty_snap` alone: `if nifty_snap is not None: manage(...)`. Position management
  must never depend on the band or basis being computable this round.

### [MEDIUM] Cooldown-end round trades on the OLD band, then replaces it  (552–574 vs 577)  certainty: confirmed-by-running
- What happens: `entry = _approach_entry(nifty_band, …)` (554) is evaluated **before** the
  `post_cooldown` recompute (565/573) swaps `st.band`; `fire`/`enter()` then use the stale local
  `nifty_band` (old levels, old `atm_strike` for the strike). Repro: profit cooldown ends; spot 4 pts
  below the OLD upper (22862) and 242 pts below the NEW upper (23100) → ENTRY at strike 22750 with
  `nifty_level_crossed=22862` in the same round `level_tape` records the new 23100 band. This is the
  exact scenario the comment at 559–561 says must not happen ("a stale level from right after the stop
  is not good enough to trade against 11 minutes later"); after an 11-min cooldown the old ATM can be
  3–4 strikes away → deep-ITM buy with no premium ceiling.
- Fix: run the cooldown edge recomputes first, then `nifty_band = self.states["NIFTY"].band` and
  compute `entry` from it.

### [MEDIUM] Engine trades on `levels_stale` bands while the cockpit dims them  (389–395, 577)  certainty: confirmed-by-reading
- `_levels_stale=True` means a due re-anchor FAILED (band age > 20 min + retries) or NIFTY blinked;
  `fire` ignores it. Operator sees "LEVELS STALE" in red while the engine enters on those levels.
- Fix: `fire` requires `not self._levels_stale`; typed reason `LEVELS_STALE`; optional hard cap
  `band_age > hold_min_secs + 120 → no entry`.

### [MEDIUM] Cockpit screams "BREACHED → BUY NIFTY …" when no entry is possible  (`_compute_alarm` 1008–1032)  certainty: confirmed-by-running
- `_compute_alarm` has no notion of `in_session`, cooldown, expiry gate, feed sanity or `levels_stale`:
  at 15:27 with spot through the upper it returns `state=BREACHED, target=BUY NIFTY 22750 CE,
  entry_block=None`. The human is told to mirror trades manually (manual §1) — this is a false order
  instruction. Same for 09:20:00–09:20:59 and during the post-profit cooldown (deliberately silent).
- Fix: pass the gate vector into `_compute_alarm`; when any gate is closed render
  `state="BLOCKED"`, `target=None`, `entry_block=<reason>`.

### [MEDIUM] Pre-open seed silently falls back to a 4-month-old recording; depends on a file the manual tells the operator to delete  (`feed.py` 176–236, `run()` 759–781)  certainty: confirmed by tape
- 10-14 seeded from `recorded:2026-06-25` → band [23941, 24131] vs the day's spot ≈ 22600 (1,400 pts
  off), stamped with the June timestamp (so the row sorts first in `level_tape` and `_level_history`,
  and the hero panel shows nonsense pre-open). Seed source (1) reads only `tick_tape.jsonl`; manual §5
  says "delete old tick_tape.jsonl (keep the parquet)" → the next day falls to source (2) = June again.
  The event is an `ANOMALY`, not `SESSION_ERROR`, and the level is still armed.
- Fix: read the prev-day **parquet** tail (`pq.ParquetFile.iter_batches` from the last row group);
  refuse any seed older than 5 trading days (`SESSION_ERROR`, levels unseeded); and never seed from
  `vajra/data/real_session_*` in live mode.

### [MEDIUM] `nonexpiry_mode: trade_small` and `caution_after: 14:30` change nothing but a flag  (220–228, 528–535, `execution.py` 167–174)  certainty: confirmed by tape
- `_expiry_gate_ok()` returns True for `trade_small`; `Executor.compute_lots` deploys ALL capital
  regardless (recorded off-expiry entries: Rs 94,559 / 89,180 / 103,350 / 94,796 / 100,464). The config
  comment ("take paper trades at reduced size off expiry") and the EXECUTION_CAUTION_WINDOW event
  ("size down + tighten exits") describe behaviour that does not exist. `expiry_gate_flag` is True on
  100% of rounds on all three tapes.
- Fix: see §3A (size multiplier + max trades/day + daily loss cap for the non-expiry regime).

### [LOW] `RECALC_TRIGGERS` enum lacks `post_cooldown`; `open` never appears after a seed  (`schema.py` 25, `freshness_engine.py` 61–72)  certainty: confirmed by tape
- `post_cooldown` rows exist (10-15: 2, 10-16: 1). After a seed the 09:20 calc is labelled `interval`,
  not `open` (`_last_attempt_ms` is set by the seed). Add `post_cooldown`; label the first intraday
  calc `open` when `age > hold_min_secs`.

### [LOW] `reversal_freeze_flag` is on for 62% / 45% of 10-15 / 10-16 and has no effect  (514–543, `decision_maker.py` 148–151)  certainty: confirmed by tape
- 49,684 and 27,150 frozen rounds; REVERSAL_WATCH 1,351 / 821 events; the legit 10-15 10:00 entry fired
  with `reversal_freeze=True`. Either the freeze gates (operator says no veto) or it should be demoted to
  a display field and stop generating events (see R-A3).

### [LOW] Decision reasons are not diagnostic (`NO_APPROACH` = 96–99% of rounds)  (659–662)
- `NO_APPROACH` conflates: no band, band stale, spot 100 pts away, outside the entry window, feed
  suspect. See §2.1.

---

## 2. Improvements / design proposals (ranked)

1. **Idle diagnostics block in `latest_state` + `decision_tape.reason_code` refinement.** Every
   round compute `idle = {reason, dist_up, dist_dn, nearest_pts, band_width, band_age_secs,
   day_range_pts, band_vs_range_ratio, regime: "expiry|T-1|T-2|T-3+", gates: {in_window, last_entry,
   expiry_gate, cooldown_left_secs, levels_stale, feed_sanity, capital_ok, premium_ok}, last_approach_ts,
   approaches_today}`. `reason` is the FIRST closed gate in priority order (FEED_SUSPECT >
   OUTSIDE_ENTRY_WINDOW > POST_STOP_COOLDOWN > POST_PROFIT_COOLDOWN > LEVELS_STALE > NO_BAND >
   FAR_FROM_LEVEL(dist) > EXPIRY_GATE). Write the same code to `decision_tape.reason_code`; render it as
   the banner text ("IDLE — 98 pts from LOWER 22505, band 265 wide vs 190 pt day range (T-3 regime)").
   The three tapes already contain the inputs; this is ~60 lines in `_update_view`.
2. **Feed-sanity gate** (§1 CRITICAL) as a small stateful class in `engine/feed_sanity.py`, fed from
   the NIFTY accumulators; its state goes on `basis_tape` (new column `spot_suspect`) and the cockpit.
3. **Entry window** `[trade_start, last_entry)` with `last_entry` default 15:10; post-exit recompute
   suppressed after `last_entry`.
4. **State checkpoint** (`state.json`) + `STATE_RESTORED`/`STATE_MISMATCH` events; `.bak` parquet merge
   in `replay.py` so a restarted day replays whole.
5. **Ordering fix in `process_round`**: (cooldown edges → recompute) → sanity → `nifty_band` from state →
   `entry` → fire; `manage()` on `nifty_snap` only. Write the round's gate vector to the decision row.
6. **Anchor ATM and strike on the implied forward**, not the cash spot (`pin.atm_by_future`): robust to
   the cash feed and consistent with spec §5.2 ("premiums live in forward space"). Band inputs then only
   need the option chain, which never froze.
7. **Breach-depth cap for anticipatory entries**: `_approach_entry` fires with `dist=-82.5`; add
   `entry.max_breach_pts: 15` — beyond that you are chasing, not anticipating (reason `BREACH_TOO_DEEP`).
8. **Both-zones degenerate case**: if `upper-lower ≤ 2×proximity_pts` (possible on expiry with thin
   extrinsic) `up_zone` and `dn_zone` are both true every round → direction flips with each tick. Require
   `band_width ≥ 3×prox` to arm, else `BAND_TOO_NARROW`.
9. **Heartbeat**: print `idle.reason` and `nearest_pts` so the terminal alone explains an idle day.

---

## 3. Recommendations specific to the operator asks

### A. Non-expiry days ("sitting idle")
From the recomputes on these tapes the band half-width is 85–137 pts off expiry and spot spends
97–99% of rounds > 17.5 pts from a level. Narrowing is a level-engine question (R-levels owners), but
the entry path must change with it or a narrower band just produces more garbage-driven and
late-day entries:
- **Regime-aware size**: `lots = floor(capital × size_mult[regime] / (prem×65))` with
  `size_mult = {expiry: 1.0, T-1: 0.5, T-2: 0.35, T-3+: 0.25}` (proposal; the lead's replays set the
  numbers). This is what `trade_small` claims to do and does not.
- **Daily guards for the non-expiry regime**: `max_trades_per_day_nonexpiry: 4`,
  `daily_loss_cap_pct_nonexpiry: 0.25` (stop after −25% of starting capital; reason `DAILY_LOSS_CAP`),
  `max_consecutive_stops: 2` → observe-only for the rest of the day. Expiry day keeps the current
  unlimited rule (operator-locked).
- **Last entry 15:10, post-exit re-anchor suppressed after it**, feed sanity ON — otherwise every
  non-expiry day will keep "trading" the 15:20 glitch, which is where all the recorded off-expiry
  activity came from.
- **Idle diagnostics** (§2.1) so "idle" is never a mystery: the banner states distance, regime and the
  closed gate.

### B. Sounds
Not my panel; one lifecycle note: the entry/exit sounds key off `entry_block==="ENTERED"` and journal
changes, so restoring a position from the checkpoint (§2.4) must not re-fire `trade_taken` on the
restart — set `_lastEntry` from the restored position on the first poll (dashboard.js ~974).

---

## 4. Things that are fine and should NOT be changed
- `decision_tape` written after `enter()`/`manage()` (580–667): the binding tape can never show FIRE
  against a REJECT. Keep.
- `signal_tape` row only on a real ENTRY (607–608). Keep.
- The ts-stamped `_nifty_band_inputs` guard (726–731): correct and never mis-fired on the tapes; extend
  it (sanity) rather than replace it.
- `finalize()` prefers `last_good` (a NIFTY-bearing round) and force-flatten refuses to book a 0.0 fill
  (`execution.py` 278–289). Keep. Ctrl+C at 15:25:52 on 10-16 still produced SHUTDOWN + flush.
- Universe completeness carry-forward with `stale: True` (487–502) and the stale sort/alert exclusion.
- Deterministic sorted-instrument iteration (457) — replay reproduces the live day byte-for-byte.
- One-position rule as the only re-entry latch, 15 s / 660 s cooldowns, trailing-stop set: operator
  locked; nothing here argues against them except the ordering fix in §1.

## 5. Tests I would add
1. `test_entry_blocked_on_implausible_basis`: band held, spot jumps +150 with implied future flat →
   no ENTRY, reason `FEED_SUSPECT`, no `post_exit`/`interval` recompute while suspect.
2. `test_frozen_spot_detector`: 90 rounds identical spot with changing option LTPs → `SPOT_FROZEN`
   event once; clears after N normal rounds.
3. `test_no_entry_after_last_entry`: approach at 15:11 → HOLD/`OUTSIDE_ENTRY_WINDOW`; 15:09 → ENTRY.
4. `test_cooldown_end_uses_fresh_band` (inverse of `ordering_repro.py` (a)).
5. `test_position_managed_when_nifty_basis_fails` (inverse of repro (b)): flatten row at 15:25:00 with
   spot-only NIFTY snapshot that still carries the held strike.
6. `test_state_checkpoint_roundtrip`: ENTRY → new Orchestrator from `state.json` → `position_active`,
   capital, cooldown anchors identical; `STATE_RESTORED` on tape; manage() exits it.
7. `test_post_exit_reanchor_refused_on_suspect_spot` and `test_no_post_exit_reanchor_after_last_entry`.
8. `test_alarm_blocked_outside_window`: `_compute_alarm` at 15:27 → `state=BLOCKED`, `target=None`.
9. `test_idle_reason_is_first_closed_gate`: parametrised over the gate vector.
10. Replay regression: the three recorded tapes under v2 must produce **0 entries after 15:10** and
    0 entries with |basis| > 40; the 10-15 10:00 trade must survive unchanged.
