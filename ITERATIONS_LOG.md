# VAJRA — 15-iteration improvement loop (council → implement #1 → re-run tapes)

Locked (never touched): all-in sizing, lot 65, paper-always, no-veto, exits = trailing (arms +10%, +7% min floor,
6% give) + hard stop (-12%) ONLY, no REVERSED/scale-out/BIG-MONEY/BREAKEVEN, 30-min interval + 09:20 calc/09:30
trade, real-data-only, premium floor.

Operator change (post-loop): REMOVED the breakeven exit (operator never requested it — it was assistant-added via a
pre-framed menu). Trailing now arms at +10% (was +16%) and secures a +7% minimum (was +15%). Result on 9 tapes:
44 trades / 52.3% win / +7.5% avg-daily / 3 profitable days (was 43.2% / +9.7% / 2). More consistent, less peak-capture.

| iter | council % to req | #1 implemented | result (trades / win% / avg-daily) | tests/gate |
|------|------------------|----------------|-------------------------------------|------------|
| 1 | 84% | Declare APPROACH_ALERT in EVENT_TYPES + self-enforcing event() contract + gate check | 44 / 43.2% / +9.7% | 43 pass / PASS |
| 2 | 84% | ENTRY_REJECTED event on the tape (audit why a breach didn't fire) | 44 / 43.2% / +9.7% | 43 pass / PASS |
| 3 | 80% | manage() no longer fabricates premium on a missing tick (phantom-stop fix) + FEED_GAP logged (manage-quote, post-exit-band) | 44 / 43.2% / +9.7% | 43 pass / PASS |
| 4 | 82% | Dashboard: live entry-status badge in SCREAM panel (ENTERED / BLOCKED:reason) from _entry_block | 44 / 43.2% / +9.7% | gate PASS (display-only) |
| 5 | 84% | Dashboard: held-level freshness age + 30-min re-anchor countdown in NIFTY calcinfo | 44 / 43.2% / +9.7% | gate PASS (display-only) |
| 6 | 82% | Live-safety: failed Breeze order now raises OrderPlacementError -> enter() emits REJECT (ORDER_PLACEMENT_FAILED) instead of building a phantom Position from str(resp) order_id | 44 / 43.2% / +9.7% | 43 pass / PASS |
| 7 | 84% | Live-safety (exit mirror): _close() guards order_id is None -> REJECT, books NO phantom P&L, keeps position OPEN for retry; new EXIT_REJECTED event on tape | 44 / 43.2% / +9.7% | 43 pass / PASS |
| 8 | 81% | Blotter consistency: journal pnl_pct (closed + live-open) now derived from canonical pnl_realized, not recalculated from rounded fill prices -> % column always agrees with rupee column | 44 / 43.2% / +9.7% | 43 pass / PASS |
| 9 | 77% | Cockpit truth: decision badge now reflects the orchestrator's real fire (FIRE_UP/FIRE_DOWN via _approach_entry, persisted to _last_decision) instead of the dead DecisionMaker HOLD -> badge matches decision_tape | 44 / 43.2% / +9.7% | 43 pass / PASS |
| 10 | 83% | Weight-aware SCREAM: confluence verdict now uses weighted same_pull (>=0.35 cfg) not raw count -> a lone heavyweight reads STRONG; net_pull surfaced in alarm + banner + scream msg. Display-only, no-veto preserved | 44 / 43.2% / +9.7% | 43 pass / PASS |
| 11 | 84% | Binding-tape truth: decision_tape now written AFTER executor.enter() -> a rejected approach records HOLD + real reject reason (INSUFFICIENT_CAPITAL etc.) instead of FIRE/APPROACH_ENTRY; no longer contradicts order_tape. +new integration test | 44 / 43.2% / +9.7% | 44 pass / PASS |
| 12 | 86% | Cockpit observability: all engine events routed through _emit_event -> bounded recent-events strip + levels_stale flag in latest_state; dashboard shows live FEED_GAP/ENTRY_REJECTED/EXIT_REJECTED/anomaly strip + dims U/L when held levels failed to re-anchor | 44 / 43.2% / +9.7% | 44 pass / PASS |
| 13 | 82% | Live crash-safety: connect() now retries w/ backoff (uses the dead reconnect knobs) then raises BreezeConnectionError -> app logs SESSION_ERROR; LiveBreezeSource.rounds() wraps per-instrument build -> FEED_GAP + skip, never kills the session. Replay/synthetic untouched. +new test | 44 / 43.2% / +9.7% | 45 pass / PASS |
| 14 | 84% | Stale-anchor guard: _nifty_band_inputs now ts-stamped; post-exit re-anchor REFUSES to recompute on prior-round inputs (NIFTY fed-gapped same round) -> sets levels_stale + FEED_GAP(STALE_INPUTS) instead of presenting stale levels as fresh. Never fires in the 9 tapes (defensive for live) | 44 / 43.2% / +9.7% | 45 pass / PASS |
| 15 | 83% | Lifecycle test coverage (locked mechanics, zero behavior change): proves an open position at 15:25 close is force-flattened into order_tape (EXIT/END_OF_SESSION + journal CLOSED + SHUTDOWN + flush) and that the stale-inputs guard refuses to anchor. Closes the determinism harness's biggest blind spot | 44 / 43.2% / +9.7% | 47 pass / PASS |

## Final state after 15 iterations
- Council %-to-requirement across iters: 84,84,80,82,84,82,84,81,77,83,84,86,82,84,83 (avg ~82-83%).
- Engine behavior on the 9 recorded tapes UNCHANGED throughout: 44 trades / 43.2% win / +9.7% avg-daily, fully deterministic. Every locked rule (all-in sizing, lot 65, paper-always, no-veto, exits set, 09:20/09:30 + 30-min timing, real-data-only, premium floors 45/30, BIG-MONEY removed) intact.
- All 15 improvements were observability / live-mode safety / audit-truth / test-coverage — NONE touched the strategy or capital rules (as instructed). Tests grew 43 -> 47; all 6 verify gates PASS.
- Honest standing caveat: the strategy edge remains unproven — it effectively trades only the 2 expiry days in this 9-day window; the loop hardened correctness/safety/observability, not the edge (which is locked).
