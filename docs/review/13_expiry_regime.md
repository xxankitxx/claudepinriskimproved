# 13 — Expiry calendar & regime classification (R13): findings

Scope: `vajra/expiry.py`, spec §4.3, config `expiry:` block, every consumer of `ExpiryResolution`.
Scripts: `/home/claude/vajra2_review/scratch/13/resolve_2026.py` (+ `resolve_2026.out`, full 2026 session table + flags), `hw_vs_T.py`.
Verification note: the web WAS reachable from my sandbox. I cross-checked the holiday list against NSE circular
NSE/FAOP/71777 (Ref 212/2025, 12-Dec-2025, fetched from nsearchives.nseindia.com) and NSE circulars NSE/CD/72233 and
NSE/DS/72239 (9-Jan-2026, Jan-15 holiday). Everything below marked "web-verified" comes from those.

## 1. Correctness defects (most severe first)

### [HIGH] No holiday list for 2027, and `holidays_verified: true` is a global boolean that will still say "true" on 2027-01-01  (vajra_config.yaml:174-175, app.py:72)  certainty: confirmed-by-running
- Sessions 2026-12-30/31 already resolve to expiry 2027-01-05 and near-month 2027-01-26 (script flags). Jan 26 2027 is a TUESDAY and Republic Day:
  `cal.resolve(2027-01-20)` returns expiry **2027-01-26** (should be Mon Jan 25) because no `holidays_2027` exists; the engine subscribes to a
  contract that does not exist, silently, with `holidays_verified: true`. The manual says "add holidays every December" but nothing enforces it.
- Fix: (a) per-year coverage check at startup: `covered_years = {yr for each holidays_<yr> key}`; if `session.year`, `expiry.year` or
  `near_month.year` not covered -> `HOLIDAYS_UNVERIFIED` event + stderr + refuse live start (or force operator ack). (b) replace the bare boolean by
  `holidays_verified_sha256: <hash of sorted dates>` + `holidays_verified_on` + `holidays_source: "NSE/FAOP/71777"`; startup recomputes the hash and treats any
  edit as unverified. (c) cross-check the computed expiry against the broker's listed contract (see §2.1) - the only defense that survives all calendar errors.

### [HIGH] `replay.py` fallback crashes with the current config  (replay.py:54-56)  certainty: confirmed-by-running
- `for k,v in expiry.items(): if k.startswith("holidays_"): parse_holidays(v)` also matches `holidays_verified: true` -> `TypeError: 'bool' object is
  not iterable` (reproduced). app.py:29 was fixed for exactly this; replay.py was not. Hit whenever a tape dir has no `EXPIRY_RESOLVED` row (e.g. only tick_tape copied, or
  the lead's streaming harness reuses `_read_expiry_from_events`). The 3 recorded days are unaffected only because their event tapes exist.
- Fix: `replay.py` should call `vajra.app.build_calendar(cfg)` (single code path), not re-implement it. Also `isinstance(val,(list,tuple))` inside `parse_holidays` callers.

### [MEDIUM] Weekday change is retroactive; replay of old tapes silently re-resolves with the new weekday  (expiry.py:44-50; replay_browser.py:65; OPERATOR_MANUAL §3.1)  certainty: confirmed-by-running
- Manual's "change one line, THU" re-maps every date: with `THU`, `resolve(2026-10-16)` -> 2026-10-22 (script). `replay_browser.py` and `demo_dashboard.py` call `build_calendar().resolve(date)`
  and so would show the wrong expiry/mode/IMPLIED-vs-ACTUAL for every already-recorded day. (`replay.py` is protected only because it reads the tape's EXPIRY_RESOLVED.)
  NSE did change weekday on 2025-09-01 (Thu->Tue, web: SEBI approval June 2025), so this is not hypothetical, and the monthly rule flipped with it.
- Fix: effective-dated schedule: `weekly_weekday_schedule: [{from: "2000-01-01", weekday: THU}, {from: "2025-09-01", weekday: TUE}]`,
  `weekday_on(d)` picks the entry with the greatest `from <= d`; for `monthly_expiry(y,m)` use the weekday in force on the 1st of month m, plus optional
  `monthly_overrides: {"2025-08": "2025-08-28"}`. Tapes' EXPIRY_RESOLVED stay authoritative for replay; replay_browser should read it too.
  Mid-year change today: operator edits one line, restarts; sessions before the change date are wrong in analytics only, live is fine.

### [MEDIUM] No cross-check of the computed expiry against what the broker actually lists  (app.py:108, feed.py:140-147)  certainty: confirmed-by-reading
- Any calendar error (missing holiday, surprise holiday, weekday change, SEBI discontinuing weeklies) makes Breeze ACK `subscribe_feeds` for a non-existent contract
  and never stream (the same failure mode as the 2026-07-01 incident in test_feed_live.py). Result: `MISSING_ATM_QUOTE` for the whole day, no tape-visible root cause.
- Fix: at startup call `breeze.option_step("NIFTY", expiry_iso)` (already exists, uses `get_option_chain_quotes`); on None/empty emit `EXPIRY_UNLISTED`, then probe the next
  Mon/Tue/Wed/Thu/earlier candidates (+/-3 days) and, if exactly one lists, use it and log `EXPIRY_OVERRIDDEN_BY_CHAIN`. Evidence the chain API works for this: apiLogs show
  `expiry_date '27-Oct-2026'` option chains (monthly = last Tuesday, matches `monthly_expiry(2026,10)`).

### [MEDIUM] Weekend special sessions are invisible to the calendar (`is_trading_day` = weekday & not holiday)  (expiry.py:53-60, app.py:104)  certainty: confirmed-by-reading + web
- Web: NSE held a regular live session on **Sunday 2026-02-01 (Union Budget)**; and **Muhurat trading is Sunday 2026-11-08**, timings "to be notified subsequently" (circular 71777).
  The live guard (`app.py:104`) would abort a Budget-type Sunday; DTE-in-trading-days counts around such a day are off by one (Fri Jan 30 -> Tue Feb 3 is 3 trading days, calendar says 2).
  Muhurat (~1h evening session) must NOT be run by the 09:15-15:30 engine; today the guard refuses it by accident, not by design.
- Fix: config `extra_sessions: [2026-02-01]` (counted as trading days) and `special_sessions: {2026-11-08: {kind: MUHURAT}}` -> refuse live with an explicit message.

### [LOW] Documentation says FIVE expiry-shifting Tuesday holidays; there are SIX  (vajra_config.yaml:172-173, OPERATOR_MANUAL §3.2)  certainty: confirmed-by-running
- Tuesday holidays: 03-03 (Holi), 03-31, 04-14, 10-20, 11-10, 11-24. Holi is in the list so behavior is right (expiry Mon 03-02), but the annotation/manual omit it. Also the comment "all 15 weekday closures"
  vs 16 entries (the 16th, 2026-01-15, is a later circular, NSE/CD/72233, web-verified).

### [LOW] Dashboard "T-N to expiry" is calendar days, not trading days  (dashboard.py:657-658)  certainty: confirmed-by-reading
- Wed 10-14 shows "T-5" for a Mon 10-19 expiry that is 3 trading days away; Fri shows T-3 for 1 trading day. Use `dte_trading` from the resolution (see §2).

### [LOW] `HOLIDAY_DECISION` is emitted on every session of a shifted week and names no holiday  (app.py:59-64); replay rebuilds `shifted_from_holiday=False` always (replay.py:48)  certainty: confirmed-by-reading
- Add `nominal_date`, `holiday_date` to the payload and persist `shifted_from_holiday`, `dte_trading`, `regime` in `EXPIRY_RESOLVED` (tapes then carry the regime).

### [LOW] Other
- `Executor._close` calls `wrapper.place("EXIT",...)` without `expiry_iso` (execution.py:265) -> live EXIT would send `expiry_date=""` (breeze.py:324). Dead while LIVE_ORDERS=false; fix before ever enabling.
- `RealSessionSource` hard-codes expiry = session date 15:30 (realfeed.py:75-76), and `synthetic._t_years` assumes ~150-270 min to expiry: both treat every day as DTE 0. Replace with the proposed `t_years_at`.
- Dashboard clock/countdown/market-phase use browser-local time (dashboard.py:478-480, 662-663, 707-709): wrong off IST machines. Send `round_ts` + IST offset or format server-side.
- `resolve()` accepts weekends/holidays silently (Tue 10-20 returns Oct 27 with DTE 5). Harmless, but a `strict=True` option for live is cheap.

## 2. Improvements / design proposals (ranked)

### 2.0 What the calendar logic gets RIGHT (all verified by running the whole 2026 table, 245 sessions)
- Monthlies: 01-27, 02-24, **03-30 (shifted)**, 04-28, 05-26, 06-30, 07-28, 08-25, 09-29, 10-27, **11-23 (shifted)**, 12-29. Oct 27 matches the Breeze chain in apiLogs.
- Oct 19 Mon expiry from Oct 20 holiday: Wed/Thu/Fri resolve to Oct 19 IMPLIED (DTE 3/2/1); Oct 19 itself `is_expiry_day=True, shifted=True`; Oct 21..26 -> Oct 27 ACTUAL.
- `active_weekly_expiry` on the shifted Monday returns today (resolved >= session), on the holiday Tuesday itself jumps to next week. Correct.
- Mar 30 and Nov 23: weekly == monthly, `is_monthly=True`, ACTUAL for the whole Wed..Mon run. Correct.
- is_monthly edge (month-boundary): comparing with `monthly_expiry(active.year, active.month)` is correct. A Tuesday holiday on the 1st shifts the expiry into the PREVIOUS month; its month's monthly is >= 7 days earlier,
  so never equal. The last Tuesday is always >= 22nd so a holiday shift can never cross a month boundary. No defect.
- Year-end: Dec 30/31 -> Jan 5 2027 IMPLIED, near-month Jan 26 2027 (correct arithmetic; see HIGH finding on 2027 holidays).
- Distribution of trading-day DTE over 2026 sessions: 0:52, 1:52, 2:52, 3:52, 4:37. ACTUAL mode covers **54 sessions (22%)**, i.e. all 4-5 sessions of each monthly week, not just the expiry day (spec-consistent: weekly == monthly all week).
- Calendar weekday is NOT a safe proxy for DTE: Fri 2026-09-11 has DTE 1 (Mon 09-14 Ganesh Chaturthi holiday), Fri 10-16 has DTE 1, Fri otherwise 2.

### 2.1 Regime API (proposed; drop-in for VAJRA 2.0)
```python
# expiry.py additions
SESSION_OPEN_MIN, SESSION_CLOSE_MIN = 9*60+15, 15*60+30      # 375 trading minutes/day
TRADING_DAYS_PER_YEAR = 252

class ExpiryCalendar:
    def trading_days_between(self, start: date, end: date) -> int:
        """# trading days d with start < d <= end (extra_sessions counted, holidays/weekends not)."""
    def t_years(self, ts_ms: int, expiry_date: date, basis: str = "trading") -> float: ...

@dataclass(frozen=True)
class ExpiryResolution:            # existing fields +
    dte_trading: int               # NIFTY weekly: 0 on expiry day, 1 = pre-expiry
    dte_calendar: int
    dte_monthly_trading: int       # BANKNIFTY/FINNIFTY/stocks/future trade the MONTHLY chain
    is_pre_expiry: bool            # dte_trading == 1
    regime: str                    # "EXPIRY_DAY" | "PRE_EXPIRY" | "NEAR" (2-3) | "FAR" (>=4)
    expiry_ts_ms: int              # expiry_date 15:30 IST, epoch ms
    def t_years_at(self, ts_ms: int, basis: str = "trading") -> float: ...   # delegates to calendar
```
Semantics (all pure functions of `ts_ms`, deterministic for replay):
- `dte_trading = calendar.trading_days_between(session_date, expiry_date)`; matches the lead's convention (10-14 -> 3, 10-15 -> 2, 10-16 -> 1, 10-19 -> 0).
- `t_years_at(ts, "trading")` = `(remaining_today + 375*dte_trading_from_today) / (375*252)` where `remaining_today = clamp(15:30 - max(now,09:15), 0, 375)` minutes
  (0 after the close, 375 before the open, based on ts's IST date, NOT the stored session_date); returns 0.0 once `ts >= expiry_ts_ms`.
- `t_years_at(ts, "calendar")` = `(expiry_ts_ms - ts)/(365*86_400_000)` (ACT/365, matches broker/BS IV tooling); floor with `max(T, 60s/yr)` only inside the pricer, never in the clock.
- Use **trading** basis for expected-move/band scaling (variance accrues in session hours: Fri 10:00 -> Mon expiry is 1.88 trading days but 3.23 calendar days: calendar overstates sqrt(T) by 31%);
  use **calendar** only when inverting a broker IV.
- Per-instrument: NIFTY options use `dte_trading`/`expiry_date`; BANKNIFTY, FINNIFTY, stocks and the future use `near_month_expiry`/`dte_monthly_trading`. Their bands today are built from a chain 7-20 trading days from expiry (`feed.py:140-147`);
  a regime that only knows the NIFTY weekly mislabels them.
- Emit `{dte_trading, dte_monthly_trading, regime, expiry_ts_ms}` in `EXPIRY_RESOLVED` and add `dte`, `t_years` columns to `decision_tape`/`level_tape` (bump `schema_version` to 1.1.0 so verify gate 2 still matches `schema.py`).
- Regime bucket thresholds: EXPIRY_DAY (0), PRE_EXPIRY (1), NEAR (2-3), FAR (>=4, only Wed-before-Tue). Plus orthogonal flags `is_monthly`, `is_shifted`, and the existing time-of-day phases (basis weight 12:30/14:00, caution 14:30, flatten 15:25).

### 2.2 Data point linking the regime variable to the band (tapes, `hw_vs_T.py`)
Band half-width = (upper-lower)/2 on NIFTY recompute rows; T = trading-basis years as above:

| day | DTE | mean T (trading days) | half-width p50 | half-width / sqrt(T_years) p10 / p50 / p90 |
|---|---|---|---|---|
| 10-14 | 3 | 3.15 | 132 | 1075 / **1185** / 1205 |
| 10-15 | 2 | 2.46 | 115 | 1095 / **1147** / 1170 |
| 10-16 | 1 | 1.29 | 90 | 1147 / **1278** / 1752 |

The ratio is near-constant (~1150-1280) across all three days, i.e. half-width ~= k * sqrt(T_trading) with k ~ 1.2e3 pts (analytically 0.44*S*sigma ~ 1.4e3 for S 25k, sigma 0.13; band half-width ~= 1.1 x extrinsic of the 1-ITM option).
Only 47 recompute samples, 3 days, so treat k as indicative. Consequence for ask A: the time-value part of the band is a deterministic function of `t_years_at(ts,"trading")`; a regime-aware cap
`hw_cap = max(hw_floor, c * k * sqrt(T))` (c<1 to tighten) can be backtested with T from the proposed API. On expiry day T -> 0 and the formula collapses to the observed +-25-35 pts, so ONE formula covers all regimes (no `is_expiry_day` branches).
Caveat: tighter than the market's own extrinsic means the level is inside the pin-risk zone defined by the strong hands' breakeven; that is a strategy decision for the band reviewers.

## 3. Recommendations specific to the two operator asks
**A (non-expiry productivity):** replace the boolean `is_expiry_day` (used at orchestrator.py:221, 599, 1019; execution.py:189-193) by `regime`/`dte_trading` everywhere.
Concrete: (1) `min_premium` and ITM fallback keyed on DTE: 0 -> 30 + ITM fallback, >=1 -> 45 (unchanged numbers, now explicit per regime);
(2) band cap as in 2.2 using `t_years_at(...,"trading")`; (3) `nonexpiry_mode` stays a gate but sizing must scale with regime (e.g. lots multiplier 1.0 / 0.6 / 0.4 / 0.25 for DTE 0 / 1 / 2-3 / 4, parameters to be swept on tapes - I have no P&L evidence for these values);
(4) show `regime` and trading-DTE in the header pill. **B (sounds):** nothing from the calendar; an expiry-day vs non-expiry-day difference in sound policy can key off `regime`.

## 4. Things that are fine and should NOT be changed
- Tuesday weekly / previous-trading-day shift / last-Tuesday monthly -> ACTUAL logic and `active_weekly_expiry` semantics (verified on all 245 sessions).
- Holiday list vs NSE circular NSE/FAOP/71777 (web-verified): all 15 weekday closures match date-for-date, plus the 2026-01-15 closure (NSE/CD/72233). Weekend holidays (Feb 15, Mar 21, Aug 15, Nov 8) correctly omitted. No entry on a weekend. I found nothing wrong in the 2026 list.
  Caveat: I verified against NSE's circular as fetched by a summarizing tool, not by reading the PDF myself; Eid/Bakri/Muharram dates can move with moon sighting - any such intra-year change would be a new circular (none found in my search).
- `holidays_verified` flag *mechanism* (loud event + stderr when false) is good; it just needs per-year/hash binding (HIGH finding).
- Near-month future expiry (`near_month_future_expiry`) incl. monthly-day and post-monthly rollover; `_options_expiry_for` (NIFTY weekly, others monthly).
- `is_monthly` comparison against the active expiry's own month.

## 5. Tests I would add (tests/test_expiry.py is 4 tests; none cover any of the following)
1. `test_full_2026_table`: parametrized golden of (session -> expiry, mode, dte) for all 245 sessions, or at least the 6 Tuesday-holiday weeks, Mar 30 / Nov 23 monthlies, Sep 11 (Monday-holiday Friday DTE 1), Dec 30/31.
2. `test_dte_trading`: Wed 10-14 ->3, Thu ->2, Fri ->1, Mon 10-19 ->0 and `is_pre_expiry` only on Fri 10-16; `dte_calendar` differs (5).
3. `test_t_years_trading`: Fri 10:00 -> 705 min => `705/(375*252)`; after 15:30 on expiry -> 0.0; before 09:15 -> full day; ts on a different date than `session_date` still correct; monotone non-increasing in ts.
4. `test_t_years_calendar_vs_trading_weekend`: Fri 10:00, Mon expiry: calendar/trading ratio ~1.72.
5. `test_holiday_coverage_guard`: session in 2026-12-30 with only `holidays_2026` -> `HOLIDAYS_UNVERIFIED` raised for 2027; hash mismatch after editing the list -> unverified.
6. `test_weekday_schedule`: with schedule THU(<2025-09-01)/TUE(after), `resolve(2025-08-27).expiry_date == 2025-08-28` and `resolve(2025-09-02)` is a Tuesday expiry; Aug-2025 monthly stays Aug 28.
7. `test_replay_fallback_calendar`: `_read_expiry_from_events` on an empty tape dir with the real config must not raise (regression for the confirmed `TypeError`).
8. `test_extra_sessions`: Sunday 2026-02-01 as `extra_sessions` -> trading day; DTE Fri 01-30 -> Tue 02-03 == 3; Muhurat date refused for live.
9. `test_expiry_chain_crosscheck`: fake Breeze returns no chain for computed expiry, one for +/-1 day -> `EXPIRY_OVERRIDDEN_BY_CHAIN`.
10. `test_double_holiday`: Monday and Tuesday holidays -> expiry previous Friday, `shifted=True`, session Friday is expiry day; monthly shifting by two days.
