"""VAJRA lab (2.2) -- fast engine-true what-if runs over every recorded day.

    python tools/lab.py cache                      # build the per-day NIFTY caches (once per day, ~2 min each)
    python tools/lab.py run base                   # replay every cached day with the shipped config
    python tools/lab.py run sl10 --set execution.hard_sl_pct=0.10
    python tools/lab.py run x --set a.b=1 --set c.d=2 --days 2026-11-06 2026-11-09 --stride 2
    python tools/lab.py compare base sl10          # day-by-day table of two finished runs

How it differs from replay.py: only NIFTY is replayed (the sisters / stocks are advisory -- they never
gate or size a trade), nothing is written to tape, and the day is held as flat numpy arrays, so one day
runs in well under a minute instead of ~45. The engine code is the real Orchestrator + Executor; only
the cross-instrument verdict on the alert differs (always LONE). ``--stride N`` feeds every Nth
recorded round (default 2 = two rounds a second).

Results land in tapes/_cache/lab/runs/<name>.json: per day the trades, P&L and counts by exit reason.
A run named ``base`` is the yardstick; ``compare`` prints the delta per day and in total.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

LAB = ROOT / "tapes" / "_cache" / "lab"
RUNS = LAB / "runs"


# ---- cache ---------------------------------------------------------------------------------------
def build_cache(day: str, tape_root: Path | None = None) -> str:
    import numpy as np
    import pyarrow.compute as pc
    import pyarrow.parquet as pq
    from vajra.tape import parquet_sources
    tape_root = tape_root or (ROOT / "tapes")
    out = LAB / f"{day}.npz"
    if out.exists():
        return f"{day}: cached"
    t0 = time.perf_counter()
    cols = {k: [] for k in ("ts", "spot", "vix", "fut", "n", "k", "ce", "ltp", "vwap", "vfeed", "vol", "oi",
                            "bid", "ask")}
    mode = None
    for f in parquet_sources(tape_root / day, "tick_tape"):
        with pq.ParquetFile(str(f)) as pf:
            for rg in range(pf.metadata.num_row_groups):
                tb = pf.read_row_group(rg, columns=["ts", "instrument", "spot", "future", "basis_mode", "vix",
                                                    "strikes"])
                tb = tb.filter(pc.equal(tb.column("instrument"), "NIFTY"))
                if tb.num_rows == 0:
                    continue
                tb = tb.combine_chunks()
                if mode is None:
                    mode = tb.column("basis_mode")[0].as_py()
                la = tb.column("strikes").chunk(0)
                fl = pc.list_flatten(la)
                cols["ts"].append(tb.column("ts").to_numpy())
                for name, c in (("spot", "spot"), ("vix", "vix"), ("fut", "future")):
                    cols[name].append(tb.column(c).to_numpy(zero_copy_only=False).astype("float64"))
                cols["n"].append(pc.list_value_length(la).to_numpy(zero_copy_only=False).astype("int32"))
                cols["k"].append(fl.field("strike").to_numpy(zero_copy_only=False).astype("int32"))
                cols["ce"].append(pc.equal(fl.field("right"), "CE").to_numpy(zero_copy_only=False))
                cols["vfeed"].append(pc.equal(fl.field("vwap_source"), "feed").to_numpy(zero_copy_only=False))
                for name in ("ltp", "vwap", "bid", "ask"):
                    cols[name].append(fl.field(name).to_numpy(zero_copy_only=False).astype("float64"))
                for name, c in (("vol", "volume"), ("oi", "oi")):
                    cols[name].append(fl.field(c).to_numpy(zero_copy_only=False).astype("int64"))
    if not cols["ts"]:
        return f"{day}: no NIFTY rows"
    arr = {k: np.concatenate(v) for k, v in cols.items()}
    LAB.mkdir(parents=True, exist_ok=True)
    tmp = LAB / f"{day}.tmp.npz"
    np.savez_compressed(tmp, mode=np.array(mode or "IMPLIED"), **arr)
    tmp.replace(out)
    return f"{day}: {len(arr['ts']):,} rounds, {len(arr['k']):,} quotes ({time.perf_counter() - t0:.0f} s)"


class _CacheSource:
    """Feeds the cached NIFTY rounds to the Orchestrator exactly like ReplaySource does."""

    def __init__(self, day: str, stride: int = 2):
        import numpy as np
        self.z = np.load(LAB / f"{day}.npz")
        self.stride = max(1, int(stride))

    def rounds(self):
        import numpy as np
        from vajra.marketdata import MarketSnapshot, StrikeQuote
        z = self.z
        ts, spot, vix, fut, n = z["ts"], z["spot"], z["vix"], z["fut"], z["n"]
        actual = str(z["mode"]) == "ACTUAL"
        off = np.concatenate([[0], np.cumsum(n)])
        k, ce = z["k"].tolist(), z["ce"].tolist()
        ltp, vwap, bid, ask = z["ltp"].tolist(), z["vwap"].tolist(), z["bid"].tolist(), z["ask"].tolist()
        vfeed, vol, oi = z["vfeed"].tolist(), z["vol"].tolist(), z["oi"].tolist()
        tsl, spl, vxl, ful, ofl = ts.tolist(), spot.tolist(), vix.tolist(), fut.tolist(), off.tolist()
        for i in range(0, len(tsl), self.stride):
            strikes = {}
            for j in range(ofl[i], ofl[i + 1]):
                r = "CE" if ce[j] else "PE"
                strikes[(k[j], r)] = StrikeQuote(strike=k[j], right=r, ltp=ltp[j], vwap=vwap[j],
                                                 vwap_source="feed" if vfeed[j] else "computed",
                                                 volume=vol[j], oi=oi[j], bid=bid[j], ask=ask[j])
            f = ful[i]
            yield tsl[i], {"NIFTY": MarketSnapshot(ts_ms=tsl[i], instrument="NIFTY", spot=spl[i],
                                                   vix=(vxl[i] if vxl[i] == vxl[i] else 0.0),
                                                   future_traded=(f if (actual and f == f) else None),
                                                   strikes=strikes)}


class _LabTape:
    """Keeps the orders and the events; every other tape call is a no-op."""

    def __init__(self):
        self.orders, self.events = [], []

    def append(self, name, row):
        if name == "order_tape":
            self.orders.append(dict(row))

    def event(self, event_type, payload, ts_ms=None):
        self.events.append({"ts": ts_ms, "event_type": event_type, "payload": payload})

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        return lambda *a, **k: None


def run_day(args) -> dict:
    day, sets, stride = args
    import io
    import contextlib
    from vajra.config import load_config
    from vajra.orchestrator import Orchestrator
    import replay as _replay
    cfg = load_config(None)
    _replay._apply_overrides(cfg, sets)
    res = _replay._read_expiry_from_events(ROOT / "tapes" / day, date.fromisoformat(day), cfg)
    tape = _LabTape()
    t0 = time.perf_counter()
    with contextlib.redirect_stderr(io.StringIO()):
        orch = Orchestrator(cfg, tape, res, breeze=None)
        orch.run(_CacheSource(day, stride))
    trades, ent = [], None
    for r in tape.orders:
        if r["action"] == "ENTRY":
            ent = r
        elif r["action"] == "EXIT" and ent is not None:
            trades.append({"ets": ent["ts"], "xts": r["ts"], "strike": r["strike"], "right": r["right"],
                           "qty": r["qty"], "in": ent["fill_price"], "out": r["fill_price"],
                           "pnl": r["pnl_realized"], "reason": r["exit_reason"]})
            ent = None
    # entry context: the APPROACH_ALERT of the entry round and the TRADE event of the exit round
    alerts = {e["ts"]: e["payload"] for e in tape.events if e["event_type"] == "APPROACH_ALERT"}
    closed = {e["payload"].get("entry_ts"): e["payload"] for e in tape.events if e["event_type"] == "TRADE"}
    for t in trades:
        a = alerts.get(t["ets"]) or {}
        c = closed.get(t["ets"]) or {}
        t["level"], t["spot"], t["dist"] = a.get("level"), a.get("spot"), a.get("dist_pts")
        t["peak_pct"], t["low_pct"] = c.get("peak_pct"), c.get("low_pct")
        t["ctx"] = {k: v for k, v in a.items() if k not in ("same_side", "opp_side", "pulls_by_instrument",
                                                               "direction", "level_side", "level", "spot",
                                                               "dist_pts", "verdict", "confidence", "conflicted",
                                                               "regime", "buy")}
        t["same_side_prev"] = (c.get("ctx") or {}).get("same_side")
    by = {}
    for t in trades:
        b = by.setdefault(t["reason"], [0, 0.0])
        b[0] += 1
        b[1] += t["pnl"]
    shadow = [e["payload"] for e in tape.events if e["event_type"] == "SHADOW_FILTER"]
    errors = [str(e["payload"].get("reason"))[:160] for e in tape.events
              if e["event_type"] == "SESSION_ERROR" and e["payload"].get("stage") == "run_loop"]
    return {"day": day, "expiry": bool(res.is_expiry_day), "pnl": round(sum(t["pnl"] for t in trades), 2),
            "n": len(trades), "wins": sum(1 for t in trades if t["pnl"] > 0), "by_reason": by,
            "rejects": sum(1 for r in tape.orders if r["action"] == "REJECT"),
            "trades": trades, "shadow": shadow, "errors": errors, "secs": round(time.perf_counter() - t0, 1)}


def cached_days() -> list[str]:
    return sorted(p.stem for p in LAB.glob("20*.npz") if ".tmp" not in p.name)


def cmd_cache(a) -> int:
    days = a.days or sorted(d.name for d in (ROOT / "tapes").iterdir()
                            if d.is_dir() and d.name[:2] == "20" and (d / "order_tape.jsonl").exists())
    for d in days:
        try:
            print(build_cache(d), flush=True)
        except Exception as e:
            print(f"{d}: FAILED {e!r}", flush=True)
    return 0


def cmd_run(a) -> int:
    days = a.days or cached_days()
    RUNS.mkdir(parents=True, exist_ok=True)
    jobs = [(d, a.sets, a.stride) for d in days]
    t0 = time.perf_counter()
    if a.workers <= 1:
        res = [run_day(j) for j in jobs]
    else:
        with ProcessPoolExecutor(max_workers=a.workers) as ex:
            res = list(ex.map(run_day, jobs))
    out = {"name": a.name, "sets": a.sets, "stride": a.stride, "days": res}
    (RUNS / f"{a.name}.json").write_text(json.dumps(out), encoding="utf-8")
    _print_run(out)
    print(f"({time.perf_counter() - t0:.0f} s)")
    return 0


def _print_run(out) -> None:
    tot = n = w = 0
    print(f"== {out['name']}  {' '.join(out['sets']) or '(shipped config)'}  stride {out['stride']}")
    for r in out["days"]:
        tot += r["pnl"]; n += r["n"]; w += r["wins"]
        br = " ".join(f"{k}:{v[0]}" for k, v in sorted(r["by_reason"].items()))
        print(f"  {r['day']}{' E' if r['expiry'] else '  '} trades {r['n']:3d} wins {r['wins']:3d} "
              f"pnl {r['pnl']:+10.0f}   {br}")
        if r.get("errors"):                 # the engine swallowed a crash (e.g. out of memory): not a result
            print(f"     !! RUN FAILED on this day -- rerun it: {r['errors'][0]}")
    print(f"  TOTAL trades {n} wins {w} ({100 * w / max(1, n):.0f}%) pnl {tot:+.0f}")


def cmd_compare(a) -> int:
    runs = [json.loads((RUNS / f"{n}.json").read_text(encoding="utf-8")) for n in a.names]
    base = {r["day"]: r for r in runs[0]["days"]}
    print("day          " + "".join(f"{n:>22s}" for n in a.names))
    tot = [0.0] * len(runs)
    cnt = [[0, 0] for _ in runs]
    failed = []
    for d in sorted(base):
        line = f"{d}{' E' if base[d]['expiry'] else '  '} "
        for i, ru in enumerate(runs):
            r = next((x for x in ru["days"] if x["day"] == d), None)
            if r is None:
                line += f"{'-':>22s}"
                continue
            tot[i] += r["pnl"]; cnt[i][0] += r["n"]; cnt[i][1] += r["wins"]
            line += f"{r['pnl']:+12.0f} ({r['n']:2d}/{r['wins']:2d})"
            if r.get("errors"):
                failed.append(f"{ru['name']} {d}")
        print(line)
    print("TOTAL        " + "".join(f"{t:+12.0f} ({c[0]:3d}/{c[1]:3d})"[-22:].rjust(22) for t, c in zip(tot, cnt)))
    if failed:
        print("!! these day runs FAILED part-way (their numbers are not results; rerun them): " + ", ".join(failed))
    for i, ru in enumerate(runs[1:], 1):
        days_better = sum(1 for r in ru["days"] if r["day"] in base and r["pnl"] > base[r["day"]]["pnl"] + 1)
        days_worse = sum(1 for r in ru["days"] if r["day"] in base and r["pnl"] < base[r["day"]]["pnl"] - 1)
        print(f"{ru['name']} vs {runs[0]['name']}: {tot[i] - tot[0]:+.0f}  (better on {days_better} days, "
              f"worse on {days_worse})")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="VAJRA lab: fast what-if replays")
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("cache"); c.add_argument("days", nargs="*"); c.set_defaults(fn=cmd_cache)
    r = sub.add_parser("run"); r.add_argument("name")
    r.add_argument("--set", action="append", dest="sets", default=[])
    r.add_argument("--days", nargs="*", default=[])
    r.add_argument("--stride", type=int, default=2)
    r.add_argument("--workers", type=int, default=3)
    r.set_defaults(fn=cmd_run)
    m = sub.add_parser("compare"); m.add_argument("names", nargs="+"); m.set_defaults(fn=cmd_compare)
    a = p.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    raise SystemExit(main())
