# 20 — Sister/stock universe pipeline: teardown
Verdict: Ready for Monday. The sister/stock pipeline in 2.0 is better than 1.0 and I found no crash. The one real risk is that the measured strike-step table is trusted unconditionally. Four unlisted stocks still use runtime inference, and a wrong table entry would silently blank that stock.
Score (your area): 1.0 = 5/10, 2.0 = 7.5/10

## Findings (most severe first)

### [MEDIUM] Strike-step table has no runtime safety net; a wrong or stale entry blanks that stock (vajra_config.yaml:34-55, feed.py:143/359, config.py:86-93) — reproduced (as a replay artifact), read in code
- `_fixed_steps` = every key in `strike_step` except `resolve_from_chain`. Those names are never auto-corrected and `cfg.strike_step()` always returns the table value. Both the feed (subscribes ATM±6 at the table step) and the orchestrator use it.
- If the live grid ever differs from the table (NSE regrid, or a price-bracket change), 2.0 does not recover. 1.0 at least adapted, although it also mis-adapted wrongly (see "better" below).
- Reproduced indirectly. Window W3 (16 Oct 14:50-15:00) replays chains that 1.0 had wrongly "corrected" to 2x spacing. AXISBANK, BAJFINANCE and BANKBARODA had 0 band anchors; AUBANK and SBILIFE were N/A for 96% and 99% of rounds; there were 7 `FEED_GAP MISSING_ATM_QUOTE` events.
  - Cause: ATM or ITM strikes at the table step are absent from the 2x chain.
  - This is a recording artifact of 1.0's bad corrections, not a live failure, but it shows the failure mode. The stock stays dark, produces no crash and no loud alert beyond a once-per-episode FEED_GAP, and drops out of the confluence.
- Table accuracy: for all 17 listed stocks, the table step appears as the full dense 13-strike chain on at least one recorded day.
  - On 15 Oct, 17 of 17 matched the table, and it was never finer than the table anywhere.
  - Day-by-day check from `grid.py`: `min diff` equals the table on 15 Oct for every listed name. On 14 and 16 Oct it is 2x for AUBANK, FEDERALBNK, BAJFINANCE, AXISBANK, BANKBARODA, SBILIFE and INDUSINDBK. That is 1.0's runtime "corrections" or sparse early chains, as the CHANGELOG claims.
  - The table looks right.
- Fix, cheap: emit a one-shot ANOMALY (`STEP_TABLE_MISMATCH`) when a table-fixed name has had `MISSING_ATM_QUOTE` or band failure for more than 60 s while `inferred_step()` is a clean multiple or divisor of the table value. Do not auto-adopt it. Make it loud so the operator can edit the yaml.

### [LOW-MEDIUM] Four stocks are not in the table and still use runtime resolution (yaml:55 `resolve_from_chain: true`; feed.py:292-310, 355-375) — read in code, partly observed in the 1.0 tapes
- The four are IDFCFIRSTB, BSE, SHRIRAMFIN and BAJAJFINSV. Measured grids are 1, 50, 20 and 20 on all three days.
- Before `option_step` resolves (STEP_RESOLVED_LATE came ~20-90 s after start, BSE up to ~100 s on 16 Oct), the feed subscribes at the 5.0 default. A 50-grid stock such as BSE streams about 1 strike in 10, so the chain is dark.
- Auto-correction still applies to these four, and the 2x risk remains: SHRIRAMFIN went 5 to 20 on 16 Oct, correct that time.
- Fix: add `IDFCFIRSTB: 1`, `BSE: 50`, `SHRIRAMFIN: 20`, `BAJAJFINSV: 20` to the table, or keep them unlisted and accept ~1-2 min of darkness at startup.
- Note: the four of these in the window runs behaved identically in 1.0 and 2.0, so this is not a regression.

### [LOW] TCS weight 2.06 is UNVERIFIED (nifty_weights.csv:34) — verified as plausible, not exact
- The row parses: 8 fields, `decision_weight` = 0.0206. In the run it loaded as 0.0206 for TCS, and the MISSING_WEIGHT warning and ANOMALY no longer appear in 2.0.
- `ind_nifty50.pdf` (29 May 2026) lists only the top 10, with ITC (2.56) as the 10th. TCS is not in the list, so TCS ≤ 2.56. The 2.06 figure is consistent with that, but cannot be confirmed from the repo.
- TCS never left NEUTRAL in any of my windows, so its voice was 0 and the effect is nil there.
- A weight error of ±0.5% changes TCS's pull by at most ~0.005. The weight has no veto power (advisory only).
- Fix: confirm against the NSE factsheet when convenient. This is not a blocker.

### [LOW] Sister table, burst board and breach box now repaint at most once per second (dashboard.py:62-64, 1125, 928-945)
- `burst_board`, `breach_alerts` and `instruments` are in HEAVY and only sent in the 1 Hz "full" event; `render(s, heavy)` redraws those tables only when `heavy`.
- That is 1.0 (250 ms poll) versus 2.0 (≤1000 ms) for the universe table. NIFTY, levels and the arming/confluence panel stay on every round.
- Probably acceptable, but it is a small step against "dashboard shows tick updates without delay" for the sister rows. Not tested in a browser.

### [LOW] Sister re-anchor is not gated by feed_sanity (orchestrator.py:~406-418)
- The `feed_health` gate only suppresses NIFTY re-anchors. Sisters and stocks still re-anchor every 3 min on 15:15+ garbage cash ticks. Same as 1.0.
- Impact is nil for entries (the confluence is advisory), but sister states after 15:15 are noise.

## What 2.0 does better than 1.0 (evidence)
Method: 3 windows of real tape rounds were run through both engines with the same snapshots, ~2,000-2,350 rounds each, 10 min per window (the 10-minute windows are in W1 15 Oct 10:40-10:50, W2 15 Oct 12:45-12:55, W3 16 Oct 14:50-15:00). Zero crashes in either engine; zero INSTRUMENT_ERROR anomalies. Output is in `/home/claude/teardown/scratch/20/out_W*_v{1,2}.json`.
- **Sister band rounding fix is real (`orchestrator.py:259-273`).**
  - Offline, one band per instrument every 3 min across the whole of 15 Oct, 116 samples each (`bands.py`):

    | Stock | "Born breached" 1.0 | "Born breached" 2.0 |
    |---|---|---|
    | ITC | 63.5% | 0.0% |
    | BHARTIARTL | 33.0% | 13.0% |
    | BANKBARODA | 17.4% | 0.0% |

    - ITC's mean nearest-level distance went from -0.06% (spot already outside the level) to +0.59%.
    - IDFCFIRSTB's half-width fell from 5.05% to 2.35% of spot. The other 19 instruments were within ~±0.2%, i.e. unchanged.
  - In the windows, 1.0 held 1-3 sister/stock names "breached" for 85-100% of all rounds: ITC, BANKBARODA, BHARTIARTL, IDFCFIRSTB. These permanently topped the burst board and poisoned the confluence.
  - In 2.0, W1 and W3 had zero sister breaches. W2 had BHARTIARTL, a genuine 23% breached with 6 episodes, plus one remaining born-breached anchor.
  - BANKNIFTY, FINNIFTY and LT (steps 100, 50, 50) are unaffected because `min(5, 0.1*step)` stays 5.
- **Breach counts per hour (episodes of non-NIFTY names, extrapolated from the 10-minute windows):**

  | Window | 1.0 | 2.0 |
  |---|---|---|
  | W1 | ~30/h | 0 |
  | W2 | ~60/h | ~36/h |
  | W3 | ~96/h | 0 (W3 numbers are partly blind, see below) |

  Real breaches survive; the stuck, phantom ones go.
- **Confluence verdicts** are less contaminated. Rounds by verdict:

  | Window | 1.0 | 2.0 |
  |---|---|---|
  | W1 | MILD 1,940 / CONFLICTED 68 | LONE 2,008 |
  | W2 | STRONG 973 / MILD 1,366 | MILD 1,637 / LONE 702 |
  | W3 | CONFLICTED 1,612 / LONE 378 / MILD 363 | LONE 2,353 (partly blind) |

  - 1.0's STRONG/MILD/CONFLICTED came mostly from born-breached stocks, not from real flow.
  - The verdict is advisory only and never vetoes, so there is no trading impact either way.
- **Strike steps**: the table beats 1.0's runtime inference. The tapes' event logs show 1.0 firing STRIKE_STEP_CORRECTED 9 times on 14/15/16 Oct. Many were demonstrably wrong (for example AUBANK 10 to 40, SBILIFE 20 to 40, BAJFINANCE 10 to 20, AXISBANK 10 to 20, BANKBARODA 2.5 to 10; AUBANK ended on 20 on 15 Oct and on 40 on 14/16 Oct). 2.0 starts with the correct grid.
- **MISSING_WEIGHT**: 1.0 emitted it on all three days (stderr and tape). 2.0 does not. TCS weight is 0.0206 in the loaded table.
- **3-minute sister re-anchor** is unchanged and works: 4 anchors in 10 min in 2.0 (about 3-min spacing, first on the first round), same as 1.0.
- Other states: 18 of 21 stocks plus BANKNIFTY and FINNIFTY had 100% identical per-round state in W1 and W2. The only differences were the names fixed by rounding.
- Tests: `pytest tests/test_feed_live.py tests/test_entry_path.py tests/test_golden.py` gave 32 passed.

## What 2.0 does worse than 1.0 (evidence)
- Nothing that regressed on a live-equivalent chain.
- On a chain whose spacing does not match the table (W3, the 2x chains from 1.0's bad corrections), 2.0 produces dark sisters where 1.0 adapted (in 1.0 AUBANK/SBILIFE/AXISBANK/BAJFINANCE were live in W3, in 2.0 they were not). It does not crash and is not loud. See the first finding.
- The universe table refreshes at 1 Hz instead of 4 Hz (LOW).
- `config.strike_step`'s new `instrument != "resolve_from_chain"` check is a harmless no-op.

## Tests / checks you ran
- `/home/claude/teardown/scratch/20/grid.py`: strike grid per instrument per day versus the table. Sampled every 6th row group. All 17 listed steps appear densely on 15 Oct; none is finer than the table.
- `bands.py`: offline sister bands, 1.0 setup (chain-inferred step, rounding 5) against the 2.0 setup (table step, rounding 0.1×step), every 3 min on 15 Oct. Born-breached rate, half-width and nearest-level distance as quoted above.
- `harness.py` and `analyze.py`: windowed replay of the real tape through each engine's own `Orchestrator.process_round` (3 windows × 2 versions). Compared per-instrument states, anchors, breach episodes, board and confluence. No crashes.
- Read the diffs of `orchestrator.py`, `config.py`, `feed.py` and `trigger_engine.py` against 1.0 (confluence and burst-board code is unchanged apart from the HEAVY split).
- Not done: the full-day replay, a browser check of the 1 Hz sister table, and a Monday expiry-day run for sisters (sister logic does not depend on the expiry regime, only NIFTY bands do).
