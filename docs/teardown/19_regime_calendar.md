# 19 — Regime & expiry-calendar teardown (T19)
Verdict: READY for Monday 19 Oct. Mon 19 Oct resolves to EXPIRY, dte 0, legacy band, IMPLIED basis. Calendar logic is the same code as 1.0 (expiry.py is byte-identical); 2.0 adds regime.py, which is correct. No BLOCKER. Only small gaps: blank dashboard badge before 09:20, no 2027 holidays for Dec 30-31, and a silent exit if started after 15:30.
Score (your area): 1.0 = 6/10, 2.0 = 8/10
(1.0 has no regime module and no non-expiry badge. Its calendar is just as correct.)

## Findings (most severe first)

### [MEDIUM] Dashboard date / expiry / regime badge is blank from start until 09:20 (orchestrator.py:531, latest_state init ~:150) — reproduced
- `process_round` returns early before `calc_start` (09:20). `latest_state` is still the init dict `{"round_ts":None, "instruments":{}, ...}`. Constructing the Orchestrator for 2026-10-19 gave `latest_state.get('regime') -> None`.
- `session_date`, `expiry` and `regime` are only added in the big state builder (orchestrator.py:1383-1395).
- Dashboard (dashboard.py:780-792): `e.date` is undefined, so the `#expday` pill stays `display:none` and the date reads "—".
- On Monday the operator starts around 08:45-09:10. The "EXPIRY DAY" badge, the one thing they want to confirm, will not show for 10-35 minutes. Same in 1.0 (not a regression), but 2.0 claims the badge fixes this.
- Fix: seed `latest_state` in `__init__` with `session_date`, `expiry{...}`, `regime{...}`, `version`, `live_orders`. About 8 lines.

### [MEDIUM] No 2027 holidays: Dec 30-31 2026 get a wrong near-month future/chain expiry (app.py:19-33, expiry.py:108-113) — reproduced
- Config only has `holidays_2026`. `near_month_future_expiry(2026-12-30)` returns `2027-01-26`. That is Republic Day (a Tuesday), so the real monthly is Mon 25 Jan.
- Command: `cal.resolve(date(2026,12,30)).near_month_expiry` printed `2027-01-26`.
- `LiveBreezeSource` uses this date as the NIFTY future expiry and as the option expiry for BANKNIFTY, FINNIFTY and all stocks (feed.py:148-155, app.py `_build_source`).
- On Dec 30 and 31 those subscriptions would target a non-existent contract. feed.py:148 documents the symptom: Breeze ACKs the subscribe but never streams, so sisters and stocks go dark and basis is degraded.
- NIFTY's weekly Jan 5 is fine.
- Not a Monday issue.
- Fix: add `holidays_2027` (Republic Day Jan 26 2027) before Dec 28, or at least WARN when the session is within 35 days of the year end.

### [LOW] Orchestrator swallows calendar-build failure and falls back to a wrong dte (orchestrator.py:107-112, regime.py:55)
- `try: build_calendar ... except Exception: cal=None`. With `cal=None`, `dte = max(1, calendar days)`. For Wed 21 Oct that gives 6, not 4 (checked: `resolve_regime(cfg, ..., None).dte_trading == 6`).
- Only the label is affected (badge text, REGIME event). Expiry/regime name come from `expiry`, which is already resolved.
- Fix: pass `cal` into the Orchestrator from app.py instead of rebuilding it with a bare except.

### [LOW] Non-expiry badge uses the same amber `expday-hot` style as EXPIRY DAY (dashboard.py:787-791)
- Both states are amber and bold. Only the text differs ("NON-EXPIRY · T-4 ..." vs "⚡ EXPIRY DAY"). A glance can't tell them apart.
- On expiry day the badge only says "monthly" when it is the monthly. It never says the expiry was holiday-shifted (Mon 19 Oct is shifted; the info is in `shifted_from_holiday` but not in the state).
- Fix: use a muted style for NON_EXPIRY, and add "(Tue holiday, shifted)" for Mon 19 Oct.

### [LOW] Engine started after 15:30 on a trading day exits silently (feed.py:424-429, orchestrator.py:947-950)
- `rounds()` breaks at once because `hhmm_reached(now, "15:30")` is true. `finalize` is skipped because there was no round. A dashboard and tape are created and the process ends.
- The startup banner prints as if all is well. There is no "market closed" message.
- Fix: refuse with a clear message in `app.run` when live and now >= session_close.

### [LOW] Muhurat session Sun 8 Nov 2026 is refused (app.py live path)
- Live mode aborts on weekends/holidays ("NOT a trading day"). Correct for normal days. A Muhurat (evening) session would not be tradable. It is not in the 2026 list, since Nov 8 falls on a Sunday.
- Fine for paper trading, noted only.

## Calendar audit, every day 17 Oct to 31 Dec 2026
Command: `python3 -B /home/claude/teardown/scratch/19/cal.py`. It runs `ExpiryCalendar.resolve` plus `resolve_regime` for each day with the config's holidays. Every trading day was checked by hand. Weekend and holiday rows show what a replay would use; live aborts on them.

Expiry days (all EXPIRY, dte 0):

| Date | Basis | Note |
|---|---|---|
| Mon 19 Oct | IMPLIED | shifted: Tue 20 Oct is Dussehra |
| Tue 27 Oct | ACTUAL | October monthly |
| Tue 3 Nov | IMPLIED | |
| Mon 9 Nov | IMPLIED | shifted: Tue 10 Nov is Diwali Balipratipada |
| Tue 17 Nov | IMPLIED | |
| Mon 23 Nov | ACTUAL | monthly, shifted: Tue 24 Nov is Guru Nanak Gurpurb, the last Tuesday of Nov |
| Tue 1 Dec | IMPLIED | |
| Tue 8 Dec | IMPLIED | |
| Tue 15 Dec | IMPLIED | |
| Tue 22 Dec | IMPLIED | |
| Tue 29 Dec | ACTUAL | December monthly |

- Sat 17 and Sun 18 Oct resolve to Mon 19 (NON_EXPIRY, dte 1), but live mode refuses them (not trading days).
- Tue 20 Oct (holiday) resolves forward to Tue 27 Oct, dte 5. Live aborts.
- Trading-days-to-expiry (dte) spot-checked:
  - Wed 21 Oct = 4 (22, 23, 26, 27).
  - Fri 23 Oct = 2.
  - Mon 26 Oct = 1.
  - Wed 4 Nov = 3 (to Mon 9 Nov: 5, 6, 9).
  - Wed 11 Nov = 4.
  - Wed 18 Nov = 3 (to Mon 23 Nov).
  - Wed 25 Nov = 4 (to 1 Dec; Tue 24 Nov was a holiday).
  - Thu 24 Dec = 2, Mon 28 Dec = 1.
  - Wed 30 Dec = 4 and Thu 31 Dec = 3 (to Tue 5 Jan 2027).
- Week of 21-26 Oct is ACTUAL because Oct 27 is the monthly. Week of 18-20 Nov is ACTUAL (monthly shifted to Mon 23 Nov). 23-24 Dec are ACTUAL (monthly Dec 29). All other non-expiry days are IMPLIED.
- Weekly holiday count and list: config holidays_2026 match the NSE 2026 trading-holiday list as I know it (15 weekday closures plus the special Jan 15). Weekend holidays are correctly omitted. I did not re-fetch the NSE circular. The verified flag is from July.
- Year end: Fri 25 Dec is a holiday and is handled. Dec 30-31 resolve to Jan 5, 2027 correctly. Only the near-month-future date is wrong (see MEDIUM above).
- No unit tests for the calendar after Dec; `tests/test_expiry.py` passes (4/4); `tests/test_v2.py::test_regime_trading_dte_and_expiry_day` asserts Oct 15 = non-expiry dte 2 and Oct 19 = expiry.

## Regime -> band method (read in code, Orchestrator constructed for 4 dates)
- Orchestrator built for 19 Oct, 21 Oct, 27 Oct and 23 Nov. Printed regimes: EXPIRY/legacy/dte 0 for 19 Oct, 27 Oct, 23 Nov; NON_EXPIRY/mtm/dte 4 for 21 Oct. All have capital_fraction 1.0, max_trades 0, daily_loss_stop_pct 0.0, and `_expiry_gate_ok()` true. That satisfies operator rules 1 and 8.
- `_build_band` (orchestrator.py:259-275): NIFTY on an EXPIRY day takes the exact 1.x `compute_band` path. That keeps the locked formula `K ± round(1.1·VWAP) − basis` unchanged. Only NIFTY on NON_EXPIRY goes through `compute_band_v2(method="mtm")`, which narrows only and never widens vs 1.x. Sisters and stocks always use 1.x, with a rounding step of 0.1 x strike step.
- Subtle path: on a non-expiry day with `max_half_width_pts` set (currently 0), NIFTY goes through v2 even if `band_method == "legacy"`. Harmless today.
- dte does not change behaviour beyond the label. Every non-expiry day uses MTM, including the day before expiry. That is consistent with the config comment.
- Started on a holiday or weekend: live aborts with a clear SystemExit. Replay and synthetic modes accept any date.
- Started after midnight on a trading day (e.g. 00:30 Mon): `session_date` is today, feed runs idle, levels anchor at 09:20. Fine.
- Started between 00:00 and the open on a non-trading day: aborts.
- Engine running through midnight: not possible; it stops at 15:30, and time gating uses tick time of day.
- `--date` in live mode is refused (new in 2.0). Replays write to `tapes_replay/` (new in 2.0).

## What 2.0 does better than 1.0 (evidence)
- Regime is explicit: a REGIME event on the tape, regime in every state/dashboard payload, and the startup banner shows `regime NON_EXPIRY (T-4, mtm band)`.
- Non-expiry days show a regime badge with trading-day count. 1.0 shows a badge only on expiry day (dashboard.py:656 in 1.0).
- Live refuses `--date` (1.0 would subscribe the wrong expiry and write into an old tape folder); `_say` stops the ⚡ banner crashing a non-UTF-8 console.
- dte counts trading days via the holiday calendar (T-5 on Tue 20 Oct is correct), not calendar days.

## What 2.0 does worse than 1.0 (evidence)
- Nothing in this area. The pre-09:20 blank badge and the missing 2027 holidays exist in 1.0 too.

## Tests / checks you ran
- `python3 -B /home/claude/teardown/scratch/19/cal.py`: full 76-day table above.
- `python3 -B /home/claude/teardown/scratch/19/orch.py`: Orchestrator constructed for 4 session dates, regime and gate printed.
- `python3 -B -m pytest -p no:cacheprovider tests/test_expiry.py -q`: 4 passed.
- Diff of expiry.py, timeutil.py (identical to 1.0) and app.py, config (changes read).
- `resolve_regime(..., cal=None)` fallback check: dte 6 for Wed 21 Oct (vs correct 4).
- `cal.resolve(date(2026,12,30)).near_month_expiry`: 2027-01-26.
