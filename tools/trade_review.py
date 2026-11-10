#!/usr/bin/env python3
"""VAJRA 2.2 -- trade review: every round trip of one or more days with the context it was taken in.

    python tools/trade_review.py                      # every recorded day
    python tools/trade_review.py 2026-11-09           # one day
    python tools/trade_review.py 2026-11-04 2026-11-09 --csv review.csv

For each trade: entry / exit, P&L, how far the premium went each way, how the entry came about (seconds
since the previous exit and whether that was a profit or a stop, NIFTY's move toward the level over the
last 30 s, whether the level was already breached, how late the feed was), and the exit reason. Then the
same trades grouped by those conditions, so the question "which kind of entry loses money?" is answered
from the tape in seconds.

Days recorded by 2.2 or later carry all of this in the event tape (TRADE + APPROACH_ALERT). For older
days the entries come from the order tape and only the fields the old tapes held are filled in.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
IST = timezone(timedelta(hours=5, minutes=30))


def hms(ms):
    return datetime.fromtimestamp(ms / 1000, IST).strftime("%H:%M:%S") if ms else "-"


def jl(path: Path):
    if not path.exists():
        return
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if line:
                try:
                    yield json.loads(line)
                except ValueError:
                    continue


def day_trades(part: Path) -> list[dict]:
    """Round trips of one day, newest schema first: TRADE events, else the order tape."""
    events = list(jl(part / "event_tape.jsonl"))
    alerts = {e["ts"]: (e.get("payload") or {}) for e in events if e.get("event_type") == "APPROACH_ALERT"}
    expiry = any((e.get("payload") or {}).get("is_expiry_day") for e in events
                 if e.get("event_type") == "EXPIRY_RESOLVED")
    out = []
    recs = [e["payload"] for e in events if e.get("event_type") == "TRADE"]
    if not recs:                                       # pre-2.2 day: rebuild from the order tape
        ent = None
        for r in jl(part / "order_tape.jsonl"):
            if r.get("action") == "ENTRY":
                ent = r
            elif r.get("action") == "EXIT" and ent is not None:
                e = ent["fill_price"]
                recs.append({"entry_ts": ent["ts"], "exit_ts": r["ts"], "right": r["right"], "strike": r["strike"],
                             "direction": "UP" if r["right"] == "CE" else "DOWN", "qty": r["qty"],
                             "entry_price": e, "exit_price": r["fill_price"], "pnl": r["pnl_realized"],
                             "pnl_pct": round((r["fill_price"] / e - 1) * 100, 1) if e else 0.0,
                             "exit_reason": r.get("exit_reason"), "peak_pct": None, "low_pct": None, "ctx": None})
                ent = None
    prev = None
    for t in recs:
        a = alerts.get(t["entry_ts"], {})
        ctx = dict(t.get("ctx") or {})
        if ctx.get("secs_since_exit") is None and prev is not None:
            ctx["secs_since_exit"] = round((t["entry_ts"] - prev["exit_ts"]) / 1000, 1)
            ctx["after"] = "stop" if prev.get("exit_reason") == "SL_HIT" else "profit" if prev["pnl"] > 0 else None
        for k in ("move_30s", "exch_lag_s"):
            if ctx.get(k) is None and a.get(k) is not None:
                ctx[k] = a[k]
        if ctx.get("dist_pts") is None:
            ctx["dist_pts"] = a.get("dist_pts")
        if ctx.get("breached") is None and ctx.get("dist_pts") is not None:
            ctx["breached"] = ctx["dist_pts"] < 0
        out.append({**t, "ctx": ctx, "day": part.name, "expiry": bool(expiry),
                    "verdict": t.get("verdict") or a.get("verdict"),
                    "hold_s": round((t["exit_ts"] - t["entry_ts"]) / 1000)})
        prev = t
    return out


def _bucket(x, edges, unit=""):
    if x is None:
        return "n/a"
    lo = None
    for e in edges:
        if x < e:
            return (f"< {e}{unit}" if lo is None else f"{lo} to {e}{unit}")
        lo = e
    return f">= {edges[-1]}{unit}"


GROUPS = [
    ("day type", lambda t: "expiry day" if t["expiry"] else "non-expiry"),
    ("exit reason", lambda t: t.get("exit_reason") or "?"),
    ("entry came", lambda t: ("first of the day" if t["ctx"].get("secs_since_exit") is None else
                              "within 20 s of a profit" if (t["ctx"].get("after") == "profit"
                                                           and t["ctx"]["secs_since_exit"] < 20) else
                              "later after a profit" if t["ctx"].get("after") == "profit" else
                              "after a stop" if t["ctx"].get("after") == "stop" else "after a scratch")),
    ("level at entry", lambda t: ("n/a" if t["ctx"].get("breached") is None else
                                  "already breached" if t["ctx"]["breached"] else "approaching")),
    ("NIFTY move toward level, last 30 s", lambda t: _bucket(t["ctx"].get("move_30s"), [0, 3, 10], " pts")),
    ("feed lag at entry", lambda t: _bucket(t["ctx"].get("exch_lag_s"), [2, 6, 15], " s")),
    ("hour", lambda t: hms(t["entry_ts"])[:2] + ":00"),
    ("option", lambda t: t["right"]),
]


def print_groups(trades) -> None:
    for name, key in GROUPS:
        g = defaultdict(list)
        for t in trades:
            g[key(t)].append(t)
        if len(g) == 1 and "n/a" in g:
            continue
        print(f"\n  by {name}")
        for k in sorted(g):
            v = g[k]
            w = sum(1 for t in v if t["pnl"] > 0)
            avg = sum(t["pnl_pct"] or 0 for t in v) / len(v)
            print(f"    {k:28s} trades {len(v):3d}  won {100 * w / len(v):5.1f}%  "
                  f"P&L {sum(t['pnl'] for t in v):+11,.0f}  avg {avg:+5.1f}%")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="VAJRA trade review")
    p.add_argument("days", nargs="*", help="YYYY-MM-DD (one day) or two dates for a range; default all")
    p.add_argument("--tapes", default=str(ROOT / "tapes"))
    p.add_argument("--csv", default=None, help="also write every trade to this CSV file")
    p.add_argument("--quiet", action="store_true", help="groups only, no per-trade lines")
    a = p.parse_args(argv)
    root = Path(a.tapes)
    parts = sorted(d for d in root.iterdir() if d.is_dir() and d.name[:2] == "20" and len(d.name) == 10)
    if len(a.days) == 1:
        parts = [d for d in parts if d.name == a.days[0]]
    elif len(a.days) >= 2:
        parts = [d for d in parts if a.days[0] <= d.name <= a.days[1]]
    trades = []
    for part in parts:
        tr = day_trades(part)
        if not tr:
            continue
        trades += tr
        pnl = sum(t["pnl"] for t in tr)
        print(f"\n== {part.name} {'(expiry)' if tr[0]['expiry'] else ''}  trades {len(tr)}  "
              f"won {sum(1 for t in tr if t['pnl'] > 0)}  P&L {pnl:+,.0f}")
        if a.quiet:
            continue
        for t in tr:
            c = t["ctx"]
            bits = []
            if c.get("secs_since_exit") is not None:
                bits.append(f"{c['secs_since_exit']:.0f}s after {c.get('after') or 'exit'}")
            if c.get("breached"):
                bits.append("breached")
            if c.get("move_30s") is not None:
                bits.append(f"move30 {c['move_30s']:+.1f}")
            if c.get("exch_lag_s") is not None and c["exch_lag_s"] > 2:
                bits.append(f"feed {c['exch_lag_s']:.0f}s late")
            pk = "" if t.get("peak_pct") is None else f" peak {t['peak_pct']:+.1f}% low {t['low_pct']:+.1f}%"
            print(f"  {hms(t['entry_ts'])} {t['strike']}{t['right']} {t['entry_price']:7.2f} -> {t['exit_price']:7.2f} "
                  f"{t['pnl_pct']:+6.1f}% {t['pnl']:+10,.0f} {t['hold_s']:5d}s {str(t.get('exit_reason')):14s}"
                  f"{pk}  {' | '.join(bits)}")
    if not trades:
        print("no trades found")
        return 1
    w = sum(1 for t in trades if t["pnl"] > 0)
    print(f"\n== ALL  days {len({t['day'] for t in trades})}  trades {len(trades)}  won {w} "
          f"({100 * w / len(trades):.0f}%)  P&L {sum(t['pnl'] for t in trades):+,.0f}")
    print_groups(trades)
    if a.csv:
        cols = ["day", "expiry", "entry_ts", "exit_ts", "right", "strike", "qty", "entry_price", "exit_price",
                "pnl", "pnl_pct", "hold_s", "exit_reason", "verdict", "peak_pct", "low_pct"]
        ck = ["secs_since_exit", "after", "breached", "dist_pts", "move_30s", "move_120s", "exch_lag_s"]
        with open(a.csv, "w", newline="", encoding="utf-8") as fh:
            wr = csv.writer(fh)
            wr.writerow(cols + ck)
            for t in trades:
                wr.writerow([t.get(c) for c in cols] + [t["ctx"].get(c) for c in ck])
        print(f"\nwrote {a.csv}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
