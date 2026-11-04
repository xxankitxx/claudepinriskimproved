# PROJECT VAJRA — Cross-Instrument Pin-Risk / Gamma-Explosion Engine
### Build Specification v1.0
> **VAJRA 2.0 note (2026-10-17):** this is the original 1.0 build spec, kept for its theory. Operator
> revisions and the 2.0 changes (non-expiry MTM band, feed-sanity gate, engine-driven
> sounds, trimmed universe) are in [CHANGELOG.md](CHANGELOG.md) and [docs/REVIEW_DOSSIER.md](docs/REVIEW_DOSSIER.md).
> Working codename **VAJRA** (वज्र — the thunderbolt: a sudden, concentrated, directional strike). Rename freely; the name is not load-bearing.

> **Sibling to MYTHOS.** VAJRA reuses MYTHOS's ICICI Direct **BreezeConnect** integration patterns, its tape/logging discipline, and its binding agent-contract conventions. Where MYTHOS is a conviction-based directional buying engine that **stands down on expiry Tuesdays**, VAJRA is the **expiry-day specialist** that takes over exactly that regime. The two are designed to be non-overlapping in their peak-activity windows.

---

## 0. READ THIS FIRST — Agent Build Contract

This section binds the coding agent. It mirrors `MYTHOS_AGENT_CONTRACT.md`. Violations fail the completion gate.

1. **No stubs, no TODOs, no `pass`-bodies, no `NotImplementedError`** in any path that the orchestrator can reach. A function that is referenced is a function that is implemented.
2. **No feature shipped switched OFF by default.** If you build a module, it is wired into the live path and enabled, or it is not "done." Do not add a flag that defaults to disabled and then mark the work complete.
3. **No silent spec changes.** If something in this document is wrong, infeasible, or conflicts with Breeze reality, STOP and surface it in writing with the exact line. Do not quietly substitute your own design.
4. **No fabricated explanations.** If a number can't be computed (missing tick, stale chain), log it as a gap in the tape with a typed reason code. Do not invent a plausible value.
5. **Completion is gated by the tape.** A module is "done" only when it emits its specified tape rows on a real (or replayed) feed and a verification script confirms the rows are well-formed. See §11.
6. **`LIVE_ORDERS = False` during all evaluation loops.** Order placement code is fully built but routed to paper/dry-run until explicitly flipped by the operator.
7. **Reuse MYTHOS's Breeze wrapper.** Do not re-implement session handling, the websocket subscribe layer, or the VIX decimal-shift correction (Breeze returns e.g. `0.25` where the true India VIX is `25.0` — the ×100 / ÷100 normalization already solved in MYTHOS must be carried over, not re-derived).

---

## 1. Mission (one paragraph)

VAJRA continuously tracks **every** instrument in a defined universe — NIFTY, BANKNIFTY, FINNIFTY, and the heavyweight NIFTY constituents — computing, per instrument, the **pin-risk gamma-trigger band** and the **spot-to-future basis** in real time. When the weight of evidence across the universe says a **gamma-explosive move** is about to begin (a high-OI pin failing, the strongest hands forced to cover), VAJRA expresses the trade **on NIFTY options only**. It tracks the whole universe but bets on one instrument. Every tick, every computed level, every basis reading, every decision, and every order is written to an append-only **tape** so that history can be replayed and the logic refined.

---

## 2. Core Trading Thesis (the "why" — encode this, don't lose it)

This is the compressed theory the engine operationalizes. Each numbered fact maps to a module in §5.

**2.1 Pin risk, cash-settled form.** NIFTY/BankNifty/FinNifty options are **cash-settled** on a closing-window average, not the last tick. So whether a near-ATM option finishes ITM/OTM stays unresolved until the bell. Premium near ATM decays to pure gamma; tiny index moves produce outsized, hard-to-hedge P&L swings. High open-interest strikes act as **magnets** (dealer delta-hedging when long gamma is mean-reverting → pins price; when short gamma → amplifies → un-pins).

**2.2 The four factors.** Pin risk is the tug-of-war between (a) **Momentum / range expansion** and (b) **Volume** — which push price *off* a strike — versus (c) **Open Interest** concentration and (d) the **Gamma effect** — which pull it *back*. An explosion is the moment OI/gamma loses and momentum wins.

**2.3 The basis (implied vs actual future).**
- Option premiums are priced off the **forward/future**, not spot.
- **Non-monthly-expiry weeks**: no exchange-traded future matches that weekly expiry, so synthesize one via put-call parity → **implied future**.
- **Monthly-expiry week (last week of month)**: the weekly *is* the monthly; an **actual future** expires that day → **use the traded future price directly**, do not synthesize.
- Basis = Future − Spot. Premium (>0) or discount (<0). Basis **decays to zero by expiry close** (forced by cash settlement).

**2.4 Basis dynamics carry the signal, not the level.** The *level* of premium/discount is just carry — **no directional information**. The **rate of change** is the signal: an abrupt Δbasis (premium collapse or flip) often **precedes an intraday trend reversal**. Minor changes are noise. On expiry day the basis matters most in the **first half**; by the close spot and future converge and the basis tells you little.

**2.5 The gamma-trigger band (the panic levels).** Built from **ITM** option VWAPs because deep-pocketed (smart-money) writers sit in ITM strikes — the ITM padded breakeven is the **pain threshold of the strongest hands**. Weak OTM hands fold earlier; when the strong hands break and **buy back their shorts** (short calls covered into a rally → fuel up; short puts covered into a selloff → fuel down), the move feeds itself. A break of the ITM line is therefore higher-conviction than a break of an OTM line — **especially close to expiry, with volume behind it.**

**2.6 Greeks scaffolding.** Delta ≈ **probability of finishing ITM** (ATM ≈ 0.50, the coin in the air; rigorously N(d₁) slightly overstates the true N(d₂), but the gap is smallest precisely in the short-dated near-ATM expiry regime VAJRA lives in). **Gamma** = how fast delta moves = the *violence* of that 0.50 resolving toward 0 or 1. **Extrinsic value is highest at ATM and collapses toward zero into expiry**, while gamma spikes toward a near-singularity. The pin strike is the **max-gamma strike**; the explosion is a gamma event triggered when writers there are forced to delta-hedge/cover.

**2.7 Cross-instrument transmission (why track the whole universe).** Financials are **~35% of NIFTY**; the same five banks (HDFC Bank, ICICI Bank, SBI, Axis, Kotak) anchor NIFTY, BANKNIFTY *and* FINNIFTY. BANKNIFTY (β≈1.13 vs NIFTY) and FINNIFTY (β≈1.18) move *more* than NIFTY and can **lead** it. A confirmed gamma break in the financial bloc or in a top constituent is a **leading/confirming signal** for the NIFTY trade. VAJRA assembles NIFTY conviction from this weighted chorus.

---

## 3. The Universe (what to track)

Tracking is **universe-wide**. Execution is **NIFTY-only**. Source weights below are from the NSE factsheets dated 2026-05-29; weights drift on semi-annual rebalance (cut-offs Jan 31 / Jul 31) — **do not hard-code; refresh from a config file** the agent can update.

### 3.1 Indices (track all three)
| Index | Constituents | β vs NIFTY (1Y) | Role |
|---|---|---|---|
| NIFTY 50 | 50 | 1.00 | **Execution instrument** + primary signal |
| NIFTY BANK | 14 | 1.13 | Leading/confirming (financial bloc) |
| NIFTY FIN SERVICES | 20 | 1.18 | Leading/confirming (financial bloc) |

> BANKNIFTY and FINNIFTY overlap heavily — treat them as a **financial bloc** for conviction (do not double-count their agreement at full weight; see §5.6).

### 3.2 Heavyweight NIFTY constituents (track individually)
Weight column = **NIFTY 50 index weight** (this is the transmission coefficient to the NIFTY trade). Last two columns flag cross-membership.

| Stock | NIFTY wt % | In BANK | In FIN |
|---|---|---|---|
| HDFC Bank | 10.56 | ✓ (17.93) | ✓ (17.86) |
| ICICI Bank | 8.32 | ✓ (13.63) | ✓ (13.58) |
| Reliance Industries | 8.27 | — | — |
| Bharti Airtel | 5.20 | — | — |
| Larsen & Toubro | 4.43 | — | — |
| Infosys | 3.77 | — | — |
| State Bank of India | 3.71 | ✓ (9.07) | ✓ (9.04) |
| Axis Bank | 3.42 | ✓ (10.28) | ✓ (10.28) |
| Kotak Mahindra Bank | 2.62 | ✓ (9.81) | ✓ (9.39) |
| ITC | 2.56 | — | — |

**Reading of this table for the engine:**
- The **five shared banks** (~28.6% of NIFTY combined) are the dominant transmission channel — a coordinated gamma break across them + the financial bloc indices is the highest-conviction NIFTY-up/down precursor that exists.
- **Reliance (8.27%)** is the single largest *non-financial* mover — the one stock that can carry NIFTY against a quiet financial bloc. Track it as an independent channel.
- **Bharti Airtel, L&T, Infosys, ITC** are secondary independent channels (telecom / construction / IT / FMCG diversification).

> The universe is **configurable**. Default = the 3 indices + the 10 stocks above. The operator may extend it; the engine must not assume a fixed list length.

---

## 4. Data Layer (BreezeConnect)

Mirror MYTHOS. Build a thin `vajra_feed` over the MYTHOS Breeze wrapper.

**4.1 Subscriptions (per instrument, real-time websocket):**
- Underlying spot LTP (index level / stock LTP).
- Near-expiry **option chain** around ATM: at minimum the ATM strike ± N strikes for both CE and PE (N configurable, default ±6), with **LTP, VWAP, volume, OI, bid/ask** per strike.
- The **near-month future** for each instrument (needed for actual-future basis in monthly week; also useful as a sanity cross-check in other weeks).
- India VIX (regime context; apply the inherited decimal-shift fix).

**4.2 Required fields per option tick:** `ts, instrument, expiry, strike, right(CE/PE), ltp, vwap, volume, oi, bid, ask`. VWAP is **mandatory** — it is the input to the trigger band (§5.3). If Breeze does not stream VWAP for a strike, compute a rolling session VWAP from (price × volume) ticks in `vajra_feed` and tag it `vwap_source = "computed"` in the tape.

**4.3 Expiry-day mapping (critical, do not get wrong):**
- NIFTY **weekly options & futures expire Tuesday**. **If Tuesday is a market holiday, expiry shifts to Monday** (the day before). Do not flag a Tuesday/Monday expiry as an error.
- **Monthly expiry = last Tuesday of the month** (shifted to Monday if holiday). The monthly-expiry week is the one where the weekly coincides with the monthly and a **dated future** expires that session → triggers **actual-future basis mode** (§5.1).
- Maintain a holiday calendar in config; compute the active expiry date deterministically each session and write the resolved date + mode to the `event_tape` at startup.

---

## 5. Computation Engine (modules)

Each module is independent, testable, and emits tape rows. All formulas below are exact.

### 5.1 Basis Engine — `basis_engine`
Computes the spot-to-future basis per instrument, in the correct mode.

```
ATM_STRIKE       = strike nearest to spot (round spot to strike step)
                   NIFTY step = 50; resolve per-instrument step from chain.

# --- Mode selection ---
if (this session is the monthly-expiry session for the instrument):
    MODE   = "ACTUAL"
    FUTURE = traded near-month future price        # use it directly
else:
    MODE   = "IMPLIED"
    FUTURE = ATM_STRIKE + ( CE(ATM_STRIKE) − PE(ATM_STRIKE) )   # put-call parity

BASIS = FUTURE − SPOT            # >0 premium, <0 discount
```
Worked check (IMPLIED): spot 24110 → ATM 24100; CE=35, PE=15 → FUTURE = 24100 + (35−15) = **24120**; BASIS = +10 (premium). ✓

Emits → `basis_tape` (see §9).

### 5.2 Pin-Strike / ATM Locator — `pin_locator`
- ATM / pin candidate = max-gamma strike ≈ strike nearest the **future** (not raw spot — premiums live in forward space).
- Also surface the **highest-OI CE strike** and **highest-OI PE strike** within the tracked window (these are the literal OI magnets from §2.1/§2.2). Distance(spot → max-OI strike) is a context field.
- Emits the chosen ATM, max-OI-CE, max-OI-PE to `level_tape`.

### 5.3 Gamma-Trigger Level Engine — `trigger_engine`
The panic band. Built from ITM VWAPs, basis-adjusted to the spot chart.

```
PAD          = max(0.10, pad_floor)              # default 10% minimum pad
ROUND_TO     = level_round_step                  # default 5 pts
ITM_DEPTH    = strikes_in_the_money              # default 1 strike ITM

# --- Upper trigger from an ITM CALL (strike below spot) ---
K_CE        = ATM_STRIKE − (ITM_DEPTH × step)        # ITM for a call
RAW_UPPER   = K_CE + roundto( VWAP(K_CE) × (1 + PAD), ROUND_TO )
UPPER_LEVEL = RAW_UPPER − BASIS                       # translate forward→spot

# --- Lower trigger from an ITM PUT (strike above spot) ---
K_PE        = ATM_STRIKE + (ITM_DEPTH × step)        # ITM for a put
RAW_LOWER   = K_PE − roundto( VWAP(K_PE) × (1 + PAD), ROUND_TO )
LOWER_LEVEL = RAW_LOWER − BASIS
```

**Unified basis rule:** `LEVEL = RAW − BASIS` for **both** levels. Premium (BASIS>0) ⇒ subtract; discount (BASIS<0) ⇒ subtracting a negative adds. This reproduces "future at premium → deduct from both; at discount → add to both."

Worked check (premium 10): CE side 24050 + round(75×1.1)=24050+83→ use 82.5→24132.5≈24135; 24135 − 10 = **24125**. PE side 24150 − round(95×1.1)=24150−104.5≈24045; 24045 − 10 = **24035**. ✓

Emits the full level construction (K_CE, VWAP_CE, RAW_UPPER, UPPER_LEVEL, K_PE, VWAP_PE, RAW_LOWER, LOWER_LEVEL, BASIS, MODE, PAD, ITM_DEPTH) to `level_tape`.

> **Why these inputs (keep in code comments):** deep-ITM price ≈ intrinsic + thin extrinsic ⇒ a *stable* number tracking spot ~1:1 (high delta) ⇒ a clean breakeven line. The thin extrinsic bleeds via **theta** through the day, so the band **compresses toward spot** — which is why §5.4 must recompute it.

### 5.4 Freshness / Recalc Engine — `freshness_engine`  (operator-revised — FIXED 30-min interval)
Levels are only valid if computed at decision-time (theta moves them inward). The original
60s-scheduled + linger-triggered model was replaced by the operator with a fixed interval:

- **Pre-open seed:** U/L levels are seeded from the PREVIOUS day's close (spot + nearest ITM CE/PE).
- **First intraday recompute at 09:20** (`calc_start`); then **re-anchor every `hold_min_secs` (30 min)**.
- **Post-exit recompute:** immediately after each trade exits (NOT at entry — the held band we entered on is the BIG-MONEY reference).
- Levels are **HELD** between re-anchors (never refreshed tick-by-tick), so a moving spot can break a fixed level. `level_tape.recalc_trigger ∈ {prev_close_seed, open, interval, post_exit, held}` with `freshness_age_secs`.

### 5.5 Basis-Change Monitor — `basis_monitor`
Operationalizes §2.4.

```
D_BASIS        = BASIS_t − BASIS_(t-Δ)
D_BASIS_EWMA   = ewma(D_BASIS, halflife = basis_ewma_hl)     # default hl = 5 min
ABRUPT         = | D_BASIS − D_BASIS_EWMA | > abrupt_k × rolling_std(D_BASIS)
```
- `ABRUPT == True` ⇒ raise a **reversal-watch** flag (feeds §5.6 as a caution, not a standalone entry).
- Minor changes (below a `basis_noise_floor`) are ignored entirely.
- **Time weighting:** weight basis signals **high before 12:30, low after**; near the close they are near-useless (convergence). Encode a `basis_signal_weight(t)` that decays through the session.

### 5.6 Cross-Instrument Decision-Maker — `decision_maker`
**Stateful, EWMA-decaying directional-bias engine** (consistent with the MYTHOS R5 Decision-Maker). This is the gate.

Per instrument, derive a **directional state** each tick from §5.3/§5.4/§5.7:
```
state ∈ { NEUTRAL, APPROACH_UP, APPROACH_DOWN,
          BREACH_UP, BREACH_DOWN, CONFIRM_UP, CONFIRM_DOWN }
dir(instrument) ∈ {+1, 0, −1}     # from the state
strength(instrument) ∈ [0,1]      # how clean the break is (distance past level, volume, continuation length)
```

Weighted, decaying bias:
```
w(instrument):
    NIFTY 50          → w_self            (primary; default 1.0)
    Financial bloc    → w_bloc            (BANKNIFTY & FINNIFTY share a single bloc weight,
                                           default 0.35, scaled by their agreement; NOT summed at full weight)
    each heavy stock  → its NIFTY weight%/100   (HDFC 0.1056, ICICI 0.0832, RELIANCE 0.0827, ...)

raw_bias_t   = Σ  w(i) × dir(i) × strength(i)
BIAS_EWMA    = ewma(raw_bias_t, halflife = bias_ewma_hl)    # default hl = 3 min, stateful across ticks
```

**Divergence suppression:** if NIFTY's own band breaks **up** while the financial bloc is breaking **down** (or vice-versa), damp conviction — a lone-index break against the bloc is low quality. Implement as: `conviction *= (1 − divergence_penalty)` when sign(NIFTY dir) ≠ sign(bloc dir).

**Reversal-watch coupling:** if §5.5 `ABRUPT` fires *against* the current `BIAS_EWMA`, freeze new entries for `reversal_cooldown` (default 2 min) — an abrupt basis flip warns the prevailing intraday direction may be turning.

Emits the full per-instrument state vector, weights, `raw_bias`, `BIAS_EWMA`, every gate flag, and the fire/no-fire decision **with a typed reason** to `decision_tape`.

### 5.7 Breach + Continuation Validator — `continuation_validator`
The lesson: **the touch is not the signal; sustained continuation is.**

- A level is **breached** when spot closes beyond `UPPER_LEVEL` / `LOWER_LEVEL` by `break_buffer` (default 0.03% of spot) on the working timeframe (default 1-min, configurable to tick-aggregated).
- **Continuation confirmed** when, after breach, price **extends** in-direction for `confirm_secs` (default 120s) OR by `confirm_points` beyond the level (default 0.1% of spot), **with volume ≥** `confirm_vol_mult` × recent average (default 1.2×). On a non-momentum day the qualifying move may be a **slow grind** — confirmation is about *persistence*, not speed; tune `confirm_secs` up rather than requiring a velocity threshold.
- A breach that **snaps back** inside the band before confirmation = **false break** → no signal, log as `FALSE_BREAK`.

---

## 6. Signal Generation & Routing

```
FIRE_NIFTY(direction) WHEN all of:
    (1) NIFTY's own FRESH band is breached in `direction`          [§5.3 + §5.4]
    (2) Continuation confirmed on NIFTY                            [§5.7]
    (3) | BIAS_EWMA | ≥ bias_threshold  AND  sign(BIAS_EWMA)=direction   [§5.6]
    (4) No active reversal-watch freeze against `direction`        [§5.5/§5.6]
    (5) Expiry-day gating satisfied                                [§8]
```

**Conviction & sizing** scale with `|BIAS_EWMA|`, the number of cross-instrument channels in agreement, and proximity to expiry (gamma is sharpest into expiry). One position regime at a time (no stacking opposite signals).

**Two modes (config `routing_mode`):**
- `CONFIRM` (default, conservative): condition (1) NIFTY-own-break is **mandatory**; the chorus is a conviction multiplier + early-arming + false-break filter. *Evidence-first — do not fire on chorus alone.*
- `LEAD` (aggressive, opt-in): an **overwhelming** financial-bloc + shared-bank chorus (`|BIAS_EWMA| ≥ bias_threshold_lead`, higher) may pre-arm and fire NIFTY **before** NIFTY's own band breaks, on the transmission logic (§2.7). Defaults OFF; when enabled, every LEAD-mode fire is tagged in the tape for separate post-hoc evaluation.

> **Detection is universe-wide; the order is always NIFTY.** A setup spotted in BANKNIFTY/RELIANCE/etc. influences the NIFTY decision per the weights above — it never places an order in that instrument.

---

## 7. Execution Layer

- **NIFTY options only.** **ATM / ITM strikes** (consistent with the deep-pocket / high-delta philosophy — avoid far-OTM lottery strikes whose delta gives no participation). Default to ATM or 1-strike-ITM in the signal direction; configurable.
- Buy CE for an up-explosion, PE for a down-explosion (directional buying, MYTHOS-style payoff).
- Build full order placement over the MYTHOS Breeze order wrapper. **`LIVE_ORDERS = False`** until the operator flips it.
- **Exit model (operator-revised 2026-06-27 — supersedes the original invalidation/fast-exit/partial-scale-out design):** a **trailing stop** that books a MINIMUM 15% of the entry premium then ratchets up to capture the max (arms at +16% peak), a **breakeven stop** (once +10% green the stop moves to ENTRY), and a **hard stop** (~12%). No signal-collapse / invalidation / partial-scale-out exits — the operator explicitly removed those; exits are all-or-nothing on these three rails. A winner below the trail is HELD, not cut early.
- **Expiry close behavior:** as the close nears, convergence kills basis edge and gamma/theta dominate; tighten exits and **flatten before the closing-window average period** unless explicitly holding to settlement. Slippage/illiquidity risk rises after 2 PM — size and exit accordingly.

Emits every action to `order_tape`.

---

## 8. Expiry-Day Specifics (encode the slide rules)

1. **First half matters** for the basis read — weight §5.5 signals high before ~12:30, decaying after.
2. **Spot and future converge by close** — late-session basis is near-useless; lean on gamma/continuation, not basis, after ~2 PM.
3. **Post-2 PM is hard to monitor manually** — this is precisely the window VAJRA exists to run systematically; but raise execution caution (liquidity/slippage) in the same window.
4. **VAJRA is the expiry-day engine.** It runs every session but conviction and size **scale up into expiry**, peaking on the expiry session (Tuesday, or Monday when shifted). On non-expiry days it may run in observe/low-size mode (config `nonexpiry_mode ∈ {observe, trade_small, off}`, default `observe`).
5. Pin/gamma magnetism is strongest at the **max-gamma ATM strike** with the **largest OI** nearby — surface and log proximity to those magnets continuously (§5.2).

---

## 9. The Tape (logging — the whole point)

All tapes are **append-only, millisecond-timestamped, partitioned by trade date**, written in a replayable columnar format (parquet preferred; jsonl acceptable for events). Every row carries `schema_version`. The tapes must be sufficient to **fully replay a session offline** and re-derive every decision. No decision may exist that is not reconstructable from the tape.

**9.1 `tick_tape`** (high-frequency, per instrument): `ts, instrument, spot, atm_strike, future, basis, basis_mode, vix, [per-tracked-strike: strike, right, ltp, vwap, vwap_source, volume, oi, bid, ask]`.

**9.2 `level_tape`** (each computation/recalc): `ts, instrument, atm_strike, max_oi_ce, max_oi_pe, k_ce, vwap_ce, raw_upper, upper_level, k_pe, vwap_pe, raw_lower, lower_level, basis, basis_mode, pad, itm_depth, round_to, recalc_trigger{prev_close_seed|open|interval|post_exit|held}, freshness_age_secs`.

**9.3 `basis_tape`**: `ts, instrument, spot, future, basis, basis_mode, d_basis, d_basis_ewma, abrupt_flag, basis_signal_weight`.

**9.4 `decision_tape`** (the brain's state, every evaluation): `ts, [per-instrument: instrument, state, dir, strength, weight], raw_bias, bias_ewma, bias_threshold, routing_mode, divergence_flag, reversal_freeze_flag, expiry_gate_flag, decision{FIRE_UP|FIRE_DOWN|HOLD}, reason_code`.

**9.5 `signal_tape`**: `ts, direction, conviction, nifty_level_crossed, continuation_confirmed, confirm_secs_elapsed, channels_in_agreement[list], routing_mode, lead_mode_flag`.

**9.6 `order_tape`**: `ts, action{ENTRY|EXIT|REJECT}, instrument(=NIFTY), strike, right, qty, intended_price, fill_price, slippage, order_id, live_orders_flag, exit_reason, pnl_realized`.

**9.7 `event_tape`** (jsonl): startup config snapshot, resolved expiry date + basis mode, holiday decisions, feed gaps (typed reason codes — never fabricate the missing value), API/session errors, anomalies, reversal-watch fires.

> **Refinement loop:** because every input and every gate state is on the tape, a future session can replay `tick_tape` through the same modules, perturb parameters (`PAD`, `ITM_DEPTH`, `bias_threshold`, `confirm_secs`, weights, `routing_mode`), and measure outcome deltas against `order_tape` — the tape is the substrate for tuning. Build a `replay.py` that consumes the tapes and re-runs §5 deterministically.

---

## 10. Risk & Guardrails

- One position regime at a time; no opposite-signal stacking (the single-position rule is also what limits re-entry).
- Exits = trailing stop + breakeven + hard stop (§7, operator-revised). No invalidation / fast-exit / partial-scale-out.
- Divergence suppression and reversal-watch freeze as specified (§5.6).
- No far-OTM strikes; ATM/ITM only.
- Convergence-aware late-session behavior (§8).
- `LIVE_ORDERS` defaults False.
- Entry is anticipatory and operator-revised: fire on EVERY approach within ~5-7 pts of a HELD level OR on a break-through; NO continuation / bias / opposite-side veto gate (the cross-instrument confluence is screamed + logged, never blocks). The anti-goalpost guard (§5.4, hold the levels) remains non-negotiable.

---

## 11. Build Constraints & Completion Gates

A `verify_vajra.py` gate script must pass before any module is "done":
1. **No-stub scan** — fail on `pass`-only bodies, `TODO`, `FIXME`, `NotImplementedError`, `raise NotImplemented`, in any orchestrator-reachable path.
2. **Tape-emission proof** — replay a recorded or synthetic feed; assert each tape (§9.1–9.7) is produced with the exact specified columns, correct dtypes, `schema_version`, and millisecond timestamps.
3. **Formula golden tests** — assert the §5.1 and §5.3 worked examples reproduce exactly: implied future 24120 / basis +10; upper 24125; lower 24035. Add discount-mode mirrors.
4. **Expiry-mapping test** — feed a holiday-Tuesday calendar; assert expiry resolves to Monday and basis mode flips to ACTUAL in the monthly week.
5. **Determinism test** — `replay.py` over a fixed tape yields byte-identical `decision_tape` across two runs.
6. **Default-on check** — assert no shipped module is reachable only behind a default-off flag (other than `LIVE_ORDERS` and explicitly-opt-in `routing_mode=LEAD`).

The orchestrator is an operator-run shell script (mirror the MYTHOS orchestrator). The coding agent must not read council/verdict files (separation of powers — carry over from HAWK).

---

## 12. Config Defaults (single source of truth — `vajra_config.yaml`)

```yaml
universe:
  indices: [NIFTY, BANKNIFTY, FINNIFTY]
  stocks:  [HDFCBANK, ICICIBANK, RELIANCE, BHARTIARTL, LT, INFY, SBIN, AXISBANK, KOTAKBANK, ITC]
  refresh_weights_from: nifty_weights.csv        # updated on rebalance; NOT hard-coded

chain:
  strikes_each_side: 6

basis:
  ewma_halflife_min: 5
  abrupt_k: 2.5
  noise_floor_pts: 1.0
  signal_weight_decay: { full_until: "12:30", near_zero_after: "14:00" }

trigger:
  pad_floor: 0.10
  level_round_step: 5
  itm_depth_strikes: 1

freshness:
  hold_min_secs: 1800        # FIXED 30-min recompute interval (09:20 first, then every 30 min)
  linger_band_pct: 0.05      # proximity band for the per-instrument APPROACH state (§5.7)

decision:
  w_self: 1.0
  w_bloc: 0.35
  bias_ewma_halflife_min: 3
  bias_threshold: <calibrate_on_tape>
  bias_threshold_lead: <calibrate_on_tape, higher>
  divergence_penalty: 0.5
  reversal_cooldown_min: 2

continuation:
  break_buffer_pct: 0.03
  confirm_secs: 120
  confirm_points_pct: 0.10
  confirm_vol_mult: 1.2

routing:
  routing_mode: CONFIRM            # CONFIRM | LEAD
  nonexpiry_mode: observe          # observe | trade_small | off

execution:
  strikes: ATM_or_1ITM
  LIVE_ORDERS: false
  # exits: trailing stop + breakeven + hard stop (operator-revised; no partial_scale_out / invalidation)
```

> All thresholds marked `<calibrate_on_tape>` are to be set from replay analysis, **not guessed**. Ship them as explicit config values once calibrated, never silently hard-coded in the engine.

---

### Appendix A — Quick formula card
```
ATM           = round(spot / step) × step
IMPLIED_FUT   = ATM + (CE_ATM − PE_ATM)         # non-monthly weeks
ACTUAL_FUT    = traded future                    # monthly-expiry week
BASIS         = FUT − SPOT
UPPER_LEVEL   = [ K_CE + round(VWAP_CE × 1.10) ] − BASIS     # K_CE = ATM − depth·step
LOWER_LEVEL   = [ K_PE − round(VWAP_PE × 1.10) ] − BASIS     # K_PE = ATM + depth·step
SIGNAL_UP     = fresh UPPER_LEVEL breached + continuation + bias_ewma ≥ thr (up)
SIGNAL_DOWN   = fresh LOWER_LEVEL breached + continuation + bias_ewma ≤ −thr
ORDER         = always NIFTY, ATM/ITM, direction of signal
```
