"""VAJRA 2.0 tool — end-to-end delivery benchmark: engine at live pace (4 rounds/s) on REAL 10-15 data with the real
TapeWriter, dashboard server in-process, and a client measuring per-round delivery.

  python tools/latency_bench.py . sse 60     (needs tapes/2026-10-15/tick_tape.parquet)
"""
import sys, time, json, threading, tempfile, statistics, http.client
from pathlib import Path
from datetime import date

ROOT = Path(sys.argv[1]); MODE = sys.argv[2]; SECS = float(sys.argv[3]) if len(sys.argv) > 3 else 120
sys.path.insert(0, str(ROOT))
import pyarrow.parquet as pq
from vajra.config import load_config
from vajra.app import build_calendar, _start_dashboard
from vajra.orchestrator import Orchestrator
from vajra.tape import TapeWriter
from vajra.feed import _snapshot_from_row

PORT = 8811 if MODE == "poll" else 8812
cfg = load_config(str(ROOT / "vajra_config.yaml"))
cfg.raw["server"]["port"] = PORT
universe = set(cfg.universe)

# ---- load ~SECS*4 real rounds starting 09:50 on 10-15 (all instruments in this build's universe)
need = int(SECS * 4) + 10
rounds, cur, bucket = [], None, {}
_tape = sys.argv[1] if len(sys.argv) > 1 else str(Path(__file__).resolve().parent.parent / "tapes" / "2026-10-15" / "tick_tape.parquet")
pf = pq.ParquetFile(_tape)
from datetime import datetime as _dt, timezone as _tz, timedelta as _td
start_ms = int(_dt(2026, 10, 15, 9, 50, tzinfo=_tz(_td(hours=5, minutes=30))).timestamp() * 1000)
for batch in pf.iter_batches(batch_size=20000):
    ts_col = batch.column("ts").to_pylist()
    if ts_col[-1] < start_ms:
        continue
    for r in batch.to_pylist():
        if r["ts"] < start_ms or r["instrument"] not in universe:
            continue
        if cur is None: cur = r["ts"]
        if r["ts"] != cur:
            rounds.append((cur, bucket)); bucket = {}; cur = r["ts"]
            if len(rounds) >= need: break
        bucket[r["instrument"]] = _snapshot_from_row(r)
    if len(rounds) >= need: break
print(f"[{MODE}] loaded {len(rounds)} rounds", flush=True)

res = build_calendar(cfg).resolve(date(2026, 10, 15))
tmp = tempfile.mkdtemp()
tape = TapeWriter(Path(tmp), date(2026, 10, 15))
orch = Orchestrator(cfg, tape, res)
_start_dashboard(cfg, orch)
time.sleep(1.5)

published = {}          # round_ts -> perf_counter when latest_state was set
round_cost = []
stop = threading.Event()

def engine():
    nxt = time.perf_counter()
    for ts, b in rounds:
        if stop.is_set(): break
        t0 = time.perf_counter()
        orch.process_round(ts, b)
        t1 = time.perf_counter()
        published[orch.latest_state.get("round_ts")] = t1
        round_cost.append((t1 - t0) * 1000)
        nxt += 0.25
        time.sleep(max(0.0, nxt - time.perf_counter()))
    stop.set()

received = {}           # round_ts -> perf_counter first seen by the client
sizes = []

def client_poll():
    c = http.client.HTTPConnection("127.0.0.1", PORT, timeout=10)
    while not stop.is_set():
        t0 = time.perf_counter()
        c.request("GET", "/api/state"); body = c.getresponse().read()
        sizes.append(len(body))
        rt = json.loads(body).get("round_ts")
        received.setdefault(rt, time.perf_counter())
        time.sleep(max(0.0, 0.25 - (time.perf_counter() - t0)))

def client_sse():
    c = http.client.HTTPConnection("127.0.0.1", PORT, timeout=30)
    c.request("GET", "/api/stream"); r = c.getresponse()
    ev = None
    while not stop.is_set():
        line = r.fp.readline()
        if not line: break
        line = line.decode().rstrip("\n")
        if line.startswith("event:"): ev = line[6:].strip()
        elif line.startswith("data:") and ev in ("hot", "full"):
            data = line[5:]; sizes.append(len(data))
            rt = json.loads(data).get("round_ts")
            received.setdefault(rt, time.perf_counter())

te = threading.Thread(target=engine); tc = threading.Thread(target=client_poll if MODE == "poll" else client_sse, daemon=True)
tc.start(); te.start(); te.join(); time.sleep(1.0); stop.set()
tape.close()

pub = [k for k in published if k is not None]
got = [k for k in pub if k in received]
lags = [(received[k] - published[k]) * 1000 for k in got]
lags.sort()
pct = lambda xs, p: xs[min(len(xs) - 1, int(p / 100 * len(xs)))] if xs else float("nan")
rc = sorted(round_cost)
print(json.dumps({
    "mode": MODE, "root": str(ROOT.name),
    "rounds_published": len(pub), "rounds_seen_by_browser": len(got),
    "rounds_never_seen_pct": round(100 * (1 - len(got) / max(1, len(pub))), 1),
    "delivery_lag_ms_p50": round(pct(lags, 50), 1), "p95": round(pct(lags, 95), 1), "max": round(lags[-1] if lags else float('nan'), 1),
    "payload_kb_avg": round(statistics.mean(sizes) / 1024, 1) if sizes else None,
    "engine_round_ms_p50": round(pct(rc, 50), 2), "p99": round(pct(rc, 99), 2), "max_ms": round(rc[-1], 1),
}, indent=1), flush=True)
