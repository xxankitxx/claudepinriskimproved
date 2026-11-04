# 10 — Replay, determinism and completion-gate teardown
Verdict: Replay itself is solid and ready for Monday (deterministic, streaming, flat memory, refuses to clobber tapes), clearly better than 1.0. The verify_vajra.py gates pass but several prove less than they claim; none blocks Monday.
Score (your area): 1.0 = 4/10, 2.0 = 7.5/10

## Findings (most severe first)

### [MEDIUM] Gate 5 "determinism" is weak: synthetic data, one tape, one process (verify_vajra.py:~255-262)
- It replays a synthetic session twice in the SAME process and compares only decision_tape.jsonl. order_tape, signal_tape, level_tape, parquet files, event_tape are not compared. Same process means the same PYTHONHASHSEED, so set-ordering nondeterminism is invisible. Real tapes are never used (Gate 7 runs the engine once).
- Reproduced independently on REAL data (see checks below): two separate processes with PYTHONHASHSEED=1 vs 999 over a 5-min 15-Oct window gave byte-identical basis/decision/level/order/signal/tick in both jsonl and parquet. So the property holds today; the gate just would not catch a regression.
- Fix: replay the 2 MB fixture twice in separate subprocesses with different hash seeds; hash every tape except event_tape.

### [MEDIUM] event_tape is not deterministic: REGIME event uses wall clock (orchestrator.py:182, tape.py:154)
- `self.tape.event("REGIME", ...)` in `Orchestrator.__init__` passes no ts_ms, so it gets `now_ms()`. In my two replays event_tape.jsonl differed on line 1 (ts 1792210948887 vs ...962718, i.e. real clock 2026-10-17, not tape time). Every other tape was identical. Not in the decision path, but it breaks "byte-identical replay" for anyone diffing the whole directory, and it puts a wrong-date row in replay output.
- Fix: pass `ts_ms` from the first round (or emit REGIME lazily on round 1), or give the replay a clock.

### [MEDIUM] Gate 7 asserts very little and rests on a 2 MB fixture (verify_vajra.py:358-406)
- Checks: regime == NON_EXPIRY, entries >= 1, no entry after 15:15, a feed_sanity FEED_GAP exists. It does not check entry count/prices, P&L, all-in sizing (qty == floor(cap/(prem*65))*65), stop/trail exits, cooldowns, the 15:25 flatten, or that exits happen. "entries >= 1" passes with any single entry. Uses a hand-rolled writer that swallows everything, so schema/typing errors in order_tape rows go unnoticed here.
- Fixture is tests/fixtures/2026-10-15 (2 MB, read-only dir). It is one day, one morning window, and contains no expiry-day data at all, so the Monday 19 Oct expiry path (ACTUAL/IMPLIED expiry formula, 1.1*VWAP level) has no real-data gate. Gate 7's own message calls it "all-in" but nothing verifies that.
- Fix: add assertions on lots (qty % 65 == 0 and qty*fill <= capital), exit reasons, a golden order_tape hash for the fixture, and a flatten check.

### [MEDIUM] Gate 6 only checks `false` booleans (verify_vajra.py gate_default_on)
- A feature disabled by `0`, `null`, `""`, `"off"`, or by a never-read key passes. It also whitelists `holidays_verified: false` and `thresholds_calibrated: false`, so "default-on" is partly by exemption. It cannot detect code that is simply unreachable. Cosmetic assurance, not proof.

### [LOW] Gate 1 is a text scan (verify_vajra.py:50-100)
- Substring "TODO" etc. anywhere (including comments/strings) fails, but stub forms like `return None` bodies, `raise Exception("unimplemented")`, or `...` inside a class are not caught. SCAN_TARGETS = ["vajra","replay.py"] omits replay_browser.py, verify_vajra.py, tools/. Passes today.

### [LOW] Gates 3/4 are narrow but correct
- Golden numbers (24120 / +10 / 24125 / 24035; discount 24145/24055) and holiday-Tuesday/monthly logic pass. They test only the IMPLIED example, one expiry shift pair, and not the locked expiry-day formula `K ± round(1.1*VWAP) - basis` against real data. Good unit checks, not an integration proof.

### [LOW] ReplaySource has no prev_close_snapshot, so replay is not a faithful rerun of live pre-open (feed.py:76-115, orchestrator.py:909)
- Live seeds pre-open levels from the prior day; `hasattr(source,"prev_close_snapshot")` is False for ReplaySource, so replay levels are unseeded until the first anchor. Only matters for 09:15-09:21 (no entries before 09:21). Also real tapes start late (14-Oct 13:26, 15-Oct 09:41, 16-Oct 11:11 IST), so full-day "09:15" replays are impossible on this data.

### [LOW] replay_browser.py re-implements round grouping and swallows bad rows (replay_browser.py:37-52)
- Own `StreamReplay` instead of `ReplaySource` (code drift risk); `except Exception: pass` silently drops unparsable instrument rows; hard-coded default day 2026-06-30; paces with sleep(0.003) so a full day takes ~4+ min plus compute; reads tapes/<date> relative to the script. It writes nothing (NullWriter), so it cannot damage tapes. Its output is not compared to replay.py, so the two can diverge unnoticed.

### [LOW] Disk footprint of a real replay (tape.py jsonl sidecars)
- 5 min of 15-Oct produced tick_tape.jsonl 98.6 MB + level_tape.jsonl 14.7 MB + basis 6.8 MB + decision 2.9 MB. Extrapolated: ~7+ GB jsonl for a full day replay (the jsonl sidecars are the part the manual says to delete). Fine on a 8 hr live run but a full-day replay needs ~8 GB free. Fix: a `--no-sidecar` option for replay.

## Replay safety (answers to the questions)
- Memory: ReplaySource streams `iter_batches(20000)` (feed.py:80-96). 60-min real window (13,261 rounds, 318k rows): RSS 66 MB base -> ~545 MB by round 2000, then flat/declining (527 -> 258 MB at end). No growth trend, so a full day is memory-safe (<1 GB). CPU: ~3.4 ms/round, linear (8 cpu-s per 2000 rounds at every checkpoint, no O(n^2)); full day ~80k rounds ≈ 5 CPU-min (wall 4x longer under 20-reviewer load). 1.0 builds a Python list of the entire day (its own comment: 11-34 GB); 2.0 fixes that.
- Refuses to damage tapes: `--out` equal to the tape's parent root -> SystemExit (tested, message printed). Non-empty out day dir -> SystemExit (tested, "already has files"). Remaining gaps: guard compares only exact resolved parent, so a different path to the same data via symlink/bind would still pass; fine in practice. TapeWriter additionally renames an existing parquet to `.bak` rather than truncating (tape.py:~87-97). 1.0 had neither guard (it appended, doubling trades). Fixtures dir is read-only (dr-xr-xr-x).
- Hash-seed independence confirmed (see below).
- Row grouping relies on equal ts per round: verified 1197/1197 rounds had exactly 24 instruments with one shared ts, zero non-monotonic rows in the window. A tape where one instrument's ts differs by 1 ms would silently split a round into two (feed.py:105-113); the live recorder apparently stamps one ts per round, but replay has no sanity check.

## What 2.0 does better than 1.0 (evidence)
- Streaming replay (no whole-day list); out-dir guards; `_read_expiry_from_events` fallback no longer crashes on `holidays_verified` bool (replay.py diff vs 1.0: shared `build_calendar`); tape writer on background thread, backups instead of truncation; Gate 7 added real-data gate; ANSI fix on Windows; undeclared event types no longer kill the session (tape.py:~148).
- Full test-suite: 101 passed in 200 s.

## What 2.0 does worse than 1.0
- Nothing material in replay. The only regression-ish item: REGIME wall-clock ts in event_tape (new in 2.0 since the REGIME event is new).

## Tests / checks ran
- `python3 -B verify_vajra.py` (in a writable copy at /home/claude/teardown/scratch/10/snap): ALL 7 GATES PASS in 6.5 s. Gate 2 saw 6 parquet tapes with exact schema; Gate 5 byte-identical 381,891 B decision_tape; Gate 7: 4 entries, mtm band, feed-sanity tripped.
- Real-data replay x2 (15-Oct 10:00-10:05 IST, 28,728 rows, 1,197 rounds) in separate processes, PYTHONHASHSEED=1 vs 999: `cmp` -> SAME for basis/decision/level/order/signal/tick in both .jsonl and .parquet; event_tape.jsonl differs only in the REGIME wall-clock ts. Result: 1 entry (NIFTY 22850 CE, qty 650 = 10 lots, fill 143.55) and 1 exit.
- 60-min window memory/CPU profile (above). Result: 3 trades, equity 114,573.
- `pytest -p no:cacheprovider tests`: 101 passed.
- Guard tests: replay --out same root -> refused; second run into existing out -> refused.
- Did not run a full-day replay (window-based, per instructions); did not run replay_browser.py (reads only tapes/ under its own dir, no tapes dir in snapshot).
