"""VAJRA synthetic dashboard demo — paced + looping, serves the live dashboard.

Run:  python demo_dashboard.py     then open  http://127.0.0.1:8766
DEV-ONLY synthetic data (non-binding) — for watching the engine animate.
"""
import sys, time, threading, tempfile
from datetime import date
from pathlib import Path

ROOT = str(Path(__file__).resolve().parent)
sys.path.insert(0, ROOT)

from vajra.config import load_config
from vajra.app import build_calendar, write_startup_events
from vajra.tape import TapeWriter
from vajra.orchestrator import Orchestrator
from vajra.realfeed import RealSessionSource
from vajra.dashboard import build_app
import uvicorn

cfg = load_config(ROOT + "/vajra_config.yaml")
cal = build_calendar(cfg)
# Replay a REAL recorded MYTHOS session. Optional day arg: `python demo_dashboard.py 2026-06-23`
_day = sys.argv[1] if len(sys.argv) > 1 else None
_src = RealSessionSource(cfg, session_date=_day)
sess = _src.session_date
res = cal.resolve(sess)
rounds = list(_src.rounds())
tmp = Path(tempfile.mkdtemp(prefix="vajra_demo_"))


class Proxy:
    latest_state = {"round_ts": None, "instruments": {}, "decision": {"decision": "—"},
                    "position": None, "live_orders": False}


proxy = Proxy()
app = build_app(proxy)

# Fail loudly if the port is already taken (don't pretend the dashboard is live).
import socket
with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as _s:
    if _s.connect_ex((cfg.server_host, cfg.server_port)) == 0:
        print(f"ERROR: port {cfg.server_port} is already in use — close the other run "
              f"(Ctrl+C) or change server.port in vajra_config.yaml, then retry.")
        sys.exit(1)

conf = uvicorn.Config(app, host=cfg.server_host, port=cfg.server_port, log_level="error")
server = uvicorn.Server(conf)
threading.Thread(target=server.run, daemon=True).start()
time.sleep(1.0)
print(f"VAJRA dashboard live at http://{cfg.server_host}:{cfg.server_port}  (Ctrl-C to stop)")

day = 0
while True:
    day += 1
    # ONE orchestrator per simulated day -> the trade count accumulates across the
    # whole session (it only resets when a brand-new day starts).
    writer = TapeWriter(tmp, sess, schema_version=cfg.schema_version)
    write_startup_events(writer, cfg, res, 0)
    orch = Orchestrator(cfg, writer, res)
    for ts, snaps in rounds:
        orch.process_round(ts, snaps)
        proxy.latest_state = orch.latest_state
        time.sleep(0.3)
    orch.finalize(*rounds[-1])
    proxy.latest_state = orch.latest_state
    writer.close()
    print(f"day {day} done ({orch.latest_state.get('trades_summary',{}).get('trades_today',0)} trades) — new day in 4s")
    time.sleep(4.0)
