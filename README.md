# VAJRA 2.1 — Cross-Instrument Pin-Risk / Gamma-Explosion Engine

> **What's new in 2.1** (details: [CHANGELOG.md](CHANGELOG.md)): the Breeze websocket is **supervised on its own
> thread** (reconnects never stall the engine; every stream token re-joined in one packet; the SDK's all-day
> "Could not authenticate" refusal latch is cleared) · **crash-safe tapes** (closed 60-s parquet parts, merged at
> the close; a killed session loses ≤ 60 s, not the day) · no more 9 GB/day jsonl sidecars · a **new cockpit**
> (`vajra/static/`) · `--pace` replay · `tools/repair_tapes.py`.
>
> **What's new in 2.0** (evidence: [docs/REVIEW_DOSSIER.md](docs/REVIEW_DOSSIER.md)):
> trades **non-expiry days** with narrower mark-to-market levels, all-in as always (expiry day unchanged) ·
> **feed-sanity gate** (no trades/re-anchors on a frozen or garbage feed) ·
> **five short, rate-limited sounds** decided by the engine (no more sister-breach "ting ting") ·
> **WHY NO TRADE** strip on the dashboard · `--token` for the daily Breeze key · safe, streaming replay.

Pin-risk engine for NIFTY options — expiry-day specialist that, from 2.0, also trades non-expiry days. Tracks the **whole universe**
(NIFTY / BANKNIFTY / FINNIFTY + 21 heavyweight stocks) and computes, per
instrument, the **gamma-trigger band** and the **spot-to-future basis** in real
time; when cross-instrument evidence says a **gamma-explosive move** is starting,
it expresses the trade on **NIFTY options only**. Every tick, level, basis read,
decision and order is written to an append-only, replayable **tape**.

Built to `VAJRA_PIN_RISK_ENGINE_SPEC.md`. Fully **independent** of MYTHOS (no
cross-folder imports); the Breeze integration *patterns* are re-implemented here.
Runs on **localhost:8766** (MYTHOS owns 8765).

> **Binding proof = recorded live tape.** Synthetic data and unit tests guard the
> deterministic formulas/plumbing only; they are *not* proof of trading behaviour.
> The daily recorded session tape (`tapes/<date>/`) is what gets reviewed.

---

## Quick start

```bash
pip install -r requirements.txt

# completion gate (spec §11) — must be green before any session
python verify_vajra.py

# dev smoke on synthetic (NON-BINDING) data + dashboard on http://127.0.0.1:8766
python -m vajra.app --mode synthetic

# live session (records the binding tape). API_KEY / API_SECRET live in vajra/credentials.py;
# pass TODAY's Breeze session key on the command line (no file edit needed):
.\run_vajra.ps1 -Token 12345678                  # Windows
python -m vajra.app --mode live --token 12345678  # any OS
.\run_vajra.ps1 -Token 12345678 -SkipVerify      # mid-day restart (skips the ~20 s gate)

# replay a recorded session deterministically (and perturb params to tune)
python replay.py --tape tapes/2026-10-15 --out tapes_replay --set nonexpiry.mtm_pad=0.25
#   (streams the tape; refuses to write into tapes/ or into a non-empty --out folder)

# 2.1: watch a recorded day in the cockpit at tape pace (1 = real time) on a spare port
python -m vajra.app --mode replay --date 2026-10-30 --pace 1 --port 8790

# 2.1: a day whose session was killed -> merge leftover parquet parts / rebuild a footer-less 2.0 file
python tools/repair_tapes.py tapes/2026-11-02 --rebuild-from-jsonl
```

> **Python**: use the interpreter that has the dependencies installed (on the operator PC that is
> Python 3.13, `py -3.13`); a bare `python` may resolve to another install.

Order placement is fully built but routed to **paper** until the operator flips
`execution.LIVE_ORDERS: true` in `vajra_config.yaml`.

---

## Architecture (spec section → module)

| Spec | Module | Role |
|---|---|---|
| §3 | `nifty_weights.csv`, `vajra/config.py` | universe + weights (refreshed from NSE factsheets; not hard-coded) |
| §4 | `vajra/breeze.py`, `vajra/feed.py` | self-contained Breeze wrapper (session/ws/**VIX ÷100 fix**) + feed (live/replay/synthetic) |
| §4.3 | `vajra/expiry.py` | Tuesday-weekly mapping, holiday→Monday shift, monthly→ACTUAL |
| §5.1 | `vajra/engine/basis_engine.py` | spot-to-future basis (IMPLIED parity / ACTUAL future) |
| §5.2 | `vajra/engine/pin_locator.py` | max-gamma ATM + OI magnets |
| §5.3 | `vajra/engine/trigger_engine.py` | ITM-VWAP gamma-trigger band (expiry) + 2.0 MTM non-expiry band (narrow-only) |
| §5.4 | `vajra/engine/freshness_engine.py` | fixed-interval re-anchor (NIFTY 20 min, sisters 3 min) + post-exit / post-cooldown |
| §5.5 | `vajra/engine/basis_monitor.py` | Δbasis EWMA, ABRUPT reversal-watch, time weighting |
| §5.6 | `vajra/engine/decision_maker.py` | stateful EWMA-decaying cross-instrument bias — **the gate** |
| §5.7 | `vajra/engine/continuation_validator.py` | breach + continuation, FALSE_BREAK, per-instrument state |
| §6 | `vajra/orchestrator.py` | anticipatory entry (7 pts of a held level), ordered entry gates, NIFTY-only |
| §7 | `vajra/execution.py` | current-ATM directional buying; exits: hard stop −12% / trail arms +10%, +7% floor, 6% give-back / 15:25 flatten |
| 2.0 | `vajra/regime.py` | EXPIRY vs NON_EXPIRY, trading days to expiry, sizing / caps |
| 2.0 | `vajra/feed_health.py` | NIFTY frozen-spot / implausible-basis / jump gate |
| 2.0 | `vajra/alerts.py` | engine-decided, rate-limited sound events |
| §8 | `vajra/orchestrator.py`, `vajra/timeutil.py` | expiry-day gating, late-session/flatten behaviour |
| §9 | `vajra/tape.py`, `vajra/schema.py` | 7 append-only, ms-stamped, partitioned, replayable tapes |
| §11 | `verify_vajra.py` | completion gate (no-stub / tapes / golden / expiry / determinism / default-on) |
| §9/§11.5 | `replay.py` | deterministic re-run + parameter perturbation |

### The tapes (§9) — `tapes/<YYYY-MM-DD>/`
`tick_tape`, `level_tape`, `basis_tape`, `decision_tape`, `signal_tape`,
`order_tape` (parquet; the small tapes also keep a crash-safe `.jsonl` sidecar) and `event_tape.jsonl`.
2.1: while a session runs, each parquet tape is a series of closed 60-second part files
(`<tape>.part-NNNNN.parquet`), merged into `<tape>.parquet` at the close — or at the next start / read
if the session was killed. A same-day restart keeps the earlier session as `<tape>.parquet.<ms>.bak`.
Every row carries `schema_version` and an int64 millisecond `ts`. The set is
sufficient to fully replay a session offline and re-derive every decision.

---

## Signal logic (operator-revised, 2.0)

Enter when NIFTY comes within `entry.proximity_pts` (7) of a **held** level, or breaks through it,
unless one of the ordered gates is closed. The first closed gate is shown as **WHY NO TRADE** and
written to `decision_tape.reason_code`: in a trade → before 09:21 → at/after 15:25 → no
levels → feed unhealthy → levels stale → non-expiry off → post-stop cooldown →
post-profit pause → no approach. The cross-instrument confluence is advisory (screamed and logged,
never a veto).

## Calibration note (§12)

`decision.bias_threshold` / `bias_threshold_lead` are spec-marked
`<calibrate_on_tape>`. They ship as **explicit PROVISIONAL** values with
`thresholds_calibrated: false`; the engine reads them (never hard-codes them),
emits a `THRESHOLDS_PROVISIONAL` event at startup, and the active threshold rides
on every `decision_tape` row. Calibrate from recorded tape via `replay.py`, set
the values, flip the flag.

## Config

Single source of truth: `vajra_config.yaml` (all spec §12 defaults verbatim).
Weights: `nifty_weights.csv` (validated against the three NSE factsheets dated
2026-05-29; betas NIFTY 1.00 / BANK 1.13 / FIN 1.18).

## Tests

```bash
python verify_vajra.py     # the binding completion gate (7 gates; gate 7 = non-expiry on real data)
pytest -q                  # 102 tests (formulas, expiry, modules, determinism, VAJRA 2.0 behaviour)
```
