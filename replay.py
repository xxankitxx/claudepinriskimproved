#!/usr/bin/env python3
"""replay.py — re-run the §5 engine deterministically over a recorded tick_tape.

The tape is the substrate for tuning (spec §9 refinement loop): replay a recorded
session through the same modules, optionally perturbing parameters (PAD,
ITM_DEPTH, bias_threshold, confirm_secs, weights, routing_mode), and compare
outcome deltas against the new order_tape.

Determinism (spec §11.5): replaying a fixed tick_tape yields byte-identical
decision_tape across runs — there is no wall-clock or RNG in the decision path;
all timing comes from the tape timestamps.

Usage:
    python replay.py --tape tapes/2026-06-16 --out tapes_replay
    python replay.py --tape tapes/2026-06-16 --out tapes_replay --set trigger.pad_floor=0.15
"""

from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path

from vajra.config import load_config
from vajra.expiry import ExpiryResolution
from vajra.tape import TapeWriter
from vajra.orchestrator import Orchestrator
from vajra.feed import ReplaySource


def _read_expiry_from_events(tape_dir: Path, session_date: date, cfg) -> ExpiryResolution:
    ev = tape_dir / "event_tape.jsonl"
    if ev.exists():
        with open(ev, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                row = json.loads(line)
                if row.get("event_type") == "EXPIRY_RESOLVED":
                    p = row["payload"]
                    nm = p.get("near_month_expiry")
                    return ExpiryResolution(
                        session_date=date.fromisoformat(p["session_date"]),
                        expiry_date=date.fromisoformat(p["expiry_date"]),
                        basis_mode=p["basis_mode"], is_expiry_day=p["is_expiry_day"],
                        is_monthly=p["is_monthly"], shifted_from_holiday=False,
                        nominal_weekday=cfg.get("expiry", "weekly_weekday", default="TUE"),
                        near_month_expiry=date.fromisoformat(nm) if nm else None,
                    )
    # Fallback: recompute from the calendar (2.0: shared builder — 1.x crashed here on the
    # boolean `holidays_verified` key, TypeError: 'bool' object is not iterable).
    from vajra.app import build_calendar
    return build_calendar(cfg).resolve(session_date)


def _apply_overrides(cfg, sets: list[str]):
    for item in sets or []:
        key, _, val = item.partition("=")
        node = cfg.raw
        parts = key.split(".")
        for p in parts[:-1]:
            node = node.setdefault(p, {})
        # best-effort typing
        try:
            parsed = json.loads(val)
        except json.JSONDecodeError:
            parsed = val
        node[parts[-1]] = parsed


def replay_session(tape_dir: str | Path, out_root: str | Path, config_path=None,
                   overrides: list[str] | None = None) -> Path:
    tape_dir = Path(tape_dir)
    session_date = date.fromisoformat(tape_dir.name)
    if Path(out_root).resolve() == tape_dir.parent.resolve():
        raise SystemExit("replay: --out must NOT be the recorded tape root (it would overwrite the "
                         "recorded day). Use e.g. --out tapes_replay")
    _out_day = Path(out_root) / session_date.isoformat()
    if _out_day.exists() and any(_out_day.iterdir()):
        raise SystemExit(f"replay: {_out_day} already has files — delete it or pick another --out "
                         "(appending would double-count trades)")
    cfg = load_config(config_path)
    _apply_overrides(cfg, overrides or [])
    res = _read_expiry_from_events(tape_dir, session_date, cfg)

    out_writer = TapeWriter(out_root, session_date, schema_version=cfg.schema_version)
    orch = Orchestrator(cfg, out_writer, res, breeze=None)
    source = ReplaySource(tape_dir)
    try:
        orch.run(source)
    finally:
        out_writer.close()
    return out_writer.partition


def main(argv=None):
    p = argparse.ArgumentParser(description="Replay a recorded VAJRA tick_tape")
    p.add_argument("--tape", required=True, help="recorded partition dir, e.g. tapes/2026-06-16")
    p.add_argument("--out", default="tapes_replay", help="output root dir")
    p.add_argument("--config", default=None)
    p.add_argument("--set", action="append", dest="sets", default=[],
                   help="override config, e.g. --set trigger.pad_floor=0.15")
    args = p.parse_args(argv)
    out = replay_session(args.tape, args.out, args.config, args.sets)
    print(f"Replay tape written to: {out}")


if __name__ == "__main__":
    main()
