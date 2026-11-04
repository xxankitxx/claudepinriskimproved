#!/usr/bin/env python3
"""VAJRA 2.0.1 — end-of-day forensic report.

    python tools\\eod_report.py tapes\\2026-10-19            (writes tapes\\2026-10-19\\EOD_REPORT.md)
    python tools\\eod_report.py tapes\\2026-10-19 --zip      (also builds <date>_forensics.zip to send)

Reads every tape the engine wrote and answers, from the data alone:
  * did the engine run every round at 4/s, how late, how long did rounds take, where were the gaps
  * what the broker feed did (raw ticks: rate, silences, bursts; frozen / garbage spot episodes)
  * every trade, with the levels and gate state around it
  * every engine sound event vs what the browser actually played (client beacons)
  * what the browser painted vs what the engine pushed (dropped rounds, lag)
The zip holds everything needed to replay and audit the day EXCEPT the multi-GB tick/level/basis jsonl
sidecars (the parquet copies are included; they replay the engine byte-for-byte).
"""
from __future__ import annotations

import json
import sys
import zipfile
from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

IST = timezone(timedelta(hours=5, minutes=30))


def hms(ms):
    return datetime.fromtimestamp(ms / 1000, IST).strftime("%H:%M:%S") if ms else "—"


def jl(path):
    if not path.exists():
        return []
    out = []
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    pass
    return out


def pct(xs, q):
    if not xs:
        return None
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(q * len(xs)))]


def main(argv):
    if not argv:
        print(__doc__)
        return 2
    part = Path(argv[0])
    want_zip = "--zip" in argv
    try:                                                   # 2.1: a killed session leaves parquet parts
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from vajra.tape import recover_partition
        rec = recover_partition(part)
        if rec:
            print(f"recovered parts for: {', '.join(rec)}")
    except Exception as e:
        print(f"(tape recovery skipped: {e!r})")
    L = []
    P = lambda s="": L.append(s)
    P(f"# VAJRA EOD report — {part.name}")
    P()

    # ---------------- rounds ----------------
    rounds = jl(part / "round_tape.jsonl")
    P("## Engine rounds")
    if rounds:
        ts = [r["ts"] for r in rounds]
        gaps = [(ts[i] - ts[i - 1]) for i in range(1, len(ts))]
        big = [(hms(ts[i - 1]), hms(ts[i]), round((ts[i] - ts[i - 1]) / 1000, 1)) for i in range(1, len(ts)) if ts[i] - ts[i - 1] > 2000]
        lag = [r.get("lag_ms") for r in rounds if r.get("lag_ms") is not None]
        proc = [r.get("proc_ms") for r in rounds if r.get("proc_ms") is not None]
        P(f"- rounds: **{len(rounds):,}** from {hms(ts[0])} to {hms(ts[-1])}")
        P(f"- round spacing: median {pct(gaps, .5)} ms, p99 {pct(gaps, .99)} ms, max {max(gaps)} ms")
        P(f"- engine lag (wall clock − round stamp): median {pct(lag, .5)} ms, p99 {pct(lag, .99)} ms, max {max(lag) if lag else None} ms")
        P(f"- processing per round: median {pct(proc, .5)} ms, p99 {pct(proc, .99)} ms, max {max(proc) if proc else None} ms")
        if big:
            P(f"- **gaps > 2 s: {len(big)}**")
            for a, b, g in big[:30]:
                P(f"    - {a} → {b}: {g} s")
        h = Counter(r.get("health") for r in rounds)
        P(f"- feed health by round: {dict(h)}")
        ep = []
        cur = None
        for r in rounds:
            st = r.get("health") or "ok"
            if st != "ok" and cur is None:
                cur = [hms(r["ts"]), st, r["ts"]]
            elif st == "ok" and cur is not None:
                ep.append((cur[0], hms(r["ts"]), cur[1], round((r["ts"] - cur[2]) / 1000)))
                cur = None
        if cur is not None:
            ep.append((cur[0], "end", cur[1], None))
        if ep:
            P(f"- feed-gate episodes (closed → open): {len(ep)}")
            for a, b, why, secs in ep:
                P(f"    - {a} → {b} ({why}, {secs} s)")
        g = Counter(r.get("gate") for r in rounds)
        P(f"- entry gate by round: {dict(g.most_common())}")
    else:
        P("- round_tape.jsonl missing (engine older than 2.0.1 or never reached 09:20)")
    P()

    # ---------------- raw ticks ----------------
    P("## Broker feed (raw websocket ticks)")
    raw = part / "raw_ticks.parquet"
    if raw.exists():
        try:
            import pyarrow.parquet as pq
            pf = pq.ParquetFile(raw)
            n = pf.metadata.num_rows
            per_sec = Counter()
            first = last = None
            kinds = Counter()
            spot_rows = []
            for b in pf.iter_batches(batch_size=200_000, columns=["recv_ms", "kind", "stock_code", "ltp"]):
                rm = b.column("recv_ms").to_pylist()
                kd = b.column("kind").to_pylist()
                sc = b.column("stock_code").to_pylist()
                lp = b.column("ltp").to_pylist()
                for t, k, s, l in zip(rm, kd, sc, lp):
                    per_sec[t // 1000] += 1
                    kinds[k] += 1
                    if k == "cash" and s == "NIFTY":
                        spot_rows.append((t, l))
                if first is None:
                    first = rm[0]
                last = rm[-1]
            P(f"- ticks: **{n:,}** from {hms(first)} to {hms(last)}; by kind {dict(kinds)}")
            secs = sorted(per_sec)
            rates = [per_sec[s] for s in secs]
            P(f"- ticks/second: median {pct(rates, .5)}, p99 {pct(rates, .99)}, max {max(rates) if rates else 0}")
            silent = [(hms(secs[i - 1] * 1000), (secs[i] - secs[i - 1])) for i in range(1, len(secs)) if secs[i] - secs[i - 1] > 5]
            if silent:
                P(f"- **silences > 5 s on the websocket: {len(silent)}**")
                for a, g in silent[:30]:
                    P(f"    - after {a}: {g} s with no tick at all")
            if spot_rows:
                frz = []
                prev_t, prev_v = spot_rows[0]
                since = prev_t
                for t, v in spot_rows[1:]:
                    if v != prev_v:
                        if t - since > 20_000:
                            frz.append((hms(since), hms(t), round((t - since) / 1000)))
                        since = t
                        prev_v = v
                    prev_t = t
                P(f"- NIFTY cash prints: {len(spot_rows):,}; unchanged > 20 s: {len(frz)}")
                for a, b, s in frz[:20]:
                    P(f"    - {a} → {b}: spot frozen {s} s")
        except Exception as e:
            P(f"- raw_ticks.parquet unreadable: {e!r}")
    else:
        P("- raw_ticks.parquet missing (not live mode, or tape.raw_ticks off)")
    P()

    # ---------------- trades ----------------
    orders = jl(part / "order_tape.jsonl")
    P("## Trades")
    ent = None
    n_tr = 0
    tot = 0.0
    rej = Counter()
    for r in orders:
        if r.get("action") == "ENTRY":
            ent = r
        elif r.get("action") == "EXIT":
            n_tr += 1
            tot += float(r.get("pnl_realized") or 0)
            P(f"- {hms(ent['ts']) if ent else '?'} → {hms(r['ts'])}  {r.get('right')} {r.get('strike')} ×{r.get('qty')}  "
              f"in {ent.get('fill_price') if ent else '?'} out {r.get('fill_price')}  **{float(r.get('pnl_realized') or 0):+,.0f}**  {r.get('exit_reason')}")
            ent = None
        elif r.get("action") == "REJECT":
            rej[r.get("exit_reason")] += 1
    if ent is not None:
        P(f"- **OPEN at end**: {ent.get('right')} {ent.get('strike')} ×{ent.get('qty')} @ {ent.get('fill_price')} (entered {hms(ent['ts'])})")
    P(f"- closed trades: {n_tr}, realized **{tot:+,.0f}**; rejects: {dict(rej)}")
    P()

    # ---------------- sounds: engine vs browser ----------------
    events = jl(part / "event_tape.jsonl")
    snd = [e for e in events if e.get("event_type") == "SOUND"]
    clients = jl(part / "client_tape.jsonl")
    P("## Sounds — engine vs browser")
    P(f"- engine sound events: {len(snd)}  {dict(Counter((e.get('payload') or {}).get('kind') for e in snd))}")
    for e in snd:
        p = e.get("payload") or {}
        P(f"    - {hms(e['ts'])} #{p.get('seq')} {p.get('kind')}: {p.get('text')}")
    played = []
    for c in clients:
        for t, k in c.get("sounds") or []:
            played.append((t, k))
    played.sort()
    P(f"- browser played: {len(played)}  {dict(Counter(k for _, k in played))}")
    for t, k in played:
        P(f"    - {hms(t)} {k}")
    if clients:
        last_c = clients[-1]
        P(f"- sound mode at end: {last_c.get('snd_mode')}, locked: {last_c.get('locked')}")
        if len(snd) and not played:
            P("- **WARNING: the engine issued sounds but the browser played none** (sound pill off/locked, or page not open)")
    P()

    # ---------------- browser delivery ----------------
    P("## Browser delivery (what the page painted)")
    if clients:
        rounds_seen = sum(int(c.get("rounds") or 0) for c in clients)
        paints = sum(int(c.get("paints") or 0) for c in clients)
        modes = Counter(c.get("mode") for c in clients)
        maxlag = max((int(c.get("max_lag_ms") or 0) for c in clients if 0 <= int(c.get("max_lag_ms") or 0) < 3_600_000), default=0)
        avgl = [int(c["avg_lag_ms"]) for c in clients if c.get("avg_lag_ms") is not None and 0 <= int(c["avg_lag_ms"]) < 3_600_000]
        hidden = sum(1 for c in clients if c.get("hidden"))
        P(f"- beacons: {len(clients)} (every 10 s while a page was open), first {hms(clients[0]['recv_ms'])}, last {hms(clients[-1]['recv_ms'])}")
        P(f"- distinct engine rounds painted: **{rounds_seen:,}** of {len(rounds):,} engine rounds; paints {paints:,}")
        P(f"- delivery mode: {dict(modes)}; lag median {pct(avgl, .5)} ms, worst {maxlag} ms; beacons sent from a hidden tab: {hidden}")
        boots = Counter(c.get("boot_id") for c in clients)
        if len(boots) > 1:
            P(f"- engine boot ids seen by the page: {len(boots)} (restarts)")
    else:
        P("- no client beacons: the dashboard page was not open (or an older dashboard)")
    P()

    # ---------------- events ----------------
    P("## Events")
    ec = Counter()
    for e in events:
        p = e.get("payload") or {}
        ec[e.get("event_type") + ":" + str(p.get("stage") or p.get("kind") or p.get("reason") or "")] += 1
    for k, v in ec.most_common(40):
        P(f"- {k}: {v}")
    sess = [e for e in events if e.get("event_type") in ("SESSION_ERROR", "RESTORED", "SHUTDOWN", "REGIME")]
    if sess:
        P()
        P("Session events:")
        for e in sess:
            P(f"- {hms(e['ts'])} {e['event_type']}: {json.dumps(e.get('payload'))[:300]}")
    P()

    # ---------------- levels ----------------
    P("## NIFTY level anchors")
    try:
        import pyarrow.parquet as pq
        lt = part / "level_tape.parquet"
        if lt.exists():
            seen = set()
            n = 0
            for b in pq.ParquetFile(lt).iter_batches(batch_size=100_000):
                d = b.to_pylist()
                for r in d:
                    if r.get("instrument") != "NIFTY" or r.get("recalc_trigger") == "held":
                        continue
                    k = (r["ts"], r.get("recalc_trigger"))
                    if k in seen:
                        continue
                    seen.add(k)
                    n += 1
                    if n <= 60:
                        _f = lambda v: None if v is None else round(float(v), 2)
                        P(f"- {hms(r['ts'])} {r.get('recalc_trigger')}: L {_f(r.get('lower_level'))} / U {_f(r.get('upper_level'))}"
                          + (f" (legacy {_f(r.get('legacy_lower'))} / {_f(r.get('legacy_upper'))})" if r.get('legacy_upper') not in (None, r.get('upper_level')) else ""))
            P(f"- anchors: {n}")
    except Exception as e:
        P(f"- level_tape unreadable: {e!r}")

    out = part / "EOD_REPORT.md"
    out.write_text("\n".join(L), encoding="utf-8")
    print("\n".join(L))
    print(f"\nwritten: {out}")

    if want_zip:
        z = part.parent / f"{part.name}_forensics.zip"
        keep = ["EOD_REPORT.md", "event_tape.jsonl", "order_tape.jsonl", "signal_tape.jsonl", "round_tape.jsonl",
                "client_tape.jsonl", "decision_tape.jsonl", "tick_tape.parquet", "level_tape.parquet",
                "basis_tape.parquet", "decision_tape.parquet", "signal_tape.parquet", "order_tape.parquet",
                "raw_ticks.parquet"]
        with zipfile.ZipFile(z, "w", zipfile.ZIP_DEFLATED) as zf:
            for name in keep:
                f = part / name
                if f.exists():
                    zf.write(f, arcname=f"{part.name}/{name}")
            for f in part.glob("*.bak"):
                zf.write(f, arcname=f"{part.name}/{f.name}")
        print(f"zip for sending: {z}  ({z.stat().st_size / 1e6:.0f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
