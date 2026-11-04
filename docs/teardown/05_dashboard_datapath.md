# 05 — Dashboard live-data path teardown

Verdict: **Ready for Monday, with caveats, and clearly better than 1.0 on the hot path.** The NIFTY hero, banner, position and capital now track the engine (median about 0–35 ms behind, against about 1 s for 1.0). The heavy tables are no better than 1.0: chain, journal rows, universe, burst board and events run 0.6–2.7 s behind by design. There are two robustness holes, both reproduced: a tab can go permanently dead when `/api/stream` returns a non-200, and the 7th dashboard tab in one browser will not load. The new lag pill shows the wrong number and stays green during an outage.

Score (your area): 1.0 = 3/10, 2.0 = 6/10

> **Measurement caveat (read first).** The box was badly overloaded the whole time: load average 18–39 on 2 CPUs, 20 reviewers, and the cgroup OOM-killer was killing processes (`dmesg`: "Memory cgroup out of memory: Killed process … python3"). Two early Chromium runs died with "Target crashed". I ran the harness at `nice -5` and paired 1.0 and 2.0 under the same conditions. Even so, the engine round times (p50 60–125 ms, against about 10 ms on an idle box) and the occasional 7–107 s whole-process stalls are the machine, not the code. So read the absolute numbers as a stress test. The 1.0-vs-2.0 *ratios* are the signal.

## Method (scratch: /home/claude/teardown/scratch/05/)
* `srv.py`: the real `Orchestrator` + real `TapeWriter` + the version's own `_start_dashboard`. It is fed **real 15 Oct tick_tape rounds from 09:41:00 at live pace** (original ts spacing, about 250 ms, 21–24 instruments a round). It logs `{round_ts, publish epoch-ms, round cost}` per round.
* `drv.py`: headless Chromium via Playwright, with an init script that changes nothing:
  * 2.0: it wraps `_onState` (message arrival) and `render` (paint; a `render` inside rAF counts as painted at render end, otherwise at the next rAF).
  * 1.0: it wraps `Response.json` (poll arrival) and uses the next rAF after it (paint).
  * It pulls CDP `Performance.getMetrics` every 10 s.
* `ana.py` reports:
  * publish→message and publish→paint for each round;
  * % of rounds painted;
  * **"behind engine"**, sampled every 50 ms: how long the newest round *not yet on screen* has been published (0 = screen shows the newest round);
  * the same for the heavy tables (2.0 only; in 1.0 every paint is a full paint).

## Results (real tape, live pace, paired runs)

| scenario | metric | **1.0** (poll 250 ms) | **2.0** (SSE) |
|---|---|---|---|
| 1 tab, 90 s (`*_n`) | rounds painted | 47.8 % | 92.8 % |
| | publish→paint p50 / p95 / max | 1064 / 1648 / 2053 ms | 145 / 347 / 737 ms |
| | behind engine p50 / p95 / max | 1019 / 1707 / 2292 ms | **33 / 322 / 693 ms** |
| | heavy tables behind p50 / p95 / max | (same as above) | 964 / 1877 / 2370 ms |
| 1 tab, 15 min long (`*_long`) | rounds painted | 67.3 % | 94.2 % |
| | publish→paint p50 / p95 / max | 473 / 1012 / 2324 ms | 132 / 323 / 1199 ms |
| | behind engine p50 / p95 / max | 356 / 970 / 2289 ms | **6 / 260 / 1189 ms** |
| | heavy tables behind p50 / p95 / max | (same) | 638 / 1623 / 2700 ms |
| | JS heap min–max; DOM nodes | 1.1–2.1 MB (first third 1.53, last third 1.59); 6.5k–17k sawtooth | 1.1–2.9 MB (1.50 → 1.92); 2.8k–9.6k sawtooth |
| | server max RSS first → last | 239 → 285 MB (flat after 4 min) | 239 → 291 MB (flat) |
| 3 tabs, 1 with rAF suspended, tab-0 reload at 45 s (`*_m`) | behind engine p50 / p95, visible tabs | 2923–3013 / 5730–7544 ms | 634–653 / 1663–1921 ms |
| | rAF-suspended tab (2.0's 400 ms fallback path) | n/a (1.0 doesn't use rAF) | p50 1007, p95 3918 ms |
| | reload → first message / first paint | 4138 / 4336 ms | 2421 / 2662 ms |
| server killed, down 12 s, restarted (`*_rs`) | first paint after the new engine's first publish | 56 ms | 300 ms (via the poll fallback; SSE resumed at +22.7 s) |
| | errors in page console | 0 | 0 |
| pre-open (engine up, round_ts null) | banner after 15 s | PRE-OPEN — engine live … | PRE-OPEN — engine live … (no false FROZEN) |

Memory: neither version leaks over the 11–15 min runs. Heap and nodes are sawtooth with no upward trend, and server RSS plateaus. Payloads (`sizes.py`, 1,204 real rounds): hot is 2.4 KB, full is 18.7 KB. Serialising (`_clean` + dumps) takes a median of 0.1 ms hot and 0.56 ms full. None of the 1,204 rounds had NaN, so 1.0's HTTP 500 path did not trigger in this window. The SSE side adds no meaningful load to the engine thread.

## Findings (most severe first)

### [HIGH] A tab dies permanently if `/api/stream` ever answers non-200 (dashboard.py:1133-1149) — reproduced
- **How it fails:**
  * `onerror` counts failures and falls back to polling only after 5 of them.
  * A non-200 or non-`text/event-stream` answer puts EventSource in **CLOSED**. It fires `onerror` **once** and never retries.
  * The 30 s poll window then clears the poll timer (`clearInterval(_pollTimer)`) and calls `_startSSE()`. If that new EventSource gets a 404, polling never restarts.
- **Reproduced** (`fake.py` + `probe.py`): a 2.0 tab was open, and the 2.0 server was replaced on the same port by the **1.0 app**, which has no `/api/stream` (the obvious Monday rollback).
  * The tab polled fine for 30 s (`poll 0 ms`, clock advancing).
  * Then it switched back to SSE: `readyState=2`, `_sseFails=1`, `_pollTimer=false`.
  * It showed **"DATA FROZEN — no response from engine"** for good, while `/api/state` was healthy. Only a manual reload recovers it.
  * The same happens on any non-200 from a proxy, an AV web shield, or a future route error.
- **Fix:** in `onerror`, `if(_es.readyState===EventSource.CLOSED){ _es=null; _startPoll(); return; }`. Also, don't stop polling until the first `hot`/`full` message has actually arrived over SSE.

### [MEDIUM] SSE uses one of Chrome's 6 HTTP/1.1 connections per host, so the 7th tab never loads, and 6 tabs starve every other request (dashboard.py:68-96, 1135) — reproduced
- **Evidence:** `tabs7.py`, 7 pages in one browser profile against the 2.0 app. Tabs 0–5 were live (`live 11–126 ms`). **Tab 6: `goto` timed out, page never loaded.**
- With exactly 6 tabs open, any extra request queues behind the 6 open SSE streams: the sound mp3 fetches (`_audioCtx()` fetches only on unlock), the poll fallback, or a reload. 1.0 has no persistent connections. It degrades badly with many tabs (in this test the 7th tab had not painted after 12 s), but it is never hard-blocked.
- **Fix:** any one of these works:
  * a `BroadcastChannel`/`SharedWorker` so only one EventSource exists per browser;
  * serve over HTTP/2 (uvicorn+h2/hypercorn), which multiplexes;
  * at minimum, document "max 5 tabs" and add a `<link rel=preload>` for the sounds before SSE opens.

### [MEDIUM] Heavy tables are 0.6–2.7 s behind — no better than 1.0, and worse than 1.0 would be on an idle PC (dashboard.py:83-87, 1125) — reproduced
- **Cause:** `full` arrives at most once a second (server timer). Separately, heavy repaint happens at most once a second (client timer, `_lastHeavy`). The two timers are not phase-locked, so a full that arrives just after a heavy paint waits up to another second.
- **Measured** heavy-table "behind" p50 / p95 / max:
  * 964 / 1877 / 2370 ms (90 s run);
  * 638 / 1623 / 2700 ms (15 min run).
- **Affected:** option-chain LTPs, the journal's **live row exit price / P&L**, the universe table, burst board, breach list, events strip, `TICK STALE` (driven by `instruments.NIFTY.stale`, which only arrives in `full`), and `feedcrit` (uses `events`).
- **Incoherent screen:** for up to about 2 s the screen contradicts itself. After an exit, POSITION says "flat · last exit <previous trade>" because `journal` is stale. After an entry, the IN-TRADE banner omits "held". The journal live row P&L disagrees with the hot POSITION card.
- **Fix:**
  * Do the heavy paint on the frame after a `full` arrives: `heavy = _gotFull; _gotFull=false`.
  * Move the small `nifty_chain` (13 rows), the open journal row and `instruments.NIFTY.stale` into `hot`.

### [MEDIUM] The lag pill measures the wrong thing and stays green during an outage (dashboard.py:85, 1114, 1128-1131, 1138) — reproduced
- **Wrong measurement:** `server_ms` is stamped when the SSE generator *sends*, not when the engine *publishes*. The pill therefore hides:
  * the generator's 20 ms poll plus event-loop delay (measured publish→send p50 32–38 ms, **max 698 ms**);
  * the rAF queue.
- **Overwritten by beats:** each 2 s `beat` sets `_lagMs` to the network delay alone, even if the engine has stalled.
- **Stale in two cases:**
  * The pill only updates inside `_paint`. When the server dies, it keeps its last value, so the probe showed **"live 0 ms · paint 1 ms" (class `feed-ok`, green) next to "DATA FROZEN"**.
  * In poll fallback `/api/state` carries no `server_ms`, so the pill shows a stale "poll 0 ms".
- **Why it matters:** the CHANGELOG tells the operator to trust this pill as delivery health.
- **Fix:**
  * Put `publish_ms` in `latest_state` (set in `_update_view`).
  * Compute lag = paint time − publish_ms.
  * Update the pill from `applyWatchdog` (grey "—" / red when frozen).
  * In `/api/state`, add `server_ms`.

### [LOW] Shipped benchmark doesn't run; the CHANGELOG latency numbers are not reproducible from the repo (tools/latency_bench.py:26) — reproduced
- `python3 -B -m py_compile tools/latency_bench.py` gives `SyntaxError: invalid syntax` (a broken nested-quote `ParquetFile("str(Path(...) / "tapes" ...")")`).
- Even when fixed, it times a Python client reading the socket, not a browser paint.
- Under load my measurements (publish→paint p50 132–145 ms, 92–94 % of rounds painted) do not match "8/18/20 ms, painted every pushed round". The direction of the claim (much better than 1.0) does hold.
- **Fix:** repair the path, and ship the Playwright paint harness.

### [LOW] SSE generator coalesces bursts; render errors are swallowed silently (dashboard.py:75-94, 1127, 1136-1137) — read in code / partly reproduced
- **Coalescing:** the generator sends only the latest `round_ts` per 20 ms tick, and the client paints only the latest message per frame. After an engine hiccup, intermediate rounds are never shown. 6–8 % of rounds were unpainted under load. This is fine for display, and sounds are carried as a cumulative list.
- **Silent errors:** `JSON.parse` errors are swallowed (`catch(e){}`), and a `render` exception only goes to `console.error`. The lag pill still turns green, so a throwing field would freeze the lower half of the page silently.
- **Data checked:** none occurred in about 6,500 real rounds (0 page errors in every run), and every field render touches is always present (`_update_view` builds all keys every round, orchestrator.py:1339). So there is no stale-key bug in the `Object.assign` merge today.
- **Fix:** count render/parse errors and show them in the pill.

### [LOW] Poll fallback re-tries SSE on a fixed 30 s and stays at 2 Hz in between (dashboard.py:1143-1149) — reproduced
- **Evidence:** after the restart, the tab ran on 500 ms full-state polling from +8.5 s to +22.7 s before SSE came back.
- **Fix:** retry SSE as soon as one `/api/state` succeeds.

## What is fine (checked)
* **Thread safety:** `latest_state` is replaced atomically with a fresh dict every round (orchestrator.py:1339, 544). The serialiser only ever reads a complete snapshot, so there is no "dict changed size" risk. Nested lists are copies.
* **NaN/inf:** `_clean` maps them to `null`, so there is no 500 (1.0 returned HTTP 500 → false FROZEN). `f(null)` renders "—".
* **Watchdog and reconnect:**
  * The watchdog still judges by `round_ts` change, so an evening replay is not masked as closed.
  * Pre-open does not false-alarm (reproduced).
  * Server death shows FROZEN within about 3 s (reproduced).
  * After a server restart, the client reprimes `_sndSeq`, so sounds are not replayed.
* **Hidden tabs:** the 400 ms `setTimeout` fallback keeps painting when rAF is suspended (p50 about 1 s behind, which matches Chrome's 1 s timer clamp in background tabs).
* **Memory:** no growth in the JS heap, DOM nodes, listeners or server RSS over 15 min. `_spotBuf` is capped at 5 min.

## What 2.0 does better than 1.0 (evidence)
* **Hot data is about 30× closer to live.** Behind-engine median was 33 ms vs 1019 ms (90 s run) and 6 ms vs 356 ms (15 min run). p95 was 322 vs 1707 ms and 260 vs 970 ms. Rounds painted: 93–94 % vs 48–67 %.
* **Degrades far better with tabs:** with 3 tabs, behind-engine p95 was about 1.7–1.9 s vs 5.7–7.5 s.
* **Reload:** first paint after a reload took 2.7 s vs 4.3 s.
* **Payload:** 2.4 KB per round vs 18.7 KB.
* **Polling:** the 1.0 loop had no in-flight guard (`setInterval(tick,250)`, 1.0 dashboard.py:995), so requests piled up under load. 2.0's fallback is guarded.
* **NaN no longer returns 500.**

## What 2.0 does worse than 1.0 (evidence)
* **Heavy tables:** chain, journal live row, universe, burst board and events are throttled to 0.6–2.7 s behind. On a healthy PC 1.0 would refresh them every 250 ms.
* **Tab limit:** a persistent connection per tab means the 7th tab is hard-blocked (reproduced). 1.0 had no such limit.
* **New ways to go permanently dead:** EventSource CLOSED → no fallback (reproduced). 1.0's plain polling self-heals as soon as `/api/state` answers.
* **Misleading health indicator:** the new lag pill is green while the page is FROZEN.

## Tests / checks you ran
* `run_one.sh 2|1 n 90`: single tab, real 15 Oct 09:41 rounds at live pace, nice -5. Table above.
* `run_one.sh 2|1 long 900`: 15-min run with heap and node sampling (68–90 samples).
* `run_one.sh 2|1 m 90 --tabs 3 --norafs 2 --reload-at 45`: multi-tab, rAF suspended in one tab, reload.
* `run_restart.sh`: kill the server, 12 s down, restart at 09:50, browser kept open.
* Pre-open (engine up, no rounds): both versions show PRE-OPEN, no false FROZEN.
* `fake.py` + `probe.py`: 2.0 tab, server swapped for the 1.0 app → permanent FROZEN with the pill green.
* `tabs7.py`: 7 tabs in one profile → tab 6 never loads on 2.0.
* `sizes.py`: hot/full payload size and serialise cost over 1,204 real rounds; NaN count 0.
* `py_compile tools/latency_bench.py` → SyntaxError.
* Discarded: early runs at load 28–39 without nice, where both renderers crashed or were OOM-killed (out/*_base*, *_b*). They are directionally the same.
