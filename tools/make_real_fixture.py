"""Extract a tiny REAL fixture (NIFTY/BANKNIFTY/FINNIFTY, 1 Hz) from a recorded tick_tape.
Usage: python3 -I make_real_fixture.py [SRC_DIR] [OUT_DIR]
Streams row-group batches (RAM-safe on the 285 MB tape); writes
OUT_DIR/<date>/tick_tape.parquet + event_tape.jsonl (EXPIRY_RESOLVED only, so replay_session works)."""
import sys, json, datetime as dt
from pathlib import Path
import pyarrow as pa, pyarrow.parquet as pq

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
SRC = Path(sys.argv[1] if len(sys.argv) > 1 else "/home/claude/vajra2/tapes/2026-10-15")
OUT = Path(sys.argv[2] if len(sys.argv) > 2 else "/home/claude/vajra2_review/scratch/29/fixture")
INST = {"NIFTY", "BANKNIFTY", "FINNIFTY"}
WINDOWS = [("09:41:00", "10:15:00"), ("15:10:00", "15:30:00")]

def hms_ms(day, s):
    h, m, sec = map(int, s.split(":"))
    d = dt.datetime.fromisoformat(day).replace(hour=h, minute=m, second=sec, tzinfo=IST)
    return int(d.timestamp() * 1000)

day = SRC.name
wins = [(hms_ms(day, a), hms_ms(day, b)) for a, b in WINDOWS]
pf = pq.ParquetFile(SRC / "tick_tape.parquet")
sec_ts = {}                      # second -> first round ts seen in that second (rounds share ts across instruments)
out_tables = []
for batch in pf.iter_batches(batch_size=4096):
    ts = batch.column("ts").to_pylist(); ins = batch.column("instrument").to_pylist()
    keep = []
    for i, (t, n) in enumerate(zip(ts, ins)):
        if n not in INST or not any(a <= t < b for a, b in wins):
            continue
        s = t // 1000
        first = sec_ts.setdefault(s, t)
        if first == t:
            keep.append(i)
    if keep:
        out_tables.append(pa.Table.from_batches([batch]).take(keep))
tbl = pa.concat_tables(out_tables)
d = OUT / day; d.mkdir(parents=True, exist_ok=True)
pq.write_table(tbl, d / "tick_tape.parquet", compression="zstd", compression_level=9)
for l in open(SRC / "event_tape.jsonl"):
    if '"EXPIRY_RESOLVED"' in l:
        (d / "event_tape.jsonl").write_text(l); break
print("rows", tbl.num_rows, "bytes", (d / "tick_tape.parquet").stat().st_size)
