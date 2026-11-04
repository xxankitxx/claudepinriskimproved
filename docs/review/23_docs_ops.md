# 23 — Documentation & Operations reviewer: findings

Scope read: all five .md docs, the spec, vajra_config.yaml (every comment), nifty_weights.csv header, run_vajra.ps1,
orchestrator.sh, .claude/*.json, .gitignore, requirements.txt, sounds/README.md, vajra/app.py, credentials.py (structure only,
values never read into this file), replay*.py, demo_dashboard.py, dashboard sound JS (454-474, 968-989), feed.ReplaySource,
tape.py, schema.py. Ran `verify_vajra.py` once (read-only: uses a TemporaryDirectory): ALL 6 GATES PASS, 20 s wall here.

## 1. Correctness / operational defects (most severe first)

### [CRITICAL] The documented "review a day" command overwrites the recorded tape  (RUN_LIVE.md:62, OPERATOR_MANUAL.md:198, app.py:106-110, feed.py:76-93)  certainty: confirmed-by-reading
- `python -m vajra.app --mode replay --date 2026-10-16` builds `TapeWriter(cfg.tape_root, session_date)` (app.py:110) = the SAME
  partition `tapes/2026-10-16/` that `ReplaySource` reads. Every parquet tape that gets flushed is first renamed to `*.parquet.<ms>.bak`
  (tape.py:66-75) and replaced by the replay's output; jsonl sidecars and `event_tape.jsonl` are opened with mode "a" and get the replay's rows
  appended (duplicate STARTUP_CONFIG / EXPIRY_RESOLVED / entries). The binding record is silently polluted.
- Same call: `ReplaySource._read_rows` loads `tick_tape.jsonl` (6.5 GB per manual) or `pq.read_table(...).to_pylist()` (285 MB parquet -> many GB of
  dicts) fully into RAM. It is not the streaming path `replay_browser.py` uses.
- Fix: docs must say "never use `--mode replay --date` on a live partition"; code: in replay mode write to `tapes_replay/<date>` (or refuse when
  `cfg.tape_root/<date>` already has tapes and the source is the same dir), and make `ReplaySource` stream via `iter_batches`. Until fixed, remove the
  command from RUN_LIVE §6 and manual §6 and point to `replay_browser.py` / `replay.py --out tapes_replay`.

### [HIGH] `--date` is accepted in LIVE mode by both run scripts and by app.py  (run_vajra.ps1:145-147, orchestrator.sh:182-184, app.py:102-105, 212)  certainty: confirmed-by-reading
- `./orchestrator.sh live 2026-10-14` (positional arg 2 is passed on regardless of mode) starts a live Breeze session whose partition, expiry
  resolution, `is_trading_day` check and option-contract subscription are all computed for the *typed* date. A fat-fingered date on a trading day subscribes
  the wrong expiry (zero ticks / FEED_GAP) and writes into an old partition, i.e. the replay clobbering above, but from live.
- Fix: scripts: if `MODE=live` and a date is supplied -> error out. app.py: `if mode == "live" and args.date: parser.error("--date is not valid with --mode live")`.
  Add `--mode real` to the ps1 `ValidateSet` or drop it from app (see 1.9).

### [HIGH] Gate-before-every-start couples a 20 s test run to the 09:15 open and to every mid-day restart  (run_vajra.ps1:132-134, orchestrator.sh:172-173)  certainty: confirmed-by-running (20 s here)
- The operator restarts mid-session (10-16 had 4 STARTUP_CONFIG events, 3 `breeze_connect_failed`). Each restart re-runs the synthetic-session gate (>= 20 s; Windows + AV typically 2-3x) and
  any gate flake or missing optional dependency means no engine at all during a live session. `$ErrorActionPreference="Stop"` plus a native non-zero exit is handled, but there is no override.
- Fix: gate only when source changed: compute SHA-256 over `*.py`, `vajra_config.yaml`, `nifty_weights.csv`; if equal to `.verify_ok` stamp skip. Provide `-SkipVerify` / `SKIP_VERIFY=1` (loudly printed, event on tape `GATE_SKIPPED`)
  for same-day restarts. Run the gate in the evening (documented) and stamp it.

### [HIGH] `run_vajra.ps1` is BOM-less UTF-8 with U+2014 in double-quoted strings (lines 27,32,36,40)  certainty: plausible (known Windows PowerShell 5.1 behaviour; not run here)
- File starts with `# =` (no BOM). Windows PowerShell 5.1 reads BOM-less files as cp1252: `—` (E2 80 94) becomes `â€”` and 0x94 is a typographic closing double-quote, which 5.1 treats as a string terminator -> parse error or garbled lines
  (and `exit 1` branches may not parse). Works only under PowerShell 7+.
- Fix: replace "—" with "-" in all four strings (or save with UTF-8 BOM). Also: `$args = @(...)` assigns to the automatic variable `$args` (works, but rename to `$pyArgs`); `[string]$Date` forgets to forbid `-Date` with `-Mode live`.

### [HIGH] Secrets live in a Python source file and the daily token procedure is manual copy-paste  (credentials.py, OPERATOR_MANUAL §2.1, §8; config.py:127-146)  certainty: confirmed-by-reading
- Real API key/secret/session token in `vajra/credentials.py`; protection = `.gitignore` entry only (the folder is not a git repo here, and §8 tells the operator to "blank the three secrets" by hand before sharing, which is exactly the step that gets forgotten). Importing the module puts them in `sys.modules`/tracebacks; pytest/CI `from . import credentials` works only if the file exists.
- `breeze_secret()` already falls back to env vars but the docs never mention it, and the yaml `*_env` names (`BREEZE_*`) differ from the names the script header suggests ("needs BREEZE_* env vars", ps1:6, sh:7) while the code reads credentials.py first. The scripts' comment is therefore wrong in the other direction.
- Proposed v2 flow (details in section 3.D): precedence `VAJRA_SESSION_KEY` env var > `vajra/.token` (git-ignored, one line, mtime-checked) > legacy credentials.py (deprecated, warns). API key/secret to `VAJRA_API_KEY`/`VAJRA_API_SECRET` or `vajra/.env`. App prints the login URL (built from api_key, which is not secret for the URL purpose but still not logged to tape), age of token, and aborts with a one-line reason if the token file is older than today 00:00 IST.
- NOTE: rotate the broker secret if the previous tree was ever zipped/shared with credentials.py intact; manual §8 says full list is "in the session notes" which do not ship.

### [MEDIUM] "Zero trades on non-expiry days is CORRECT" is written into the operator manual, troubleshooting and tape table  (OPERATOR_MANUAL.md:27-28, 220, 181; vs vajra_config.yaml:109-114 and spec §8.4)  certainty: confirmed-by-reading
- Contradicts the operator's new requirement and the config's own comment ("the paper engine must generate trade decisions EVERY session"). Also `trade_small` is documented as "reduced size" in config:112 and spec but executes at full size (deploy_full_capital) - the doc promises behaviour the code does not have (brief A1). Must be rewritten for v2 (draft in 3.B).

### [MEDIUM] `requirements.txt` does not match imports  certainty: confirmed-by-reading + grep
- `numpy>=1.26.0` and `python-dateutil>=2.9.0` are never imported by any shipped module (grep over vajra/, tests/, root scripts; "numpy" appears only in a comment, tape.py:153). Remove them (smaller install on the operator's Windows box; dateutil is used by nothing in expiry.py).
- Missing: `pytest` (README/manual tell the operator to run `pytest -q`; 77 tests) -> add `requirements-dev.txt`. `uvicorn[standard]` pulls websockets/httptools/watchfiles - fine; nothing needs `httpx`. Upper bounds absent for fastapi/uvicorn/pyarrow/breeze-connect: a daily-run trading tool should ship a `constraints.txt` (pin what was green: pyarrow 26.0.0, fastapi 0.143.3, uvicorn 0.53.0, PyYAML 6.0.3 are what the review box uses).
- "Python 3.11+ (tested 3.13)": only 3.13.16 was run; `from __future__ import annotations` makes `str | None` safe on 3.9/3.10 anyway, so the 3.11 floor is conventional, not tested. Say "tested on 3.13 only".

### [MEDIUM] Demo / real / browser-replay entry points reference data that is not in the tree  certainty: confirmed-by-running (ls)
- `.claude/launch.json` starts `demo_dashboard.py` -> `RealSessionSource` requires `vajra/data/real_session_<date>.jsonl`; only `vajra/data/real_nifty_day.csv` exists. `--mode real` and the demo therefore crash with "no recorded session". Config `feed.real_session_date: "2026-06-16"` and `feed.mode: live` default also point nowhere.
- `replay_browser.py` default day `2026-06-30` (docs repeat it, manual:195) and `replay.py`/README examples use `tapes/2026-06-16` - neither exists; only 2026-10-14/15/16 do. Update examples to a day that ships (`2026-10-16`) and make the default "latest folder under tapes/".
- `.claude/settings.local.json` is operator-local (pip install allow rule) but is not in `.gitignore`; add `.claude/settings.local.json`.

### [MEDIUM] Code defaults silently differ from the yaml the docs describe  certainty: confirmed-by-reading
- `orchestrator.py:92` `trade_start` default "09:30" (yaml 09:21); `freshness_engine.py:26,57` default 1800 s / "30 min" (yaml 1200; docstring still says 30 min); `orchestrator.py:95` `post_stop_cooldown_secs` default 180 (yaml 660); `config.py:107` `nonexpiry_mode` default "observe" (yaml trade_small). A truncated/old yaml or a `--config` pointing at a minimal file would run the *old spec* silently. Fix: single `DEFAULTS` dict in config.py equal to the yaml; log "using default for X" as `CONFIG_DEFAULT_USED` event.

### [LOW] Docs claim approach window "~5 pts"; actual 7 (`entry.proximity_pts`, orchestrator.py:1030 uses `prox`)  (OPERATOR_MANUAL.md:62) certainty: confirmed-by-reading. Banner state `WATCHING` starts at 2.5x prox = 17.5 pts - undocumented.
### [LOW] `replay.py:55-56` re-implements the holiday merge without the `isinstance(list)` guard that `app.build_calendar` got (the comment at app.py:26-30 documents the crash). Only reached when EXPIRY_RESOLVED is absent from the event tape, but with `holidays_verified: true` it would raise in `parse_holidays(True)`. Reuse `build_calendar`.
### [LOW] `.gitignore` gaps: `*.bak` (tape backups, ~hundreds of MB), `vajra_results*.xlsx` and `ind_*.pdf` (manual §8 says exclude xlsx), `vajra/data/`, `vajra/.token`, `.claude/settings.local.json`, `tapes/**/*.jsonl` is covered only by `tapes/`.

## 2. Contradiction table (docs/comments vs code vs each other)

| # | Claim A (where) | Claim B (where) | Verdict / fix |
|---|---|---|---|
| 1 | Spec §3.2/§12: universe = 3 indices + 10 stocks. README:3-4 "10 heavyweight constituents" | yaml:15-17 = 21 stocks (24 instruments incl. indices); manual §1:11 "NIFTY + 23 other instruments (BANKNIFTY, FINNIFTY, 21 stocks)"; RUN_LIVE:3-4 "the 20 stocks"; yaml:11 + csv:3 comments "= 20 unique" | Real count is 21 (TCS added later, but not in csv). Manual's 23 is arithmetically right (2+21). README/RUN_LIVE/yaml comment/csv comment are stale. Make one sentence: "24 instruments: NIFTY, BANKNIFTY, FINNIFTY + 21 stocks" and generate it from config. |
| 2 | Spec §5.4/§12: `hold_min_secs: 1800` (30 min); ITERATIONS_LOG header "30-min interval"; ITER:97 "30-min timing"; ITER:5 | yaml:71 = 1200, README none, manual §1 "20 min", RUN_LIVE:39-40 "moved from 30-min"; freshness_engine docstring lines 26/57 say 30 | 20 min is live. Spec + ITERATIONS_LOG + freshness_engine docstring stale. |
| 3 | Spec: first calc 09:20 / trade 09:30 (ITER:5-6, ITER:97 "09:20 calc/09:30 trade"); orchestrator.py:92 default "09:30" | yaml:167, manual §1/§2.3, RUN_LIVE:39: trade 09:21 | 09:21 live; code default and ITERATIONS_LOG stale. With v2 non-expiry bands the 09:21 start is also on the open's widest-spread minute - see 3.A note. |
| 4 | Spec §7:252 and §10:294 "trailing (arms +16%, min 15%) + **breakeven stop** (+10% -> stop to entry) + hard stop ~12%" | README:58-ish table line "§7 ... invalidation, fast-exit, partial scale-outs"; manual §1/§4: arms +10%, floor +7%, give-back flat 6%, hard stop -12% pre-arm only, **no breakeven**; ITER:7-9 says breakeven removed post-loop | Spec §7/§10 stale vs operator rule. README §7 row describes the *superseded* original design (spec §7's own text says it was superseded). Fix README row: "trailing stop (arm +10%, floor +7%, flat 6% give-back) + hard stop -12%; no breakeven; flatten 15:25". Add a spec ERRATA block (do not silently edit; spec rule 0.3). |
| 5 | README:58 `vajra/signal.py` (§6 FIRE routing) | no such file; routing lives in `orchestrator.py` (`_approach_entry`, `_expiry_gate_ok`) | Remove row / point to orchestrator.py. |
| 6 | README:6-7,40 "Order placement is fully built ... routed to paper **until the operator flips** LIVE_ORDERS"; spec §0.6/§7 same | manual §1:14 "LIVE_ORDERS: false - **permanent**"; manual §4 "never flip without a full risk review"; yaml:118-122 "NOT part of normal operation" | Pick one policy statement: "paper-only by operator decision; the live-order code path exists but is unreviewed on real money (see ITERATIONS_LOG 6-7)." |
| 7 | README:10-11 "Fully independent of MYTHOS ... patterns re-implemented"; breeze.py header | Spec §0.7 "Reuse MYTHOS's Breeze wrapper. Do not re-implement" ; spec §0.3 "no silent spec changes" | Documented deviation inside code but not in spec. Add ERRATA to spec. |
| 8 | Manual §1:27-28, §7 table:220, §5 table:181 "zero trades on a quiet non-expiry day is correct / expiry-day system" | yaml:109-114 + operator's new requirement + routing `trade_small` "every session" | Rewrite (3.B). Also README line 3 "Expiry-day specialist". |
| 9 | yaml:112 + spec: `trade_small` = "reduced size off expiry" | code: `_expiry_gate_ok` returns True for trade_small and sizing is all-capital (execution deploy_full_capital) | trade_small only opens the gate (brief A1). Doc must say "same size"; v2 needs a real `nonexpiry_size_mult` or drop the word "small". |
| 10 | Manual §5:184-186 "one live day = 8 GB, tick_tape.jsonl ~6.5 GB, parquet twin ~0.4 GB; delete jsonl, keep parquet" | tapes/ shipped: parquet only, 0.39 GB (10-15), 0.41 GB (10-16), 0.23 GB (10-14); jsonl sidecars for the 6 columnar tapes are absent in the review tree; code (tape.py:90-94) writes a jsonl line for EVERY parquet row | Parquet figure is right; the 6.5 GB figure is unverifiable here and plausible (~16x text blow-up). BUT: replay of a day with its tick jsonl deleted silently switches to the parquet path (feed.py:82-91), fine; and tape.py comment "jsonl = durable source of truth" conflicts with advice to delete jsonl. Add: "after the close, delete `*.jsonl` except order/signal/event; keep `.bak`". Consider `tape.sidecar: false` for tick/level/basis (halves disk and live I/O; parquet flush every 5 s is crash-safe enough with row-group append + a footer-less file is NOT readable -> if sidecar dropped, write a new parquet file per flush). |
| 11 | Manual §2.2 `cd D:\ClaudePinRisk`; ps1 uses `$PSScriptRoot` | install dir name unknown (README says nothing) | Say "run from the repo root". |
| 12 | Manual:3 "Last updated 2026-07-06"; RUN_LIVE:9 "READINESS last confirmed 2026-07-01 ... PROVEN" | Three recorded Oct days show 15:15-15:20 feed freeze and entries at 15:20, `breeze_connect_failed` x3, pre-open seed from a June tape | Readiness claim is stale and over-confident. Replace with dated checklist + "known upstream issue: cash feed freezes ~15:15-15:20 (v2 guards it)". |
| 13 | RUN_LIVE:49-50 "Expiry-day ITM fallback is ACTIVE (decide keep/revert - on 9-day backtest it doubled expiry trades and turned them net-negative)" | manual §1 "expiry-day-only nearest-ITM fallback" as a locked fact; yaml silent | An unresolved decision recorded in a runbook as a TODO to the operator. Resolve and delete in v2. |
| 14 | RUN_LIVE:10-11 "54 [trades] on 06-30 alone"; ITERATIONS "9 tapes ... 44 trades" | different corpora (MYTHOS-recorded replay vs live tape) never distinguished; neither corpus ships | Label corpora; cite the three shipped Oct tapes for v2. |
| 15 | ITERATIONS_LOG table col "result 44 / 43.2% / +9.7%" all 15 rows; line 8-9 "after breakeven removal 44 trades / 52.3% / +7.5%, 3 profitable days"; tests "43->47"; "locked ... 30-min interval ... 09:30 trade" | now 77 tests, 20-min, 09:21, flat 6%, 15 s cooldown, 11-min cooldown (all after the log); log has no entries for 06-30 sweep, 07-01 adaptive tier, 07-02 revert | ITERATIONS_LOG ends ~06-29. Freeze it as history, header "STATE AS OF 2026-06-29"; start CHANGELOG.md (3.A) with a back-filled "1.x" table. |
| 16 | Manual §2.3 "Timeline ... 15:25 flatten -> 15:30 self-stop" ; no last-entry cutoff anywhere (orchestrator.py:93 `entry_cutoff = flatten_before`) | brief A2: entries at 15:20 on a broken feed | v2 adds `entry_cutoff` 15:10 (docs 3.B/3.C). Note code already has an `entry_cutoff` attr = flatten time - rename to avoid doc confusion. |
| 17 | sounds/README.md:3 "Drop **four** audio files"; table lists six; dashboard.js:244 tooltip "upper-approach / lower-approach / trade-taken / sister-breach" (4 of 6); README says "pick four that sound clearly different" | 6 files, 6 players (dashboard.py:456-459) | Fix to six; v2 will have five events (3.E) and a different file set. Durations: 2.4-6.0 s (stoploss.mp3 6.03 s, 193 KB; trade 5 s; sister 4 s) vs the doc's "SHORT 0.3-1.0 s" - sounds overlap each other, a root cause of "ting ting ting". |
| 18 | Manual §2.2 "click the 🔔 sound pill once" | pill initial text "🔇 sound off" (dashboard.py:244/464); clicking toggles; a click when ON turns it OFF | Say "click the pill so it reads 🔔 sound on". |
| 19 | Manual §2.4 "amber ... spot within ~5 pts" | proximity_pts 7; WATCHING <= 17.5 | Update (row above #17). |
| 20 | Manual §6: `replay_browser.py` "(default 2026-06-30, port 8770)"; README `replay.py --tape tapes/2026-06-16` | folders absent | see section 1. |
| 21 | Manual §6 `--mode replay --date` "engine-native replay" ; RUN_LIVE §6 "Review:" | overwrites live partition (CRITICAL above) | Remove. |
| 22 | Spec §9 "7 tapes"; README:61 "7 append-only ... tapes" | schema.PARQUET_TAPES = 6 + event_tape = 7 (README:66-68 lists them correctly); manual §5 table lists 7 | Consistent. (No change.) |
| 23 | Spec/README: `RECALC_TRIGGERS ∈ {prev_close_seed, open, interval, post_exit, held}`; manual §5:178 adds `post_cooldown` | schema.py:25 omits `post_cooldown`, orchestrator.py:565/573 writes it | Schema bug (cross-ref brief A3); manual is right, schema/spec wrong. |
| 24 | Manual §3.2 "`holidays_verified: true` only after verifying; while false prints loud warning" | yaml:174 true | Consistent. Config comment 2026-07-02 verification; Oct-20 Dussehra is on the list (Monday 19 Oct expiry confirmed by brief). |
| 25 | Manual §4 weights: "TCS missing -> zero voice + loud startup warning + tape event `MISSING_WEIGHT`" | csv has no TCS; manual §9 says add at next rebalance | Not a doc/code conflict but a standing known defect for months; fix now (add weight) and delete the caveat. |
| 26 | .claude/launch.json `vajra-dashboard` -> `python demo_dashboard.py` port 8766 | demo needs missing real_session files; and 8766 is the live port | Point launch.json at `python -m vajra.app --mode synthetic` or `replay_browser.py 2026-10-16 8770`. |
| 27 | Scripts' headers "(needs BREEZE_* env vars)" | credentials come from credentials.py first (env is a fallback, and the env var names are `BREEZE_API_KEY`... in yaml) | Fix comments (v2 flow in 3.D). |
| 28 | orchestrator.sh / ps1 title "mirrors the MYTHOS operator-run model" ; "coding agent must NOT read council/verdict files" | irrelevant to the operator; nothing reads such files | Delete boilerplate from user-facing scripts (keep in spec). |

Also noted, no action needed: manual "77 tests" and "6-gate harness" are accurate; `orchestrator.sh` is executable and `set -euo pipefail`-safe (`[[ -n ]] && ...` last-line `&&` under `set -e` is fine because it is not the final command); expiry/holiday text in manual §3 matches `expiry.py`/yaml.

## 3. Recommendations and drafted documents

### 3.D Daily SESSION_KEY procedure, v2 (operator flow + code sketch)
1. Pre-open (09:00): `.\run_vajra.ps1 -Login` (or `python -m vajra.login`). It prints `https://api.icicidirect.com/apiuser/login?api_key=<API_KEY>` (reads key from env/.env, never prints secret or token), then prompts `Paste apisession value or the whole redirect URL:`, extracts `apisession=` with `urllib.parse`, writes `vajra/.token` (`{"token": "...", "date": "2026-10-19"}`, chmod 600 / `icacls` on Windows), and does a cheap Breeze `get_funds`-style probe. No editing of .py files.
2. Resolution order in `config.breeze_secret`: env `VAJRA_SESSION_KEY` -> `vajra/.token` (reject if `date != today IST`: abort with "token is from <date>; run -Login") -> legacy `credentials.py` (print DEPRECATED, still reject empty). Same for API key/secret: `VAJRA_API_KEY`, `VAJRA_API_SECRET`, else `vajra/.env`.
3. `breeze_connect_failed` today leaves only a retry loop; add a distinct tape event `SESSION_TOKEN_STALE` when the failure is an auth error so the dashboard can say "TOKEN EXPIRED - run -Login" instead of a blank spot.
4. `.gitignore`: `vajra/.token`, `vajra/.env`, `*.token`; keep `vajra/credentials.py` listed; ship `vajra/credentials.py.example` with blanks and delete the real file from distributed trees. Rotate the Breeze secret once (the current file has been copied around: its docstring says "values copied from MYTHOS").
5. Manual §8 becomes: "share the tree as-is; secrets are not in it".

### 3.A CHANGELOG.md skeleton (actual text)

```markdown
# VAJRA changelog
Format: Keep-a-Changelog. Versions are config+code releases; tapes carry `schema_version` and `engine_version`.
Binding rule: any change that alters decisions on a recorded tape MUST list the measured delta on the three
Oct-2026 tapes (14/15/16) in its entry.

## [2.0.0] - <<YYYY-MM-DD>>
### Why
Operator reported (a) idle on non-expiry days, (b) too many sounds. Three-day tape review found: band half-width 90-132 pts
off expiry (3-6 minutes inside the 7-pt trigger per day); every recorded entry at 15:20 was taken on a frozen/garbage cash feed;
REVERSAL_WATCH 1,351x / FALSE_BREAK 12x in 23 s event floods.

### Added
- Non-expiry level cap: bands are clipped to `spot +/- levels.nonexpiry_max_halfwidth_pts` (<<45-60>>, config) measured at anchor time;
  tape columns `level_raw_upper/lower`, `level_cap_applied`, `halfwidth_pts`. (Expiry-day levels unchanged.)
- Regime field `regime` in level/decision tapes: EXPIRY | PRE_EXPIRY_1D | NON_EXPIRY (<<definition, e.g. trading days to expiry>>).
- Last-entry cutoff `execution.last_entry_time: "<<15:10>>"` (flatten stays 15:25).
- Feed-sanity gates: frozen-spot detector (<<N>> s unchanged spot with moving options, active <<15:15-15:20>> and all day at <<M>> s),
  implausible-basis gate (|basis| > <<pts>> or spot-vs-implied-future jump > <<pts>> in one tick) -> entries blocked, event `FEED_SUSPECT`,
  dashboard banner "FEED SUSPECT - entries paused".
- "Why no trade" strip on the dashboard: one line naming the first failing gate (<<list>>).
- Sound policy v2: backend-driven, rate-limited sound events (`sound_events` in latest_state); five synthesised sounds
  (entry, exit-profit, exit-loss, approach, feed-alarm). Sister-breach sound OFF by default (`sound.sister_breach: false`).
- `VAJRA_SESSION_KEY` env var / `vajra/.token` login flow; `-Login`, `-SkipVerify` switches; verify gate caches by source hash.
- `CHANGELOG.md`, `MIGRATION_1_to_2.md`, `engine_version` stamped in STARTUP_CONFIG.

### Changed
- `routing.nonexpiry_mode: trade_small` now actually sizes down: `execution.nonexpiry_size_mult: <<0.5>>` (or removed; decision pending).
- REVERSAL_WATCH and FALSE_BREAK are debounced (<<min re-arm secs / hysteresis pts>>).
- Replay never writes into the source partition.
- Schema: `RECALC_TRIGGERS` gains `post_cooldown`; `schema_version` 1.0.0 -> <<1.1.0>>.
- Docs: README, OPERATOR_MANUAL, RUN_LIVE rewritten; "zero trades off-expiry is correct" removed.

### Fixed
- <<list from reviewers: breeze._on_ticks falsy carry-forward, TCS weight, STALE_PREV_CLOSE_SEED handling, ...>>

### Removed
- Per-side approach sounds (upper/lower), sister-breach default, six old mp3s (kept in `vajra/sounds/legacy/`).
- numpy and python-dateutil from requirements (unused).

### Measured on tapes (fill from lead's replay harness)
| tape | v1 trades / P&L | v2 trades / P&L | entries after 15:10 | FEED_SUSPECT events |
|---|---|---|---|---|
| 2026-10-14 | <<>> | <<>> | <<>> | <<>> |
| 2026-10-15 | <<>> | <<>> | <<>> | <<>> |
| 2026-10-16 | 2 / +8,459.75 (reproduced) | <<>> | <<>> | <<>> |

## [1.x] - history (back-filled; not in git)
| date | change | evidence |
|---|---|---|
| 2026-06-27 | exits revised (trailing + breakeven + hard stop) | spec §7 |
| 2026-06-29 | 15-iteration hardening loop (ITERATIONS_LOG.md) | 47 tests |
| 2026-06-30 | sweep: NIFTY interval 30 -> 20 min; trade_start 09:30 -> 09:21; breakeven removed; trail arm +16 -> +10%, floor +15 -> +7% | 9-tape replay |
| 2026-07-01/02 | adaptive 12/9/6 give-back tried then reverted to flat 6%; post-profit cooldown 120 s -> 15 s | 06-30 tape |
| 2026-07-06 | REVERSAL_WATCH display run-length dedup; manual last updated | cockpit |
| 2026-10-14..16 | first three live tapes under test; 77 tests | this review |
```

### 3.B OPERATOR_MANUAL v2 sections

**New §1 replacement bullet (deletes the "structurally an expiry-day system" bullet):**
> VAJRA runs every session. On expiry day (Tuesday, or Monday when Tuesday is a holiday) it uses the full pin-risk band. On every other day it
> uses a *capped* band so that "approach" is physically reachable inside the day's range (§2A). Size and expectations differ by regime; silence
> is never assumed to be correct - if there is no trade, the dashboard tells you which gate said no (§2B).

**§2A — Non-expiry regime (final text with placeholders)**
> #### 2A. Non-expiry regime (Mon/Wed/Thu/Fri, and Tue-holiday weeks' Tuesday)
> **Why levels were too far.** The band is `K_CE + round(1.1 x VWAP(1-ITM CE)) - basis` (upper) and the mirror for puts. On expiry day the ITM VWAP is almost
> pure intrinsic value, so the band hugs spot (about +/-25-35 pts). On other days the same option carries 80-200 pts of time value, so the band sits
> 90-130 pts either side of spot (recorded 14-16 Oct 2026: median half-width 132 / 115 / 90 pts, median distance to the nearest level 100 / 67 / 70 pts). Price had to travel ~100 pts inside
> one 20-minute re-anchor window to come within the 7-pt entry trigger, which happened for 3-6 minutes per day.
>
> **What v2 does.** Levels are still computed exactly as before (the raw band is kept on the tape). When the regime is not EXPIRY, each level is
> then clipped to at most `levels.nonexpiry_max_halfwidth_pts` = **<<50>>** points from spot at the moment of anchoring:
> `upper = min(raw_upper, spot + cap)`, `lower = max(raw_lower, spot - cap)`; a level already closer than the cap is untouched. Levels stay HELD until the next re-anchor (every
> <<20>> min, plus post-exit / post-cooldown), so an approach can still be a real move rather than a re-centring artefact. On the day before expiry the cap is <<45>> pts; two-plus days out
> <<60>> pts (`levels.cap_by_regime`).
>
> **What you will see.** Level cells show a small `cap` tag when clipped (hover: raw level and by how much). The tape records `level_cap_applied`, `halfwidth_pts`, `regime`.
> **Sizing.** Off expiry the engine sizes at <<50% of capital / same as expiry>> (`execution.nonexpiry_size_mult`). Premium floor stays 45 (30 on expiry).
> **Expectations.** Expect <<N-M>> approaches per session off expiry instead of 0-1. More trades does not mean more edge: the first weeks are evidence-gathering
> (compare the `regime` column in the order tape before changing anything). Never loosen `entry.proximity_pts` to "get trades"; change the cap instead, via replay.
> **Operator-locked unchanged:** all-in sizing rule (scaled by the multiplier above if enabled), exits, cooldowns, 7-pt proximity, 09:21 start.

**§2B — Why-no-trade strip (text)**
> A single line under the banner, e.g. `NO TRADE - <<gate>>`. Gates are evaluated in this order and the first failing one is shown:
> 1 FEED SUSPECT (frozen spot / implausible basis) - 2 TOO EARLY (<09:21) - 3 PAST LAST ENTRY (<<15:10>>) - 4 COOLDOWN (stop 11 min / profit 15 s, with countdown) -
> 5 POSITION OPEN - 6 NO LEVEL IN RANGE (nearest level X pts, trigger 7) - 7 PREMIUM TOO THIN (< Rs 45/30) - 8 INSUFFICIENT CAPITAL - 9 REVERSAL WATCH.
> Every row is also written to `decision_tape.reason_code`. If the strip says `NO LEVEL IN RANGE` for hours while spot has moved, check the cap (§2A) before suspecting the engine.

**§2C — Sound policy v2 (final text)**
> #### 2C. Sound policy
> Sounds are generated by the engine (one `sound_events` list per round), not guessed by the browser from banner state, and each event type has a minimum gap.
> The pill must read **🔔 sound on**; click it once after every page load (browsers block audio until a click).
>
> | Event | Plays when | Sound (<<file>>) | Rate limit |
> |---|---|---|---|
> | ENTRY | a paper trade opens | two-note rising "tick-tock", <<0.6 s>> | once per trade |
> | EXIT_PROFIT | a trade closes with P&L >= 0 | single bright up-chime <<0.7 s>> | once per trade |
> | EXIT_LOSS | a trade closes with P&L < 0 | single low dull thud <<0.7 s>> | once per trade |
> | APPROACH | NIFTY first comes within <<proximity>> of a held level (either side, one sound) | soft single blip <<0.4 s>> | once per level per anchor; re-arm only after spot moves > <<20>> pts away or the level re-anchors; global minimum gap <<30>> s |
> | FEED_ALARM | feed suspect / data frozen / session token stale | three descending beeps <<1.2 s>> | once per episode; repeats every <<120>> s while it lasts, never more |
>
> **Off by default:** sister/stock breach (`sound.sister_breach: false`), upper-vs-lower approach distinction, "breach" sound, REVERSAL_WATCH, FALSE_BREAK, cooldown end.
> **Global rules:** at most one sound per <<2>> s; if a higher-priority event arrives the lower one is dropped (priority FEED_ALARM > EXIT_LOSS > EXIT_PROFIT > ENTRY > APPROACH);
> no sound while a position is open except EXIT_* and FEED_ALARM (approach chimes while you are already in a trade are noise). All sounds are < 1.5 s and synthesised
> (generated by `tools/make_sounds.py`, CC0, no download needed); the old six files are archived in `vajra/sounds/legacy/`.
> **Why you used to hear "ting, ting, ting":** the old page compared banner state every 250 ms, so a spot hovering on the approach threshold flipped `lean`/state back and forth and re-armed
> the sound each time; the same flicker produced 1,351 REVERSAL_WATCH events on 15 Oct. v2 makes the *backend* debounce the event and the browser only plays what it is told, once, by event id.
> **Mute rules:** `sound.quiet_hours` (<<09:15-09:20 off>>), pill off = silent but `FEED_ALARM` still flashes the page title.

**§2D — Feed health, last-entry cutoff (text)**
> **Feed health.** Each round the engine compares NIFTY cash spot with the option-implied future. Entries are blocked (banner `FEED SUSPECT`, event `FEED_SUSPECT`, sound FEED_ALARM) when
> (a) cash spot has not changed for more than <<20>> s while any option LTP changed (<<15:14-15:21 window tightened to 10 s>>), or (b) one tick moves spot by more than <<40>> pts while the implied future moves
> by less than <<10>>, or (c) |basis| > <<60>> pts. The block lifts after <<60>> s of consistent ticks. The frozen window seen at 15:15-15:20 on 14/15/16 Oct is an upstream Breeze behaviour; v2 defends against it, it cannot fix it.
> **Last entry.** No new trade after <<15:10>> (`execution.last_entry_time`). Open positions are still managed and are force-flattened at 15:25. The banner shows `PAST LAST ENTRY` after the cutoff.

### 3.C RUN_LIVE v2 checklist (replaces the file; same one-page style)
```
VAJRA 2.0 - LIVE CHECKLIST (attended, paper-only)
[ ] T-1 evening: python verify_vajra.py && python -m pytest -q  -> both green (stamp .verify_ok). Never run on tapes/<today>.
[ ] 09:00 python -m vajra.login   (prints Breeze login URL; paste apisession/URL; token -> vajra/.token). Do NOT edit .py files.
[ ] 09:05 .\run_vajra.ps1 [-Mode live]   (no -Date in live mode; the gate runs only if code changed; -SkipVerify for same-day restart)
[ ] Startup banner shows: mode live | session date | expiry + basis mode | REGIME <<EXPIRY|NON_EXPIRY>> | LIVE_ORDERS=False | engine_version 2.x
[ ] Event strip: STARTUP_CONFIG, EXPIRY_RESOLVED; NO SESSION_TOKEN_STALE, NO STALE_PREV_CLOSE_SEED (if seen: yesterday's tape missing -> pre-open levels are from an old day; wait for 09:20)
[ ] Click the sound pill to "sound on"; trigger test beep (button) once.
[ ] 09:20 first calc / 09:21 first possible entry. Non-expiry days: confirm level cells show the `cap` tag and half-width <= <<50>>.
[ ] Throughout: FEED SUSPECT banner => do not trust any open paper position, check ICICI. Why-no-trade strip tells you the gate.
[ ] 15:10 last entry, 15:25 flatten, 15:30 self-stop. After close: delete *.jsonl sidecars of tick/level/basis (keep order/signal/event), archive tapes.
[ ] Review ONLY via replay_browser.py <date> or replay.py --out tapes_replay. NEVER `--mode replay --date` (until fixed).
Known upstream issue: cash feed freezes ~15:15-15:20 on recorded days. Known: mid-day restart loses the paper position.
```

### 3.F README v2 outline ("what changed")
Keep: purpose, quick start, tapes. Change: title "Expiry-day specialist" -> "Pin-risk / gamma engine for every session (expiry-day band + capped off-expiry band)";
universe line generated ("24 instruments"); architecture table: delete `vajra/signal.py`, add `levels cap` module (`vajra/engine/level_cap.py`), `feed_health.py`, `sound_events.py`; §7 row -> real exit ladder;
add a "What changed in 2.0" section (4 bullets mirroring the CHANGELOG Why) and a "Safety" box (paper only, replay never writes live partitions, token handling).

### 3.G MIGRATION note (tapes 1.0 -> 2.0)
```markdown
# MIGRATION 1.x -> 2.0
Tapes: v1 tapes (schema 1.0.0; 2026-10-14/15/16) stay valid and replayable. v2 adds columns (all nullable for old rows):
 level_tape: regime, level_raw_upper, level_raw_lower, level_cap_applied, halfwidth_pts
 decision_tape: regime, no_trade_gate
 event types added: FEED_SUSPECT, FEED_RECOVERED, SOUND_EVENT, SESSION_TOKEN_STALE, GATE_SKIPPED, CONFIG_DEFAULT_USED
 RECALC_TRIGGERS += post_cooldown.   schema_version 1.0.0 -> <<1.1.0>>; STARTUP_CONFIG gains engine_version.
Reading old tapes with v2: missing columns are read as null; `regime` is derived from EXPIRY_RESOLVED (is_expiry_day) when absent. Replay of a v1 tape with v2 code is NOT expected to reproduce
 v1 P&L (cap, cutoff and feed gates change decisions) - use `--set levels.nonexpiry_max_halfwidth_pts=999 --set execution.last_entry_time=15:25 --set feed_health.enabled=false` to get the v1 behaviour
 (the lead's check: 10-16 must give 2 trades / +8,459.75).
Do not compare P&L across versions without the same tape set; keep v1 results in CHANGELOG as the baseline.
Config: new keys listed in CHANGELOG; v1 yaml still loads (defaults fill the new keys and emit CONFIG_DEFAULT_USED).
Operator actions: 1) new login flow; 2) delete old sound files not needed; 3) rotate Breeze secret; 4) update requirements (pip install -r requirements.txt); 5) archive tapes/ older than <<14>> days.
```
Acceptance: `--feed_health.enabled=false` etc. keys must exist so the v1-equivalence replay is possible (also useful as the regression anchor for the determinism gate).

## 4. Things that are fine and should not be changed
- Manual §3 expiry/holiday maintenance text matches expiry.py and the yaml (single `weekly_weekday` line, `holidays_<year>` merge, `holidays_verified`); the TUE-holiday->Monday annotations are right.
- `build_calendar`'s `isinstance(list)` guard and `write_startup_events` snapshot that drops the `breeze` section (no secrets in tape) are correct.
- `.gitignore` already covers `tapes/`, `logs/`, `vajra/credentials.py`, `.env`; `tape.py` never truncates (.bak rename) - keep.
- Gate-before-run is a good principle; only its frequency (every restart) and lack of an override are wrong.
- "77 tests / 6 gates / must be green" discipline; run order `pytest` then `verify`.
- `verify_vajra.py` is hermetic (TemporaryDirectory); safe to run any time.

## 5. Tests / checks I would add (docs & ops)
1. `test_docs_consistency.py`: parse README/manual/RUN_LIVE for the numbers 20/21/23, "30-min", "09:30", "signal.py" and fail if they contradict yaml (hold_min_secs/60, trade_start, len(universe)). Cheap and would have caught rows 1-5.
2. `test_cli_guards.py`: `app.main(["--mode","live","--date","2026-10-14"])` exits with an error; replay mode with source dir == tape dir raises.
3. `test_requirements.py`: AST-collect top-level imports of `vajra/`, `replay*.py`, `verify_vajra.py`, map to distribution names, assert subset of requirements.txt and no unused entries.
4. `test_config_defaults.py`: every `cfg.get(..., default=X)` literal in code equals the shipped yaml value (regex scan or a central DEFAULTS dict).
5. `test_ps1_ascii.py`: `run_vajra.ps1` is pure ASCII or starts with a BOM.
6. `test_token_flow.py`: env > .token > credentials; stale `.token` rejected; token never appears in tape STARTUP_CONFIG or logs.
7. `test_sounds_manifest.py`: every file referenced by the dashboard exists in `vajra/sounds/`, duration < 1.5 s (ffprobe or mutagen optional), README lists exactly those files.
8. `test_replay_never_writes_source`: hash the partition before/after a replay run; must be identical.
