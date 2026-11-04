"""Repair a recorded day's tapes (2.1).

    python tools/repair_tapes.py tapes/2026-11-02 [--rebuild-from-jsonl]

Two kinds of damage are fixed:
  * leftover ``<tape>.part-NNNNN.parquet`` files from a killed 2.1 session -> merged into ``<tape>.parquet``
    (fast: row groups are copied, nothing is re-parsed);
  * a footer-less ``<tape>.parquet`` left by a killed 2.0 session beside a complete ``<tape>.jsonl``
    sidecar -> rebuilt from the jsonl (``--rebuild-from-jsonl``; streaming, a few minutes for a 7 GB tick
    tape). The broken file is kept as ``.unreadable.bak``.
Nothing is ever deleted except merged parts.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vajra import schema as S                                   # noqa: E402
from vajra.tape import recover_partition, rebuild_from_jsonl, _readable    # noqa: E402

import pyarrow.parquet as pq                                    # noqa: E402


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    rebuild = "--rebuild-from-jsonl" in argv
    dirs = [Path(a) for a in argv if not a.startswith("--")]
    if not dirs:
        print(__doc__)
        return 2
    rc = 0
    for d in dirs:
        if not d.is_dir():
            print(f"{d}: not a directory"); rc = 1; continue
        t0 = time.perf_counter()
        rec = recover_partition(d)
        print(f"{d.name}: merged parts for {rec or 'nothing'} ({time.perf_counter() - t0:.1f} s)")
        for name in list(S.PARQUET_TAPES) + ["raw_ticks"]:
            p = d / f"{name}.parquet"
            if not p.exists():
                continue
            if _readable(p):                            # (closes its handle: Windows can't rename an open file)
                with pq.ParquetFile(str(p)) as pf:
                    print(f"  {name}.parquet OK ({pf.metadata.num_rows:,} rows)")
                continue
            j = d / f"{name}.jsonl"
            if rebuild and j.exists():
                t1 = time.perf_counter()
                try:
                    n = rebuild_from_jsonl(d, name)
                    print(f"  {name}.parquet UNREADABLE -> rebuilt from jsonl: {n:,} rows ({time.perf_counter() - t1:.0f} s)", flush=True)
                except Exception as e:
                    print(f"  {name}.parquet rebuild FAILED: {e!r}"); rc = 1
            else:
                print(f"  {name}.parquet UNREADABLE (no footer)"
                      + (" -- jsonl present, re-run with --rebuild-from-jsonl" if j.exists() else " -- no jsonl to rebuild from"))
                rc = 1
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
