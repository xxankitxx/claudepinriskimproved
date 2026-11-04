# 14 — R14 Configuration & defaults reviewer: findings
Scope: `vajra_config.yaml`, `vajra/config.py`, `nifty_weights.csv` + every `cfg.get` / `ex.get` / property read in the codebase.
Evidence scripts: `/home/claude/vajra2_review/scratch/14/` (`key_audit.py` -> `key_audit.out`, `confirm_misc.py`, `step_parity.py`, `startup_config_diff.py`, `step_events.py`, `events_summary.py`). Nothing under `/home/claude/vajra2/` was modified.

## 0. Headline
- The 3 recorded days ran with **exactly** the current yaml (STARTUP_CONFIG diff = 0 differences on all 6 startup events, excluding the popped `breeze` block). So the lead's replays use the same config as the live runs — no drift. (confirmed by running)
- Roughly a third of the yaml is **not trade-affecting** on the live entry path (advisory/log only) and 5 keys are fully dead. 4 code defaults silently disagree with the yaml in ways that would **revive operator-reverted behaviour** if a key were ever deleted.
- The two operator asks cannot be tuned from config today: `pad_floor` cannot go below 0.10, `trade_small` does not size down, there is no last-entry key, and the sound policy is hard-coded in JS.

## 1. Correctness defects (most severe first)

### [HIGH] Code defaults silently disagree with the yaml; deleting a key resurrects reverted behaviour  (execution.py:118-119, orchestrator.py:92,95,97)  certainty: confirmed-by-running
Ran with the 5 keys removed from an in-memory copy: `stop_cooldown 180 s (yaml 660)`, `profit_cooldown 120 s (yaml 15)`, `trade_start 09:30 (yaml 09:21)`, `trail_give wide/mid/tight = 0.12/0.09/0.06 (yaml 0.06 flat)`.
- Why it matters: the 12/9/6 adaptive trail was tried and **reverted by the operator on 2026-07-02**; the 120 s profit cooldown lost to 15 s in the 06-30 sweep. A yaml typo/rename (`trail_give_pct_wide` -> anything), a hand-edited partial yaml, or a future refactor makes the engine fall back to the old values with no warning. `--config other.yaml` for a what-if run is the likeliest trigger.
- Fix: one defaults table (`DEFAULTS` dict in `config.py`, identical to the yaml), no `default=` literals at call sites; `validate()` (section 2) fails on missing required keys. Interim: change the 6 code defaults to the yaml values.
- Full mismatch table:

| key | yaml | code default (site) |
|---|---|---|
| execution.post_stop_cooldown_secs | 660 | 180 (orchestrator.py:95) |
| execution.post_profit_cooldown_secs | 15 | 120 (orchestrator.py:97) |
| execution.trail_give_pct_wide | 0.06 | 0.12 (execution.py:118) |
| execution.trail_give_pct_mid | 0.06 | 0.09 (execution.py:119) |
| expiry.trade_start | 09:21 | 09:30 (orchestrator.py:92) |
| freshness.hold_min_secs | 1200 | orchestrator passes 1200, but `FreshnessEngine.__init__` default **1800** (freshness_engine.py:26); tests use 1800 explicitly |
| routing.nonexpiry_mode | trade_small | observe (config.py:107) — safe direction |
| feed.poll_interval_secs | 0.25 | 1 (feed.py:383, app.py:131) — app prints a WARNING if >0.5 live |
| feed.mode | live | synthetic (app.py:91) — safe direction |
| expiry.holidays_verified | true | false (app.py:72) — safe direction |
| feed.real_round_stride / real_session_date | 2 / "2026-06-16" | 1 / None (dev only; `vajra/data/real_session_*.jsonl` do not exist in this tree) |

### [HIGH] `replay.py` holiday fallback crashes with the current yaml  (replay.py:54-56)  certainty: confirmed-by-running
`for k,v in expiry.items(): if k.startswith("holidays_"): parse_holidays(v)` has no `isinstance(list)` guard; `holidays_verified: true` -> `TypeError: 'bool' object is not iterable`. `app.build_calendar` was fixed (app.py:26-29 documents this exact bug); `replay.py` kept the unfixed copy. Triggered whenever a tape dir lacks an `EXPIRY_RESOLVED` event (hand-built/partial tapes, the lead's streaming harness if it reuses `replay_session`). Fix: call `app.build_calendar(cfg)` from replay.py (one implementation).

### [HIGH] `trigger.pad_floor` below 0.10 is silently ignored  (trigger_engine.py:69)  certainty: confirmed-by-running
`pad = max(0.10, pad_floor)`: pad_floor 0.0/0.05/0.10 all give pad 0.10 (upper 24215/lower 23985 in the probe); 0.20 widens. The yaml header says thresholds are "not silently hard-coded in the engine" yet 0.10 is. Impact on ask A: the obvious "narrow the band" knob is a no-op downward. Fix: rename to `trigger.pad_pct`, validate `0 <= pad_pct <= 0.5`, drop the `max()`; keep 0.10 as the default.

### [MEDIUM] Dead / advisory keys that look live  certainty: confirmed-by-reading + grep
| key | status |
|---|---|
| expiry.session_open, expiry.shift_if_holiday_to, tape.format, tape.flush_interval_secs | **never read** (grep = 0 refs). TapeWriter flushes by row count (`flush_threshold=256`, tape.py:39), calendar always shifts to prev trading day. |
| universe.index_beta.*, CSV `beta_vs_nifty` | parsed (config.py:170), **never used**; third copy lives in synthetic.py `_BETA`. |
| execution.default_qty_lots | only read when `deploy_full_capital=false` — and verify Gate 6 rejects any `false` leaf (verify_vajra.py:327-342), so it can never be activated via config without editing the gate. Dead by construction. |
| execution.strikes: `ATM_or_1ITM` | only the literal `"1ITM"` is special (execution.py:140); every other string = ATM. Effective value is ATM; typos are silent; "or 1ITM" is not implemented. |
| execution.caution_after (14:30) | emits one `EXECUTION_CAUTION_WINDOW` ANOMALY whose note says "size down + tighten exits" — nothing does either; `Executor.caution_active()` has zero callers. Fires every day (seen on all 3 tapes). |
| execution.trail_give_pct_wide/_mid, trail_extend_mid/tight_pct | all three give values equal 0.06 -> tier logic inert (by operator decision, documented). Keep as one `trail_give_pct`, or keep tiers but pin them with a test. |
| decision.bias_threshold, bias_threshold_lead, routing.routing_mode (incl. LEAD), decision.reversal_cooldown_min | **do not gate entry**: `fire` (orchestrator.py:577) ignores `dr.decision` and `reversal_freeze`; `executor.enter(conviction, threshold, channels)` ignores all three args (execution.py:180). They only fill `decision_tape`/dashboard. `routing_mode: LEAD` cannot produce an entry. |
| basis.* (ewma, abrupt_k, noise_floor, reversal_weight_floor, decay) | only feed REVERSAL_WATCH events + decision_tape flags; no trade effect. |
| continuation.* , freshness.linger_band_pct, freshness.sister_hold_min_secs, decision.w_*, entry.strong_pull | live but **display/log only** (states, heat, confluence verdict, bias gauge, `signal_tape.conviction`). NIFTY entry uses only band levels + `entry.proximity_pts`. |
| decision.thresholds_calibrated=false | only suppresses a THRESHOLDS_PROVISIONAL event; that event fired on every startup (7 times in 3 days) — noise about thresholds that gate nothing. |
Trade-affecting keys (for the record): universe, chain.strikes_each_side, strike_step.NIFTY, trigger.*, entry.proximity_pts, freshness.hold_min_secs/retry_secs, routing.nonexpiry_mode, execution.* (except above), expiry.weekly_weekday/calc_start/trade_start/session_close/holidays_*.

### [MEDIUM] `strike_step` is mutated at runtime inside `cfg.raw`  (feed.py:252,265,297,311,327; config.py:84)  certainty: confirmed-by-reading + tapes
- The feed writes resolved/corrected stock steps into `cfg.raw["strike_step"]`; `config.strike_step()` then reads them. Config is shared mutable state; `STARTUP_CONFIG` is written *before* resolution (app.py:112) so the recorded snapshot has only NIFTY/BANKNIFTY/FINNIFTY and **never** the stock steps; initial resolution (feed.py:265) emits no event. Steps change run to run: AUBANK 10->40 (10-14), 10->20 (10-15), 10->40 (10-16); SBILIFE 5->20, 20, 20->40; 3-7 `STRIKE_STEP_CORRECTED` and 4-6 `STEP_RESOLVED_LATE` events per day (events_summary/step_events output).
- Replay: ReplaySource never populates it, so replay uses `snap.inferred_step()`. Probe of sampled row groups (~8% of rows, 3 days): replay-ATM != recorded-ATM on **1 of ~158k rows** (AUBANK 10-15). So practical parity today is good (tape only holds rows where live basis was ok) — but it is luck, not design, and only sisters/stocks are affected (no trade effect today). It becomes a real risk the moment confluence/bias gates anything in 2.0.
- `resolve_from_chain` sits *inside* the instrument->step mapping: `cfg.strike_step("resolve_from_chain")` returns 1.0 (confirmed); setting it `false` changes nothing for stocks (fallback is `chain_inferred or 5.0`), so the flag is effectively dead.
- Fix: a `StrikeSteps` registry object owned by the feed (not cfg), with `snapshot()` taped as a `STRIKE_STEPS` event after each change (incl. initial resolution); config keeps only `strike_step.explicit: {NIFTY:50,BANKNIFTY:100,FINNIFTY:50}`; replay seeds the registry from the taped events (or infers) deterministically. `cfg` frozen after load (`MappingProxyType`/deep-frozen dataclass).

### [MEDIUM] Config is not validated; overrides can introduce unknown keys silently  certainty: confirmed-by-running
`--set trigger.pad_flor=0.0` is accepted (replay.py:61-73 uses `setdefault`) and does nothing; the key list shows `pad_flor` beside `pad_floor`. Wrong types/ranges (`hard_sl_pct: 12`, `trade_start: "9:21"` parses badly, `weekly_weekday: "tue "`) are not caught. `schema_version` in yaml and `vajra.SCHEMA_VERSION` are two sources of truth with no equality check; the yaml value stamps every tape row (tape.py:41) while `schema.py` is the real contract.

### [MEDIUM] Config provenance gap for replay
Replay always loads the *current* yaml, ignoring the `STARTUP_CONFIG` taped for that day (no loader exists). Today harmless (diff = 0) but the first config change makes old tapes unreproducible without git archaeology — and 2.0 will change many keys. Fix: `load_config_from_tape(dir)` (+ `--set` overrides on top), and a `config_hash` (sha256 of canonical JSON of the *effective* config incl. strike steps) in `STARTUP_CONFIG`; replay warns on mismatch.

### [MEDIUM] Verify Gate 6 will block 2.0's natural config  (verify_vajra.py:327-354)
Any new boolean defaulting to `false` (e.g. `sounds.sister_breach.enabled: false`) fails the gate and `run_vajra.ps1`/`orchestrator.sh` then refuse to start. The gate is also trivially bypassed by `"off"`/`0`. Either use enum strings (`mode: off`) for switches, or extend `ALLOWED_FALSE` deliberately. Recommended: replace Gate 6 with the validator (section 2), which owns an explicit `OPT_OUT_SWITCHES` list.

### [MEDIUM] TCS has zero voice; weights CSV  (nifty_weights.csv, config.py:31-49)  certainty: confirmed-by-running
- TCS is in `universe.stocks` and `breeze.WS_NAME_TO_INSTRUMENT` but has no CSV row -> `decision_weight` 0 (MISSING_WEIGHT anomaly every start). The NIFTY-50 factsheet PDF lists only the top 10 (ends at ITC 2.56) so **TCS is not in the PDF**; it must be < 2.56. From knowledge, TCS is ~2.0-2.4% of NIFTY 50 (IT sector is 8.48% total, Infosys 3.77) — **verify against the NSE factsheet / index methodology file before use**; I would not guess a number into the CSV. Note TCS is not in Nifty Bank/Fin, so add `TCS,stock,<w>,,,,constituent,independent_secondary`.
- CSV semantics: columns `instrument,type,nifty_weight_pct,bank_weight_pct,fin_weight_pct,beta_vs_nifty,role,channel`; `role`/`channel`/`beta` are parsed but unused. `decision_weight` falls back to the bank/fin weight for non-NIFTY names (e.g. FEDERALBNK 6.38% of *Bank* nifty treated like 6.38% of NIFTY) — inconsistent units; Σ decision weights currently 1.0248 (> NIFTY's own w_self=1.0 scale mixes). If confluence ever gates, rescale bank/fin-only names by (BANKNIFTY share of NIFTY ~ w_fin) or drop them.
- Loader: unknown/extra columns, duplicate instruments, non-numeric strings (`float()` raises with no row context) and weights not summing sensibly are not validated; a typo'd instrument row is silently ignored.
- `isec_override` covers 10 of 21 stocks; the other 11 depend on `get_names` or fall back to the raw symbol ("may subscribe the wrong stock", breeze.py:177). Validator should WARN.

### [LOW] Hard-coded constants that mimic config (drift risk)
- Dashboard: static text "trail arms at +10%", "min-profit floor +7%", "hard-stop near" = entry*0.97, flatten 15:25, close 15:30, pre-open 09:20 and "first calc 09:20 · trading 09:21", proximity tiers 2/5/7 pts, `hold_secs || 1200` (dashboard.py:480,623,662,708,723,894,928, 569-571, 960). Changing yaml makes the UI lie. `arming.proximity_pts` is sent but unused by JS. Fix: serve a `ui` block (see 2.4).
- Engine: `BasisMonitor.std_window=30`, `basis_signal_weight.floor=0.05`, `_build_chain(n=6)` ignores `chain.strikes_each_side`, "WATCHING" at 2.5×proximity (orchestrator.py:1033), `_heat` constants, step-maintenance 90 s / 3 rounds / 2x (feed.py), `TapeWriter.flush_threshold`.
- `compute_band` called with the same 3 `cfg.get` reads copy-pasted at orchestrator.py:313/705/734 -> read once in `__init__`.
- `_emit` of `holidays_2026`: yaml dates parse to `datetime.date` but taped as ISO strings; fine, but `holidays_<year>` merge is by key-prefix convention (`holidays_verified` is a bool sharing the prefix — the cause of the replay crash). Rename flag to `holidays_verified` -> `calendar.verified` and put lists under `expiry.holidays: {2026: [...]}`.
- Docs/tests stale: manual §4 says "trail ... flat" fine, but tests carry 30-min/09:30/180 s/120 s/12% comments; `test_trade_gate_0930...` hard-codes 09:30 and never reads `trade_start`. No test pins the operator-locked values.

## 2. Improvements / design proposals (ranked)
1. **Single typed config** (`VajraConfig` frozen dataclasses or pydantic) with defaults == yaml, `validate()` at load, `extra="forbid"`, deep-frozen. Remove all `default=` literals.
2. **`validate()` fail-fast** (sketch below) + effective-config hash on tape + `load_config_from_tape`.
3. **`regime:` block** replacing scattered `*_expiry/_nonexpiry` pairs and `nonexpiry_mode` (2.1).
4. **`feed_sanity:`** (2.2) and **`sounds:`** (2.3) blocks; **`ui:`** export (2.4).
5. Collapse dead/advisory keys into an `advisory:` section (clearly "never gates") or delete: session_open, shift_if_holiday_to, tape.format/flush_interval, index_beta, default_qty_lots, strikes, caution_after, routing_mode/LEAD, bias thresholds (keep as tape-only diagnostics).

### 2.1 Proposed `regime:` block (values = starting points for the lead's sweep; operator-locked ones marked LOCKED)
```yaml
regime:
  # selected per session by ExpiryResolution.is_expiry_day; tape-stamped on each level row (regime column)
  expiry:                                  # today's behaviour, unchanged
    band:   {mode: itm_vwap, pad_pct: 0.10, round_step: 5, itm_depth: 1,
             extrinsic_weight: 1.0, half_width_cap_pts: null}     # LOCKED: reproduces current levels
    entry:  {proximity_pts: 7, last_entry: "15:00", size_fraction: 1.0}   # proximity LOCKED; last_entry NEW
    cooldown: {post_stop_secs: 660, post_profit_secs: 15}                 # LOCKED
    min_premium: 30                                                       # LOCKED
    reanchor_secs: 1200                                                   # LOCKED
  nonexpiry:
    band:   {mode: itm_vwap_capped, pad_pct: 0.10, round_step: 5, itm_depth: 1,
             extrinsic_weight: 0.35,          # level = K +/- (intrinsic + w*extrinsic)*(1+pad): strips 65% of time value
             half_width_cap_pts: 45,          # hard cap |level-spot| (~0.19% of 24k); evidence p50 half-width 90-132
             half_width_floor_pts: 20}        # never closer than expiry-day hugging
    entry:  {proximity_pts: 7, last_entry: "14:45", size_fraction: 0.5}   # sizing finally implements 'trade_small'
    cooldown: {post_stop_secs: 660, post_profit_secs: 15}                 # keep LOCKED until measured
    min_premium: 45                                                       # LOCKED
    reanchor_secs: 600                # shorter than 1200 so a 100-pt move re-centres within 10 min (sweep 300/600/1200)
```
Reasoning: on the 3 tapes the nearest-level p50 was 67-100 pts with only 3-6 minutes inside 7 pts; a 45-pt cap plus extrinsic weight makes the band geometrically reachable within one anchor window while expiry behaviour stays byte-identical (all `expiry:` values equal today's, so existing replays must reproduce +8,459.75 on 10-16 only if regime=expiry is forced — verify). `size_fraction` must be implemented in `compute_lots` (`floor(capital*fraction/(prem*65))`), otherwise `trade_small` stays a lie. Gate: validator requires `nonexpiry.band.half_width_cap_pts >= half_width_floor_pts` and `size_fraction in (0,1]`.

### 2.2 `feed_sanity:` (addresses A2: frozen cash feed 15:15-15:20, +185/+240/-134-pt spot jump vs <10-pt implied-future move)
```yaml
feed_sanity:
  spot_frozen_secs: 20            # spot unchanged while option/future ticks advance -> FEED_SUSPECT, block entries
  spot_vs_future_max_pts: 40      # |spot - implied_future_prev_basis| jump (NIFTY); basis outside [-60,+60] is impossible
  max_abs_basis_pts: 60
  max_tick_jump_pct: 0.35         # one-tick spot move beyond this with future move < 10 pts -> reject tick
  recovery_confirm_secs: 30       # after trip, require N s of consistent spot/future before entries resume
  block_entries_on_trip: true     # (enum switch, see Gate-6 note) ; exits still allowed
  last_entry_cutoff: "15:00"      # also under regime.*.entry.last_entry; the stricter of the two wins
  min_instruments_live: 0.5       # fraction of universe with fresh ticks
```
Plus taped `FEED_SUSPECT`/`FEED_OK` events (add to `schema.EVENT_TYPES`; also add `post_cooldown` to `RECALC_TRIGGERS`).

### 2.3 `sounds:` (policy knobs; ask B)
```yaml
sounds:
  master: on                      # on | off  (client toggle still wins)
  volume: 0.7
  global_min_gap_secs: 4          # at most one sound per 4 s, highest priority wins
  events:                         # priority high->low; each EVENT is edge-triggered with hysteresis
    trade_entry:  {sound: trade_taken.mp3, policy: once_per_trade}
    trade_exit:   {win: profit.mp3, loss: stoploss.mp3, policy: once_per_trade}
    feed_alarm:   {sound: stoploss.mp3, policy: on_edge, repeat_every_secs: 60}   # NEW, the one that matters
    approach:     {sound: approach_upper.mp3, policy: once_per_level_anchor, hysteresis_pts: 3, min_gap_secs: 90, only_when: flat}
    sister_breach: {sound: sister_breach.mp3, policy: off}          # default OFF: 23 instruments x 3-min re-anchor = the ting-ting-ting
  # approach_lower.mp3 folded into approach with pitch-by-side, or dropped (6 files -> 4)
```
Cause of "ting ting ting" visible from config: `freshness.sister_hold_min_secs: 180` re-anchors 23 sister bands every 3 min, so `breach_alerts` membership flickers and `_breached` re-arms per instrument (dashboard.py:979-981); the approach sound re-arms whenever `a.state` blinks out of ABOUT_TO_BREACH (no hysteresis, 4 Hz; 12 FALSE_BREAKs in 23 s on 10-15). Use enum `policy: off` rather than `enabled: false` (Gate 6).

### 2.4 `ui:` export
`/api/state.ui = {flatten_before, calc_start, trade_start, session_close, trail_activate_pct, min_profit_pct, hard_sl_pct, proximity_pts, hold_secs, post_stop_cooldown_secs, sounds}` built from cfg; JS reads it instead of literals.

### 2.5 Validator sketch (`vajra/config.py`)
```python
SCHEMA = {  # path -> (type, lo, hi, choices)  — generated from the same table as DEFAULTS
  "execution.hard_sl_pct": (float, 0.01, 0.5, None),
  "execution.post_stop_cooldown_secs": (float, 0, 3600, None),
  "expiry.trade_start": (hhmm, None, None, None), ...}
OPT_OUT_SWITCHES = {"execution.LIVE_ORDERS", "decision.thresholds_calibrated", "expiry.holidays_verified"}
def validate(raw, *, weights=None, strict=True) -> list[str]:
    errs = []
    for p in leaf_paths(raw):                        # unknown keys
        if not matches_schema(p): errs.append(f"unknown key {p} (typo?)")
    for p, spec in SCHEMA.items():                   # required + type + range + enum
        v = get(raw, p, MISSING)
        if v is MISSING: errs.append(f"missing required key {p}"); continue
        errs += check(p, v, spec)
    t = lambda k: parse_hhmm(get(raw, k))
    ordered = ["expiry.session_open","expiry.calc_start","expiry.trade_start",
               "regime.nonexpiry.entry.last_entry","execution.flatten_before","expiry.session_close"]
    # cross-field: session_open <= calc_start <= trade_start < last_entry <= flatten_before <= session_close
    # trail_activate_pct > min_profit_pct; trail_give <= trail_activate; all tier give values equal OR tiers ordered wide>=mid>=tight
    # execution.LIVE_ORDERS is False unless env VAJRA_ALLOW_LIVE=1;  schema_version == vajra.SCHEMA_VERSION
    # universe: NIFTY in indices; stocks unique; every stock in WS_NAME_TO_INSTRUMENT.values(); weights row exists (ERROR for strict, WARN otherwise)
    # weights csv: unique instruments, numeric, 0<w<=100, columns exact, universe coverage; isec_override coverage WARN
    # holidays: parseable, weekdays only (WARN on weekend), sorted, year covers today.year and today.year+1 after Dec 1
    # regime: half_width_cap >= floor; size_fraction in (0,1]; last_entry < flatten_before
    return errs
# load_config(strict=True) raises ConfigError("\n".join(errs)); replay --set re-validates AFTER overrides.
```
Also: deep-freeze the dict after validation; compute `config_hash`; STARTUP_CONFIG includes `{config, config_hash, strike_steps}`.

## 3. Recommendations specific to the two operator asks
**A (non-expiry productivity)** — config can only deliver it once code honours: `trigger.pad_pct` (no 0.10 floor), `regime.nonexpiry.band.{extrinsic_weight, half_width_cap_pts}`, real `size_fraction`, shorter `reanchor_secs`, `last_entry`. Sweep grid for the lead: cap {30,45,60} x extrinsic_weight {0.2,0.35,0.5} x reanchor {300,600,1200}; report trades/day, win%, P&L per day on the 3 tapes with feed-sanity ON (A2 trades are fiction; exclude after 15:15). Keep expiry-day numbers frozen. Caveat: 3 non-expiry days is a tiny sample — treat the winning cell as a hypothesis, prefer the plateau over the peak.
**B (sounds)** — policy above: 4 sounds (entry, win/loss exit pair, feed alarm, optional one-per-anchor approach), sister sound OFF, global 4 s gap, hysteresis 3 pts. Feed alarm is more valuable than any other cue given A2.

## 4. Things that are fine and should NOT be changed
- `LIVE_ORDERS: false`, lot 65, capital 100000, all-in sizing, +10% arm / +7% floor / 6% give / -12% SL, no breakeven, cooldown values 660/15, premium floors 45/30, flatten 15:25, 09:20/09:21 gates — operator-locked; only pin them with tests (below).
- Safe-direction defaults (`holidays_verified` false, `feed.mode` synthetic, `nonexpiry_mode` observe) are good *as defaults*; keep that philosophy, but make them the yaml values' twin via the single DEFAULTS table.
- `breeze_secret()` reading credentials.py with env fallback and never putting secrets in yaml; `STARTUP_CONFIG` popping `breeze` (credentials file not touched in this review).
- Holiday list correctness: Tue 2026-10-20 (Dussehra) -> Monday 19th expiry matches the tapes' EXPIRY_RESOLVED.
- `round_to` HALF_UP golden behaviour; schema-versioned tapes.

## 5. Tests I would add (`tests/test_config.py`, `tests/test_config_locked.py`)
1. `test_yaml_validates` — `validate(load_raw())==[]`; `test_unknown_key_rejected`, `test_override_typo_rejected` (`--set trigger.pad_flor=0`), `test_bad_type_range` (hard_sl_pct=12, trade_start="9:21").
2. `test_code_defaults_equal_yaml` — for every leaf in DEFAULTS vs yaml; fails on the 6 mismatches listed in section 1 today (xfail-strict until fixed).
3. `test_locked_values_pinned` — golden dict of operator-locked values (660, 15, 0.07, 0.10, 0.06 x3, 0.12, 45, 30, 65, 100000, "09:20","09:21","15:25", 1200, 180, 7).
4. `test_trail_tiers_equal_or_ordered` and `test_executor_from_yaml_is_flat_6pct`.
5. `test_time_ordering` (cross-field) and `test_flatten_after_last_entry`.
6. `test_pad_pct_zero_narrows_band` (replaces silent `max(0.10,..)`).
7. `test_replay_fallback_calendar_with_bool_flag` — regression for the `holidays_verified` TypeError (call `_read_expiry_from_events` on an empty dir).
8. `test_strike_step_registry_not_in_cfg` — cfg deep-frozen (mutation raises), registry snapshot taped, replay reproduces recorded `atm_strike` for sampled rows of each tape.
9. `test_weights_csv`: universe coverage (TCS fails until added), unique rows, numeric, columns exact; `test_isec_override_coverage` WARN.
10. `test_config_hash_roundtrip` — STARTUP_CONFIG -> `load_config_from_tape` -> identical hash; replay warns on mismatch.
11. `test_regime_expiry_equals_legacy` — with regime=expiry the new band/entry path is byte-identical to today's on a recorded round (guards operator-locked behaviour).
12. `test_ui_export_matches_cfg` — `/api/state.ui` values equal cfg (kills the hard-coded dashboard literals); `test_sounds_policy_schema`.
