# 13 — Operator-rule compliance & config audit: teardown
Verdict: Ready for Monday as far as the rules go. Every binding rule (1–9) holds in the 2.0 code with the shipped yaml, and the expiry-day band is byte-for-byte the 1.0 call. 2.0 does add three entry/re-anchor gates the operator never asked for (feed-sanity, LEVELS_STALE, no interval re-anchor while in a trade). It also leaves one rule-violating code default (sisters 600 s) and some stale or contradictory docs and comments. On rule compliance it is better than 1.0 overall.
Score (your area): 1.0 = 6/10, 2.0 = 7.5/10

Method: `diff -ru` of both `vajra/` trees (the dashboard.py and orchestrator.py diffs were redone with `--strip-trailing-cr`, because 1.0 is CRLF and a plain diff shows a whole-file rewrite). I also diffed the yaml, replay.py, verify_vajra.py and run_vajra.ps1. Then: a config-key-vs-code-read script, a probe that constructs both Orchestrators for 16 Oct and 19 Oct and prints every rule parameter, a feed-sanity scan of all 3 tapes, a fixture replay, and one late-session window replay (10-14 from 14:55). Scratch files are in `/home/claude/teardown/scratch/13/`.

## Rule-by-rule compliance (code + yaml + docs)
| # | Rule | Code | Config | Docs | Notes |
|---|---|---|---|---|---|
| 1 | All-in, compounding, no cap, no loss stop | OK. `execution.py:174-178` budget = capital×fraction. `regime.py:56,62` hard-codes fraction=1.0, max_trades=0, loss_stop=0 in both regimes | OK | OK (MANUAL:19-20, RUN_LIVE:44, CHANGELOG:21) | Reproduced on the fixture: 11 lots at 138.65 (100000/(138.65·65)=11.09), then 12 lots at 128.30 on 107,186 capital. Leftover plumbing and stale comments: see F4 |
| 2 | NIFTY only, paper | OK, unchanged | `LIVE_ORDERS: false` | OK | — |
| 3 | 7-pt anticipatory / break entry, no veto, 09:21→15:25 | `_approach_entry` is unchanged (`orchestrator.py:1057-1081`). Confluence is advisory only, and `conviction` is not used in sizing | `proximity_pts: 7`, `trade_start 09:21`, new `last_entry "15:25"` (= flatten) | OK | New gates FEED_UNHEALTHY / LEVELS_STALE: see F1, F3 |
| 4 | −12% stop, arm +10%, floor max(+7%, peak×0.94), 15:25 flatten | `manage()` is unchanged (`execution.py:223-266`) | wide/mid/tight give = 0.06/0.06/0.06 | OK | The code defaults are still 0.12/0.09 (`execution.py:118-119`), same as 1.0. A missing key would silently change the trail (F6) |
| 5 | 11 min after stop, 15 s after profit | Defaults fixed to 660/15 (`orchestrator.py:103,105`). In 1.0 they were 180/120 | 660 / 15 | OK | Bug fix: the cooldown-end re-anchor now runs *before* the entry check (`orchestrator.py:~660`) |
| 6 | NIFTY 20 min (+exits/cooldown ends), sisters 3 min. Expiry formula locked | Expiry-day NIFTY goes through `compute_band` with pad 0.10, the 1.0 formula (`orchestrator.py:_build_band`, `trigger_engine.py:79`) | 1200 / 180, pad_floor 0.10 | OK | Rule changes: no interval re-anchor mid-trade (F3). Sister rounding is now 0.1×step (F5). Sister **code default is 600** (F2) |
| 7 | 3 indices + 21 stocks | 24 states (probe) | OK | OK | TCS gets an UNVERIFIED weight of 2.06 (F7) |
| 8 | Trade non-expiry days, live ticks, no unnecessary sounds | MTM band (new). SSE push. Sister sound removed | `nonexpiry.band_method: mtm`, `sounds:` | OK | The approach cue is redundant with the entry cue (F8) |
| 9 | Monday 19 Oct expiry runs | Probe: `regime EXPIRY dte 0 band legacy`, expiry resolves to 2026-10-19 (Tue 20 Oct holiday in yaml:255) | OK | OK | Feed-sanity gate is active on expiry day too (F1) |

Probe output for 2.0, which is identical to 1.0 on every rule parameter (`python3 -B scratch/13/probe.py <root>`):
```
2026-10-19 expiry 2026-10-19 is_exp True regime Regime(name='EXPIRY', dte_trading=0, ..., band_method='legacy', capital_fraction=1.0, max_trades=0, daily_loss_stop_pct=0.0)
  trade_start 09:21 entry_cutoff 15:25 flatten 15:25 stopCD 660000 profitCD 15000 prox 7.0
  live False lot 65 cap 100000.0 full True frac 1.0 sl 0.12 arm 0.1 min 0.07 give 0.06 0.06 0.06 minprem 45.0 30.0
  holds {'NIFTY': 1200.0, 'BANKNIFTY': 180.0, 'HDFCBANK': 180.0} n_universe 24 gate True
```
The only difference from 1.0 is the stock strike steps: in 1.0 every stock starts at 5.0 and is resolved at runtime; in 2.0 17 of them are fixed from yaml.

## Findings (most severe first)

### [MEDIUM] Feed-sanity gate is a new, unrequested entry veto that runs every day, expiry included (`orchestrator.py:~690-705` gate order, `feed_health.py:150-182`) — reproduced
- **What it does:** blocks NEW entries and NIFTY re-anchors whenever spot is frozen ≥30 s, |basis| > 45, or a one-round jump > 40, plus 30 s of recovery. Rule 3 says entries are allowed until 15:25 and gives no such gate. The rationale is good: the 15:15+ Breeze cash feed is garbage. I scanned all 3 tapes with the real `FeedHealth` class (`scratch/13/health.py`). There are **zero trips before 15:15** on any day, so the CHANGELOG's "no false alarm" claim holds:
  ```
  2026-10-14 [['15:15:45','SPOT_FROZEN','15:20:38'], ['15:20:48','SPOT_IMPLAUSIBLE','15:21:19'], ['15:21:35',...,'15:22:05'], ['15:22:43',...,'15:27:19']]
  2026-10-15 [['15:16:16','SPOT_FROZEN', None]]   2026-10-16 [['15:15:33','SPOT_FROZEN', None]]
  ```
- **Leaky:** on 10-14 the gate re-opens three times inside the garbage period (15:20:38-48, 15:21:19-35, 15:22:05-43, so 10 s, 16 s and 38 s windows). Entries are allowed then on a feed that printed garbage seconds earlier. In my 14:55 window replay a post-stop cooldown masked these windows, so this is *plausible*, not reproduced.
- **Not time-aware:** the jump test compares against the last spot no matter how much time has passed. After any NIFTY-missing gap, a real >40-pt move trips SPOT_JUMP and blocks entries for 30 s, exactly during a gamma move on expiry day. The bundled fixture shows it: there is a time gap in the fixture, and the replay logged `15:10:00 feed_alarm NIFTY feed SPOT_JUMP`.
- **Fix:** get the operator's explicit OK (it is a rule change). Scale `max_jump_pts` by elapsed time (e.g. skip the jump test if dt > 2 s). Make recovery sticky after 15:15 (e.g. a re-trip within 120 s doubles `recovery_secs`).

### [MEDIUM] Code default for sister re-anchor is 600 s, not the operator's 180 s (`orchestrator.py:234`) — read in code
- `cfg.get("freshness","sister_hold_min_secs", default=600)`. In 1.0 the default was 180. The yaml sets 180, so the effective value today is correct (probe: 180). But CHANGELOG:38 says "Code defaults match the operator yaml", and this one doesn't. It is a leftover of a draft that changed sisters to 10 min: the docstring at `tests/test_entry_path.py:100` still says "2.0: 3 min -> 10 min" while its assertion checks 180. Rule 6 breaks silently if the key is missing.
- **Fix:** set `default=180` and fix the test docstring.

### [MEDIUM] Interval re-anchor is suppressed while a trade is open (`freshness.hold_while_in_trade: "on"`, `orchestrator.py:~412-415`) — read in code
- Rule 6 says NIFTY re-anchors every 20 min. 2.0 skips that while a trade is open. The re-anchor then happens at the post-exit recompute, which was already a rule. Entries are blocked in a trade and exits don't use the band, so this has **no P&L effect**. It does change what the dashboard and the level_tape show during a long trade: e.g. the 10:06→15:12 fixture trade held one band for 5 hours.
- It is documented (MANUAL:27 "not mid-trade") but was not asked for. **Fix:** tell the operator, or default it to "off" to honour the rule literally.

### [LOW] Stale and contradictory sizing and cut-off comments (and an orphan event type)
- `execution.py:136-137`: "non-expiry trade_small = 0.5 by default". False; the fraction is always 1.0.
- `orchestrator.py:99`: "NEW entries stop at last_entry (15:10)". The yaml and default are 15:25, so the comment is wrong.
- `vajra_config.yaml:138-140`: "trade_small = take paper trades at reduced size". This contradicts line 141 and rule 1.
- `regime.py:3`: "decides … sizing, trade caps".
- `schema.py:59,244`: `RISK_STANDDOWN` is a declared reason and event that nothing emits. It is left over from the removed cap/loss-stop draft.
- `dashboard.py:766` renders "trades x/cap" only when `max_trades` > 0. It is inert today, but CHANGELOG:85 advertises "trades against the cap".
- None of these change behaviour. They are traps for whoever edits next. **Fix:** delete the `capital_fraction`, `max_trades`, `daily_loss_stop_pct` and `RISK_STANDDOWN` plumbing, or at least correct the comments.

### [LOW] LEVELS_STALE is now an entry gate (`orchestrator.py:703`) — read in code
- 1.0 entered on the held band even when a re-anchor had just failed. 2.0 blocks, but `_levels_stale` clears on the next round that has a held band (lines 493-500). So in practice it blocks the single round on each 30 s retry, which looks random. It is not documented as a gate. **Fix:** either drop it (simplest, matches 1.0 and rule 3) or make it sticky and document it.

### [LOW] Trail-ladder code defaults still differ from the locked rule (`execution.py:118-119`)
- `trail_give_pct_wide` defaults to 0.12 and `trail_give_pct_mid` to 0.09, against the yaml's flat 0.06. Unchanged from 1.0, but CHANGELOG:38 claims defaults now match the yaml. **Fix:** set both defaults to 0.06.

### [LOW] Sister/stock level formula changed: rounding 5 → 0.1×step, and steps fixed from yaml (`orchestrator.py:_build_band`, `feed.py:_fixed_steps`)
- Real bug fix for low-priced stocks. It changes sister breaches and the confluence/lean display only. I confirmed conviction/confluence never reaches `enter()` sizing or gating, so there is no trade effect. Rule 6 locks the formula "K ± round(1.1·VWAP) − basis", and for NIFTY that still holds exactly.

### [LOW] Approach sound is redundant on every trade, and rings during cooldowns (`orchestrator.py:~674-678`, `alerts.py`)
- **Reproduced** on the fixture (`scratch/13/snd.py`). Every entry is preceded *in the same round* by an approach cue:
  ```
  09:45:56 approach NIFTY upper level 22775 (7 pts)
  09:45:56 entry BUY NIFTY 22750 CE @ 138.65   (x4 trades, same pattern)
  ```
- The approach condition is `entry is not None and health_ok and not position_active`. It doesn't check the stop/profit cooldowns or LEVELS_STALE, so the code comment "only when a trade is actually possible" is false. During an 11-min stop cooldown with spot hovering near the level, the cue rings every ≥2 min, and each post-exit re-anchor re-arms it.
- A 1.0 browser with `vajra_sound=1` migrates to mode **all** (`dashboard.py:537-538`), so the operator gets these cues by default. Feed alarm is counted as a "trades only" kind, so it also rings at ~15:15 every day.
- **Fix:** gate the approach emit on `gate in (None, NO_APPROACH)` and skip it when the entry fires in the same round. Migrate `vajra_sound=1` to `trades`.

### [LOW] Token precedence: a stale `vajra/.token` or env var beats a freshly edited credentials.py (`config.py:140-150`)
- The order is `--token` > `VAJRA_SESSION_KEY` > `vajra/.token` > `credentials.py`. The operator's 1.0 habit is to edit credentials.py daily. If a `.token` file was ever written, Monday's login fails with a stale key. **Fix:** prefer whichever source is newest by mtime, or print which source was used and the file's age at startup.

### [LOW] Docs and claims that don't verify
- CHANGELOG:2 and :46 link `docs/REVIEW_DOSSIER.md` and `docs/review/`. The directory does not exist.
- CHANGELOG:10 "Result on the recorded days" is an empty placeholder, but MANUAL:41 says "see the replay table in CHANGELOG.md".
- "Tests: 77 → 102": `pytest --collect-only` reports **101** (all 101 pass).
- TCS weight 2.06 is marked UNVERIFIED in `nifty_weights.csv` (display-only voice).

### [LOW] Dead yaml keys (same in 1.0)
- `universe.index_beta`, `expiry.shift_if_holiday_to`, `expiry.session_open`, `tape.format`, `tape.flush_interval_secs` are never read (`scratch/13/keys.py`). The `sounds.*_cooldown_secs` and `breeze.*_env` keys are read through f-strings and are fine.

## Complete behavioural diff 1.0 → 2.0 (classified)
**Changes that affect trading** (they change which trades happen or their P&L):
| Change | Where | Class | Operator asked? |
|---|---|---|---|
| Non-expiry NIFTY band = MTM pain level, narrow-only, 25-pt floor | `trigger_engine.py:compute_band_v2`, `orchestrator.py:_build_band`, `nonexpiry:` | new feature | Yes (rule 8) |
| Entry strike = current ATM, not anchor-time ATM | `execution.py:189-191`, `orchestrator.py:~726` | bug fix | No. Changes strike and lots on every trade where spot moved since the anchor. Tell the operator (he mirrors by hand) |
| Feed-sanity gate blocks entries and re-anchors | `feed_health.py`, `orchestrator.py` | rule change | **No** (F1) |
| LEVELS_STALE blocks the entry | `orchestrator.py:703` | rule change | **No** (minor) |
| Cooldown-end re-anchor before the entry check | `orchestrator.py:~650-662` | bug fix | Implied by rule 6 |
| Position managed even when the NIFTY band/basis failed that round | `orchestrator.py:~742` | bug fix | Yes in spirit (rule 4) |
| Code defaults 09:21 / 660 / 15 (were 09:30 / 180 / 120) | `orchestrator.py:97-105` | bug fix | Yes (matches the rules) |
| `nonexpiry_mode` default `observe` → `trade_small` | `config.py:109` | bug fix | Yes |
| `pad_floor` no longer clamped at ≥0.10 | `trigger_engine.py:81` | bug fix | No effect at 0.10 |

**Changes with no trade effect:**
| Change | Class | Operator asked? |
|---|---|---|
| No interval re-anchor mid-trade (`hold_while_in_trade`) | rule change | No (F3) |
| No post-exit re-anchor after 15:25 | bug fix | — |
| Sister rounding 0.1×step, 17 fixed stock steps, no auto-correction | bug fix | — |
| REVERSAL_WATCH needs \|bias\| ≥ 0.125 and fires at most once per 5 min (the advisory DecisionMaker input changes too) | bug fix (noise) | Yes ("not noisy") |
| FALSE_BREAK at most once/min; APPROACH/ENTRY/EXIT_REJECTED dedup at 60 s | bug fix | — |
| INSTRUMENT_ERROR has its own latch | bug fix | — |
| Sounds: engine-issued, 5 cues, no sister sound, Web Audio queue | new feature | Yes (rule 8) |
| SSE push dashboard, NaN-safe `/api/state`, `/docs` off, WHY-NO-TRADE strip, feed chip | new feature / bug fix | Yes (rule 8) |
| Tape: background parquet writer, once-per-round jsonl flush; undeclared event logged instead of raising; schema 2.0.0 adds columns | bug fix / new feature | — |
| Replay writes to `tapes_replay/`, streams parquet, refuses to overwrite; `--date` refused live | bug fix | — |
| Prev-close seed from parquet, skipping \|basis\| > 45 rows | bug fix | — |
| `--token` / env / `.token` session key; ASCII ps1, `-SkipVerify` | new feature | No (convenience; F9) |
| `gc.freeze()` at live start; UTF-8-safe banner | bug fix | — |
| Verify gate 7 (real non-expiry fixture) | new feature | — |
| TCS weight row added (UNVERIFIED) | data change | — |

## What 2.0 does better than 1.0 (evidence)
- Rule-default drift is fixed for the trading-critical keys: trade_start, both cooldowns and nonexpiry_mode (1.0 defaults were 09:30 / 180 / 120 / observe).
- Stops and the 15:25 flatten now run even on rounds where the NIFTY band or basis failed. 1.0 skipped `manage()` on those rounds.
- Expiry day is provably untouched: `_build_band` routes `is_expiry_day` to the exact 1.0 `compute_band` call with pad 0.10, and the regime for 19 Oct resolves to EXPIRY.
- Docs, yaml and regime.py all state the all-in / no-cap / no-loss-stop rule, and tests lock it in (`tests/test_v2.py:55-59,238-242`).

## What 2.0 does worse than 1.0 (evidence)
- It adds entry gates the operator didn't request (feed-sanity, LEVELS_STALE) and suspends the 20-min interval re-anchor during trades.
- It introduces a new rule-violating default (sister 600 s, where 1.0 had 180).
- It leaves dead cap/loss-stop plumbing and three comments that contradict rule 1.
- Every trade now plays two sounds (approach + entry), and approach cues can ring during cooldowns.
- Its docs cite files and tables that don't exist.

## Tests / checks you ran
- `python3 -B -m pytest -p no:cacheprovider -q` gives 101 passed (54 s). `--collect-only` gives 101, not the 102 the CHANGELOG claims.
- `scratch/13/probe.py` on both trees for 16 and 19 Oct: all rule parameters match (output above).
- `scratch/13/keys.py` and an inline default audit: the only rule-relevant mismatch is `sister_hold_min_secs` 600 vs 180, plus the trail give defaults inside Executor.
- `scratch/13/health.py` over 3 full tapes (NIFTY columns only): no trips before 15:15, and leaky re-opens on 10-14.
- `scratch/13/snd.py` fixture replay: 4 entries, each with a same-round approach cue; all-in sizing compounds correctly; one SPOT_JUMP feed alarm caused by the fixture's time gap.
- `scratch/13/late.py` replay of 2026-10-14 from 14:55 (window only): one entry at 15:00:59 that hit its stop (SL_HIT) at 15:16:26. The post-exit re-anchor was refused (FEED_SPOT_FROZEN). No entries during the garbage period; the leaky windows were masked by the 11-min cooldown.
