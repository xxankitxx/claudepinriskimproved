# 11 — Breeze SDK wrapper (vajra/breeze.py) + live-session robustness: findings

Scope: `vajra/breeze.py`, the live path in `vajra/feed.py` (LiveBreezeSource), `vajra/app.py:_connect_breeze`,
`vajra/config.py:breeze_secret`, `vajra/credentials.py` (structure only — no values were read out or quoted),
`logs/websocketLogs.log`, `logs/apiLogs.log`, and the 10-14/15/16 event tapes. I cross-checked every SDK
contract against the real `breeze-connect` source (1.0.64, downloaded read-only into my scratchpad; the
operator's installed version is older — it writes `websocketLogs.log`, 1.0.64 writes `websocketLogs_1305.log`).
Scripts: `/home/claude/vajra2_review/scratch/11/ws_log_analysis.py`, `subs_from_tape.py`.

## 0. What the SDK logs actually reveal (confirmed by running the scripts)

* `websocketLogs.log` (10-16, 11:11:00 -> 15:21:49) contains **941 lines, all of them**
  `Stock <CODE> subscribed successfully`. Nothing else is ever logged: the SDK only logs subscribe acks and
  subscribe exceptions. It logs **no** connect / disconnect / reconnect events (python-socketio is created with
  `logger=False`), so "the ws log shows no reconnect at 15:15" is **not evidence** of anything, either way.
* **"Every stock subscribed twice at startup" is cash + futures, not a bug.** 11:11:00 has exactly
  49 = 24 instruments x (subscribe_spot + subscribe_future) + 1 INDVIX. The SDK prints the identical message for
  NSE cash and NFO futures (`STOCK_SUBSCRIBE_MESSAGE.format(stock_code)`), hence the visual duplicate. The real
  waste is different: 21 stock futures + 2 sister futures are subscribed and **never used** (`_basis_mode_for`
  returns IMPLIED for every non-NIFTY instrument, orchestrator.py:216-218); only NIFTY-in-monthly-week reads
  `future_traded`. 23 of 49 base subscriptions are dead weight (they are recorded to tick_tape only).
* 11:11:02: exactly **624 = 24 x 13 strikes x 2 rights** — the first round's full ATM+/-6 chain.
* After that the set **only grows**: 892 option subscribes by 15:21 vs 624 needed (+43%). Reconstructing the
  window from tick_tape `atm_strike` reproduces the storm second-for-second: 15:20:09 -> 14, 15:20:10 -> 8,
  15:20:19 -> 6/20, 15:20:24 -> 6 (the ws log shows 60 subscribes 15:20:09–15:21:11). Cause: the 15:20 garbage
  cash tick (+185/-134 pts) moved ATM 2–5 steps on NIFTY/BANKNIFTY/FINNIFTY at once. Mid-session
  `STRIKE_STEP_CORRECTED` events (7 on 10-16) add a whole new 26-strike window per stock on top of the old one.
* `apiLogs.log`: 21 `get_names` OK (local ScripMaster lookup, no network); **29 `get_option_chain_quotes`
  calls, 9 of them HTTP 503** ("Expecting value" = non-JSON body) when fired 8-wide in parallel
  (`feed.py:255-262`). 7/21 (33%) failed at startup, 2/7 failed again on the immediate round-1 retry
  (`_step_retry[inst] = 0` makes them due at once), BSE resolved at 11:12:31. That is a rate-limit signature.
* `get_names` for HDFCBANK matched two ScripMaster rows (equity + "HDFC BANK WARRANT AUG 23"); the SDK's
  `.to_string().split()[1]` hack returned the right `isec_stock_code` **by row order only**
  (`'exchange_stock_code': 'HDFCBANK 1465 HDFCBANK'`, mangled). Fragile.
* The `company name` returned by `get_names` for all 21 stocks is **byte-identical to the ws `stock_name`**
  (the SDK fills `stock_name` from the same ScripMaster column). The hard-coded `WS_NAME_TO_INSTRUMENT`
  (breeze.py:36-63) can therefore be built at runtime — see proposal P3.
* 10-16 event tape: three `SESSION_ERROR breeze_connect_failed` at 10:24:40, 10:28:19, 10:46:11, each
  `Exception('Failed to connect to live stream')`. That string is the SDK's
  `LIVESTREAM_SOCKET_CONNECTION_DISCONNECTED`, raised from `SocketEventBreeze.connect()` **after
  `generate_session()` had already succeeded** (a stale apisession fails earlier, in `api_util()`, with
  "Could not authenticate credentials. Please check session key." / "Session key is expired."). So on 10-16
  the REST login was accepted and the socket.io handshake (hard-coded `wait_timeout=3`) failed five times per
  run, three runs, 46 minutes of operator retries. Whether the stream server rejected the token or simply timed
  out is **unknowable from VAJRA's record** because the wrapper discards the chained root cause (F2).

## 1. Correctness defects (most severe first)

### [CRITICAL] No tick-age / staleness in the wrapper: a frozen feed is indistinguishable from a live one (breeze.py:247-310, feed.py:344-356)  certainty: confirmed-by-reading (+ lead's A2 evidence)
- What happens: `latest[k]` is overwritten only when a new tick arrives and never expires. `_build_snapshot`
  reads `spot_tick["ltp"]` with no notion of when it was received. During the 15:15–15:20 cash stall on all
  three days the engine consumed a 290-s-old spot as current and traded on the garbage resume tick.
  The SDK quote tick carries `ltt` (last trade time) on every quote (`parse_data` data[19]/data[21]) and the
  wrapper ignores it.
- Fix: in `_normalize_tick` add `"recv_ms": time.time_ns()//1_000_000` and parse `ltt`
  (`'%a %b %d %H:%M:%S %Y'` -> epoch, it is a local-time string); in `_build_snapshot` return None (FEED_GAP
  `STALE_SPOT`) when `ts_ms - recv_ms > feed.max_spot_age_secs` (**3 s** for indices during 09:15–15:30;
  10 s for stocks). Also add `spot_jump_pct` guard: reject a cash tick whose |Δ| vs the previous tick exceeds
  0.5% of spot **and** disagrees with the ATM implied future by > 0.3% (NIFTY basis beyond +/-75 pts is
  impossible) — emit `ANOMALY SPOT_IMPLAUSIBLE`, keep the prior value, do not enter.

### [HIGH] connect() retries a non-retryable auth failure, re-downloads the ScripMaster 5x, and loses the root cause (breeze.py:129-153, app.py:171-192)  certainty: confirmed-by-reading
- What happens: every attempt builds a new `BreezeConnect` and calls `generate_session`, which hits
  CUST_DETAILS **and** downloads the full NFO/NSE ScripMaster CSV (`get_stock_script_list`). A stale
  SESSION_KEY is deterministic — 5 attempts + 2/4/6/8 s backoff just waste ~40 s and the operator sees
  `Exception('Unexpected error: Could not authenticate ... session key.')` buried in a generic
  `BreezeConnectionError`. For the 10-16 case the ws failure's real cause (socketio `ConnectionError`,
  timeout vs rejected) is in `e.__context__` (the SDK re-raises inside `except`) and is dropped.
  `SESSION_ERROR` is stamped `ts_ms=start_ms` (app.py:190) so the tape cannot even show how long it took.
- Fix: split into `login()` (once; classify `SESSIONKEY_INCORRECT/EXPIRED/APPKEY_INCORRECT` -> raise
  `BreezeAuthError` immediately, no retry, message: "Breeze session token rejected — refresh it: `python -m
  vajra.app --token <apisession>`") and `open_stream()` (retry up to N with backoff, **without** re-login;
  include `repr(last_err.__context__)` in the error). Stamp SESSION_ERROR with `now_ms()`.

### [HIGH] Credential precedence is inverted: env/CLI can never override credentials.py (config.py:128-146)  certainty: confirmed-by-reading
- `breeze_secret` returns the credentials.py constant first; `BREEZE_SESSION_TOKEN` is consulted only if the
  constant is blank. Since SESSION_KEY is populated, the documented env fallback is dead code and the operator
  must edit a .py file inside the package every morning (RUN_LIVE.md §1, "#1 failure point").
- Fix: precedence `--token` CLI > `BREEZE_SESSION_TOKEN` env > token file `~/.vajra/breeze_session.json`
  (`{"token": ..., "date": "2026-10-16"}`, written by a new `python -m vajra.token <apisession>` command,
  refused when `date != today` -> fail fast before any network call) > credentials.py. Keep
  `vajra/credentials.py` in `.gitignore` (it is) and add `~/.vajra/` is outside the repo by construction;
  also add a `credentials.example.py` so the real file is never the template.

### [HIGH] Subscription set only grows; no unsubscribe, no cap (feed.py:334-342, breeze.py — no `unsubscribe_*`)  certainty: confirmed-by-running (scratch/11/subs_from_tape.py)
- +43% subscriptions by 15:21 on a 303-pt day; a 475-pt trend day (10-15) would add ~20 extra NIFTY strikes
  alone, each step correction adds a parallel 26-strike window, and the garbage 15:20 tick triggered 60
  subscribes in 60 s. More tokens = more `_on_ticks` lock traffic for strikes the engine never reads, and
  Breeze enforces a per-connection token budget (undocumented; exceeding it silently drops streams).
- Fix: `BreezeWrapper.unsubscribe_option(...)` (SDK has `unsubscribe_feeds` with the identical signature) and
  in `_ensure_option_subscriptions` keep `wanted = ATM+/-(n+2)` with hysteresis: unsubscribe a strike only
  when it is > n+2 steps from ATM for **3 consecutive rounds** (so the 4/s jitter never churns), and purge its
  `latest` entry. Rate-limit new subscribes to <= 20 per round; log `SUBSCRIPTION_CHURN` when a round wants > 20.

### [HIGH] SDK subscribe failures are invisible: subscribe_feeds never raises (breeze.py:218-244, feed.py:273-278)  certainty: confirmed-by-reading (SDK source)
- `subscribe_feeds` catches every exception and **returns a string** `"Exception while subscribing to feeds ..."`
  (also when `authentication == False` after a reconnect-auth failure, and when the strike/expiry does not exist
  — `subscribe_exception()` *returns* an Exception object, then `str + False` raises TypeError, caught).
  The wrapper discards the return value, so `FEED_GAP subscription` (feed.py:277) can never fire for a real
  broker-side failure; the instrument just never ticks (exactly the 2026-07-01 "ACKs but never streams" story).
- Fix: `r = self._breeze.subscribe_feeds(...)`; `if not isinstance(r, dict) or "subscribed" not in
  str(r.get("message","")): raise BreezeSubscribeError(r)`. Same for `unsubscribe_feeds`.

### [HIGH] place_option_order: expiry format and price are wrong for the REST order API (breeze.py:313-332)  certainty: confirmed-by-reading (SDK README; not exercised live — LIVE_ORDERS=false)
- `expiry_date=ws_expiry(expiry_iso)` sends `19-Oct-2026`. The REST `place_order` documents
  `expiry_date="2025-02-27T06:00:00.000Z"` (ISO-8601 Z) in every example; `DD-Mon-YYYY` is the **websocket**
  format (`get_option_chain_quotes` happened to accept it on 10-16 — apiLogs shows `'expiry_date':
  '27-Oct-2026'` echoed back — but there is zero evidence `place_order` does). `price=""` for market orders
  trips the SDK's first `if ... price == ""` branch, which **skips the product/action/order_type/validity
  validation** and posts the body anyway; the documented value is a string number (`"0"`). `product`,
  `action.lower()` ("buy"/"sell"), `order_type` "market", `validity` "day", `right` "call"/"put",
  `strike_price=str(int(strike))`, `quantity=str(int(qty))` are all correct per `config.PRODUCT_TYPES /
  ACTION_TYPES / ORDER_TYPES / VALIDITY_TYPES / RIGHT_TYPES`. The `Success` check is right (a rejection
  returns `{"Success": "", "Status": 500, "Error": ...}` -> `resp["Success"]["order_id"]` raises TypeError,
  caught -> OrderPlacementError).
- Fix: `rest_expiry(iso) -> f"{iso}T06:00:00.000Z"`; `price="0"` for market; log `resp["Error"]` in the
  OrderPlacementError. Must be verified with ONE 1-lot `preview_order` call before LIVE_ORDERS is ever flipped.

### [MEDIUM] Carry-forward of falsy fields turns 0 into "stale previous" (breeze.py:257-260)  certainty: confirmed-by-reading
- `if not norm.get(fld) and prev.get(fld): norm[fld] = prev[fld]` for ltp/vwap/volume/oi/bid/ask. A genuine
  `bid=0` (no bids, deep OTM) or `oi=0` is replaced by the old non-zero value; `ltp` 0/None keeps the last
  price forever (A4 bullet). Combined with F1 this is what makes a dead strike look alive.
- Fix: carry forward **only** `None` (field absent), never `0`; keep `recv_ms` per field group; a tick whose
  `ltp` is None/0 should not refresh `recv_ms`.

### [MEDIUM] `last or ltp or close` and `ttq ... or ltq` fallbacks mix semantics (breeze.py:268, 300)  certainty: confirmed-by-reading (SDK field list)
- Real quote-tick fields are `last, bPrice, sPrice, avgPrice, ttq, OI, close` (NFO adds OI/CHNGOI). An option
  with no trade today streams `last=0` -> falls through to `close` = **yesterday's** close, presented as LTP.
  `volume` falls through to `ltq` (last trade quantity, not cumulative) when `ttq` is 0/absent, feeding
  `VWAPTracker` garbage deltas. `ltp`, `avgPrice`, `average_price`, `ltq_vwap`, `open_interest`,
  `best_bid_price` are not ws fields at all (they are REST `get_quotes` names) — harmless but misleading.
- Fix: `ltp = _f(t.get("last"))` only, `volume = _i(t.get("ttq"))` only; expose `prev_close` separately.

### [MEDIUM] `int(float(strike))` truncates half-strikes; `_key` collides across expiries (breeze.py:215-216, 296; feed.py:337)  certainty: confirmed-by-reading
- BANKBARODA resolved step 2.5 (event tape 11:11:00) and ITC 2.5: `atm=int(round_to(spot,2.5))` and
  `int(atm+k*2.5)` request strikes like `227` that do not exist -> SDK silently fails (F5) -> no ticks ->
  `MISSING_ATM_QUOTE` until the streamed-grid correction (BANKBARODA at 11:15:56, ITC at **15:21:10** — four
  hours of a dead instrument). `_key` has no expiry: a weekly and a monthly NIFTY 25000 CE would share a key
  (not triggered today because only one expiry per instrument is subscribed; a real hazard the moment V2 adds
  the next weekly for roll-over).
- Fix: keep strikes as `float` end-to-end (format `"{:g}"` for the SDK, i.e. `"227.5"`); add `expiry` to the key.

### [MEDIUM] Parallel option-chain burst triggers 503s; failures requeue at 0 then 90 s (feed.py:254-272, 299-316)  certainty: confirmed-by-running (apiLogs: 9/29 = 503)
- 8 concurrent REST calls -> 33% 503. Fix: `max_workers=2`, 150 ms spacing, treat a 503/JSON error as
  retryable with 2 s -> 4 s -> 8 s backoff (not an immediate round-1 retry, not 90 s).

### [LOW] `option_step` uses the chain centre as ATM (breeze.py:204)  certainty: confirmed-by-reading
- `ks[len(ks)//2]` is the median listed strike, which for a stock whose chain is skewed (new strikes added on
  one side after a move) is far from spot; near-centre spacing can be the coarse far-OTM grid (BAJFINANCE
  10 vs real 20, SBILIFE 20 vs 40, AUBANK 10 vs 40 — 5 corrections on 10-16). Use the response's `spot_price`
  field (present in every row, e.g. `'spot_price': '1206.6'`) to centre the window.

### [LOW] `snapshot_raw()` copies ~700 dicts under the lock 4x/sec (breeze.py:308-310)  certainty: plausible
- Shallow `dict(self.latest)` of ~700 entries is ~50 µs; fine. The real contention is `_on_ticks` holding the
  lock while normalising each tick (string ops, float parsing). Normalise **outside** the lock, then swap in.
  Not a defect today; becomes one if V2 subscribes depth.

### [LOW] `disconnect()` never un-registers `on_ticks`, `connected` is never set False on a drop (breeze.py:155-157, 334-340)
- `connected` is a connect-time latch, not a liveness signal. See P2.

## 2. Improvements / design proposals (ranked)

**P1 — Session/token workflow (operator asks: no daily .py edit).**
```
python -m vajra.token 12345678        # writes ~/.vajra/breeze_session.json {token, date, user masked}
python -m vajra.app --mode live       # reads CLI --token > $BREEZE_SESSION_TOKEN > token file > credentials.py
python -m vajra.app --check-session   # pre-flight: generate_session only, prints OK/REJECTED + which source
```
Pre-flight runs before the tape is opened and before the dashboard starts; on `BreezeAuthError` it exits 2 with a
one-line message containing the login URL (api_key URL-encoded, never the secret). `.gitignore` already covers
`vajra/credentials.py`, `.env`, `*.env`; add `*.session.json` and `.vajra/`. API_KEY/API_SECRET stay in
credentials.py or `BREEZE_API_KEY/SECRET` env (CI); the token is the only daily-rotating value.

**P2 — Liveness + reconnect-on-silence.** Wrapper keeps `last_tick_ms` (any instrument) and
`last_cash_tick_ms`. Feed loop, during 09:15–15:30 on a trading day: no tick for **5 s** -> `FEED_GAP
feed_silent`; **15 s** -> `ws_disconnect()` + `ws_connect()` (same session, no re-login) +
re-subscribe the *current* wanted set (not the historical one); still silent after 2 cycles -> re-login once
(token may have been invalidated by a second login elsewhere) -> `SESSION_ERROR`. Cash-only silence
(options ticking, cash frozen > 10 s, the 15:15 pattern) -> `FEED_GAP cash_stall`, mark spot stale (F1), block
entries, do **not** reconnect (it is upstream). Note the SDK already re-joins `tokenlist` on socketio
reconnect — V2 should pass `reconnection=False`-equivalent by owning the reconnect itself, otherwise the SDK
re-subscribes the bloated historical token list behind our back.

**P3 — Resolve ws names at runtime.** `resolve_codes()` already receives `company name` from `get_names`;
store `self._ws_name_to_inst[name.upper()] = sym` and use it ahead of the static map. Verify
`isec_stock_code` against `breeze.isec_override` and emit `ANOMALY CODE_MISMATCH` on disagreement (the
HDFCBANK warrant collision). Drop stock/sister **futures** subscriptions unless `basis_mode == ACTUAL`.

**P4 — Subscription manager** (F4): wanted-set reconciliation each round with hysteresis, unsubscribe,
per-round cap, `SUBSCRIPTION_CHURN` event, and a tape-durable `SUBSCRIPTIONS` summary every 5 min
(count, per-instrument window) so the next review can see the storm without the SDK log.

**P5 — Tick normalisation contract** (F6/F7/F8): one `Tick` dataclass with `recv_ms`, `ltt_ms`, float
strike, expiry in the key, no 0->stale carry-forward, VIX x100 fix unchanged but logged once per session
(`VIX_SCALE_APPLIED raw=0.12 -> 12.0`) and the threshold tightened to `0 < raw < 2.0` (India VIX has never
printed below 8; 5.0 leaves a window where a real 4.x would be multiplied — not realistic, but cheap).

**P6 — Startup gating.** Do not start the first round until the first NIFTY cash tick arrives (<= 10 s), so the
24 `MISSING_ATM_QUOTE` + `all_instruments_gapped` + `round_missing_nifty` events at 11:11:00-02 stop polluting
every tape at every restart.

## 3. Recommendations specific to the two operator asks
A (non-expiry levels): nothing in the wrapper decides levels, but two wrapper-level facts matter for any
proposal measured on these tapes: (i) all 15:20 entries sit on stale/garbage spot — any V2 scoring must
exclude rounds flagged `STALE_SPOT`/`SPOT_IMPLAUSIBLE` (F1) or the 10-16 "+8,459.75" survives as a false
positive; (ii) with a 15-s pre-market liveness gate (P6) the 09:20 first calc gets a real chain instead of
the MISSING_ATM_QUOTE round. B (sounds): none from this module; I would add exactly one new sound —
feed-dead (P2 15-s reconnect) — and it should be the only sound allowed to repeat.

## 4. Things that are fine and should NOT be changed
* `ws_expiry()` explicit-English-month format for **websocket + option-chain** calls (confirmed accepted by the
  chain endpoint on 10-16). Keep; add a separate `rest_expiry()` for orders only.
* `INDEX_CODE`, `INDIA_VIX_CODE`, `RIGHT_TO_BREEZE`, `get_market_depth=False` (depth ticks have no `last`).
* `get_names` at runtime with the override fallback (all 21 resolved; 10 of 21 also covered by the override).
* `OrderPlacementError` never returning a phantom order_id; `_f/_i` tolerant parsing.
* Lazy `from breeze_connect import` (the SDK downloads the SecurityMaster zip at **import** time — keep it
  out of tests/replay).

## 5. Tests I would add (fake SDK; no network)
`tests/fake_breeze_sdk.py`: a `FakeBreezeConnect` with `generate_session` (raises the SDK auth strings on
a configurable token), `ws_connect` (raises `Exception('Failed to connect to live stream')` with a chained
`ConnectionError`), `subscribe_feeds`/`unsubscribe_feeds` returning the SDK dict **or the SDK error string**,
`get_option_chain_quotes` returning `{"Success": rows}` / raising after a 503, and a `push(tick_dict)` helper
that calls `on_ticks` with real SDK-shaped dicts (fields from `parse_data`: `symbol, last, bPrice, sPrice,
avgPrice, ttq, OI, close, ltt, stock_name, product_type, strike_price, right`).
1. stale token -> `BreezeAuthError` after **1** attempt, message names `--token`; ws failure -> N attempts,
   `__context__` text present, SESSION_ERROR ts > start ts.
2. subscribe error string -> `BreezeSubscribeError` -> `FEED_GAP subscription` taped.
3. ATM moves +3 steps -> exactly 6 new subscribes; moves back -> 0; a strike > n+2 away for 3 rounds ->
   unsubscribed and purged from `latest`; subscribe count per round <= cap.
4. tick with `last=0, close=11.85` -> ltp None (not 11.85); `bid=0` not carried forward; `recv_ms` set;
   `_build_snapshot` returns None when `now - recv_ms > 3 s` and emits STALE_SPOT once.
5. half-strike `"227.5"` round-trips key/subscribe; weekly vs monthly same strike -> distinct keys.
6. silence 15 s -> `ws_disconnect`+`ws_connect` called once, resubscribe == current wanted set; options
   ticking + cash frozen 10 s -> `cash_stall` event, no reconnect, entry blocked.
7. `place_option_order` body: `expiry_date` ISO-Z, `price="0"` for market, `product="options"`,
   `action in {buy,sell}`; `{"Success": "", "Error": "x"}` -> OrderPlacementError containing "x".
8. precedence: CLI > env > token file (date-checked) > credentials.py, with credentials.py populated.
