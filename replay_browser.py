"""Replay a recorded day's tape through the CURRENT code and watch the cockpit in the browser.

    python replay_browser.py                # replays 2026-06-30 (default)
    python replay_browser.py 2026-06-30     # any recorded day under tapes/<date>/
    python replay_browser.py 2026-06-30 8771 # ... on a custom port

Then open  http://127.0.0.1:8770  (or the port you passed).

Safe by design:
- Reads tapes/<date>/tick_tape.parquet in batches -> never loads the whole (multi-GB) file into RAM.
- Writes NO tapes (a null writer) -> your real recorded data is never touched.
- Uses a separate port (default 8770) so it can't collide with a live run on 8766.
- Stays up after the replay finishes so you can review the final journal + capital.
"""
import sys, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))                       # run from anywhere

import pyarrow.parquet as pq
from datetime import date, datetime, timezone, timedelta
from vajra.config import load_config
from vajra.app import build_calendar, _start_dashboard
from vajra.orchestrator import Orchestrator
from vajra.feed import _snapshot_from_row

DAY = sys.argv[1] if len(sys.argv) > 1 else "2026-06-30"
PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 8770
IST = timezone(timedelta(hours=5, minutes=30))
def hm(ms): return datetime.fromtimestamp(ms / 1000, tz=IST).strftime('%H:%M:%S')

class NullWriter:                                   # discards all tape writes
    def __getattr__(self, n): return lambda *a, **k: None

class StreamReplay:
    def __init__(self, pqt, orch): self.pqt = pqt; self.orch = orch
    def prev_close_snapshot(self): return None
    def rounds(self):
        cur = None; bucket = {}; n = 0
        for batch in pq.ParquetFile(self.pqt).iter_batches(batch_size=20000):
            for r in batch.to_pylist():
                ts = int(r['ts'])
                if cur is None: cur = ts
                if ts != cur:
                    n += 1
                    if n % 4000 == 0:
                        cap = self.orch.latest_state.get('capital', {})
                        print(f'[replay] {hm(cur)} round {n} trades={len(self.orch.journal)} '
                              f'equity={cap.get("equity")}', flush=True)
                    time.sleep(0.003)               # pace + release the GIL so the dashboard stays responsive
                    yield cur, bucket; bucket = {}; cur = ts
                try: bucket[r['instrument']] = _snapshot_from_row(r)
                except Exception: pass
        if bucket and cur is not None:
            yield cur, bucket

tick = ROOT / "tapes" / DAY / "tick_tape.parquet"
if not tick.exists():
    sys.exit(f"No tape found at {tick} — pick a date that exists under tapes/")

cfg = load_config(str(ROOT / "vajra_config.yaml"))
cfg.raw.setdefault('server', {})['port'] = PORT
y, m, d = map(int, DAY.split('-'))
res = build_calendar(cfg).resolve(date(y, m, d))
orch = Orchestrator(cfg, NullWriter(), res, breeze=None)
server = _start_dashboard(cfg, orch)
print(f'DASHBOARD UP  ->  http://{cfg.server_host}:{cfg.server_port}   (replaying {DAY})', flush=True)
try:
    orch.run(StreamReplay(str(tick), orch))
except Exception as e:
    import traceback; print('replay error:', e); traceback.print_exc()
cap = orch.latest_state.get('capital', {})
print(f'REPLAY DONE  trades={len(orch.journal)}  equity={cap.get("equity")}  pnl={cap.get("pnl")} '
      f'— dashboard staying up at http://{cfg.server_host}:{cfg.server_port}  (Ctrl+C to quit)', flush=True)
while True:
    time.sleep(10)
