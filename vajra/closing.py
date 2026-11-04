"""2.0.4 — NSE Closing Auction Session (CAS): session times and the closing-session NIFTY spot.

WHY (researched 21 Oct 2026; NSE circular 38/2026 SOP, SEBI circular of 15 Oct 2026):
  * From 3 Aug 2026 the F&O stocks stop CONTINUOUS trading at 15:15 and close through an auction
    (15:15-15:20 reference price, 15:20-15:30 order entry with a random close 15:28-15:30, matching to
    ~15:35). Index options and futures keep trading continuously until 15:40.
  * The NIFTY index therefore freezes at 15:15 and from 15:20 Breeze streams the INDICATIVE auction
    close (21 Oct: +435 pts at 15:20, drifting back to +75 by 15:29; 19 Oct: +250). It is not a price
    anybody trades NIFTY options at.
  * On a NON-expiry day NIFTY options and futures ignore it: option put-call parity at three strikes
    stayed within ~1 pt of the futures mid all through 15:15-15:30 (21 Oct tape). So from 15:15 the
    engine's NIFTY spot is  FORWARD - BASIS_REF  where FORWARD is the same future it used all day
    (traded future in ACTUAL mode, ATM put-call parity in IMPLIED mode) and BASIS_REF is the median
    clean basis of the last few minutes before 15:15.
  * On an EXPIRY day the expiring options settle on the AUCTION close (until 7 Dec 2026), so from
    ~15:16 they track the indicative close, not the futures (19 Oct: 22900 CE 15 -> 46 -> 0.1 in ten
    minutes while the future was flat). No spot proxy can make that tradable -> expiry-day overrides.
  * From 7 Dec 2026 (SEBI 15 Oct circular) the auction moves to 15:30-15:40, index F&O trade to
    15:45, and EXPIRING index contracts stop at 15:30 (settled on the 15:00-15:30 VWAP).

Everything here is config-driven (vajra_config.yaml `closing_session:`) and resolved per session date.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import date

# Built-in fallback if the yaml has no schedule (kept identical to the shipped yaml).
_DEFAULT_SCHEDULE = [
    {"from": "2000-01-01", "index_frozen_from": "15:30", "fo_close": "15:30",
     "last_entry": "15:25", "flatten_before": "15:25",
     "expiry_day_last_entry": "15:25", "expiry_day_flatten_before": "15:25"},
    {"from": "2026-08-03", "index_frozen_from": "15:15", "fo_close": "15:40"},
    {"from": "2026-12-07", "index_frozen_from": "15:30", "fo_close": "15:45",
     "expiry_day_fo_close": "15:30"},
]


@dataclass(frozen=True)
class ClosingTimes:
    session_date: str
    rule_from: str                 # which schedule entry applied
    index_frozen_from: str         # NIFTY index is frozen / indicative from here (spot proxy takes over)
    fo_close: str                  # NIFTY options stop trading (expiring ones on an expiry day)
    session_close: str             # engine runs until here (= fo_close)
    last_entry: str                # no NEW entry at/after
    flatten_before: str            # open position squared off at/after
    is_expiry_day: bool
    spot_proxy: bool               # closing-session spot proxy enabled

    def as_dict(self) -> dict:
        return asdict(self)


def _hm(s: str) -> int:
    h, m = str(s).split(":")[:2]
    return int(h) * 60 + int(m)


def _fmt(mins: int) -> str:
    return f"{mins // 60:02d}:{mins % 60:02d}"


def resolve_closing(cfg, session_date: date | str, is_expiry_day: bool) -> ClosingTimes:
    """Pick the schedule entry in force on session_date and apply operator times + clamps."""
    cs = (cfg.get("closing_session", default=None) or {}) if cfg is not None else {}
    sched = cs.get("schedule") or _DEFAULT_SCHEDULE
    d = session_date.isoformat() if isinstance(session_date, date) else str(session_date)
    rows = sorted(sched, key=lambda r: str(r["from"]))
    merged: dict = {}
    rule_from = str(rows[0]["from"])
    for r in rows:                       # the LATEST entry in force applies (entries do not inherit)
        if str(r["from"]) <= d:
            merged = {k: v for k, v in r.items() if v is not None}
            rule_from = str(r["from"])

    def pick(key, default):
        # schedule entry value wins (date-specific market structure), else operator value, else default
        if key in merged:
            return str(merged[key])
        return str(cs.get(key, default))

    frozen = str(merged.get("index_frozen_from", "15:30"))
    fo_close = str(merged.get("fo_close", "15:30"))
    if is_expiry_day:
        fo_close = str(merged.get("expiry_day_fo_close", fo_close))
        last_entry = pick("expiry_day_last_entry", cs.get("last_entry", "15:29"))
        flatten = pick("expiry_day_flatten_before", cs.get("flatten_before", "15:38"))
    else:
        last_entry = pick("last_entry", "15:29")
        flatten = pick("flatten_before", "15:38")
    # Clamps: square off >= 2 min before the F&O close; never enter after the square-off.
    close_m = _hm(fo_close)
    flatten = _fmt(min(_hm(flatten), close_m - 2))
    last_entry = _fmt(min(_hm(last_entry), _hm(flatten)))
    proxy = str(cs.get("spot_proxy", "on")).lower() not in ("off", "false", "0", "no")
    return ClosingTimes(session_date=d, rule_from=rule_from, index_frozen_from=frozen,
                        fo_close=fo_close, session_close=_fmt(max(close_m, _hm(frozen))),
                        last_entry=last_entry, flatten_before=flatten,
                        is_expiry_day=bool(is_expiry_day), spot_proxy=proxy)


def parity_forward(snap, step: float, center: float) -> float | None:
    """Option-implied forward: median of K + CE - PE over the 3 strikes around `center` (LTPs, as the
    engine's IMPLIED basis does all day). None if no strike has both legs."""
    atm = int(round(center / step) * step)
    vals = []
    for k in (atm - int(step), atm, atm + int(step)):
        ce, pe = snap.ce(k), snap.pe(k)
        if ce is None or pe is None or not ce.ltp or not pe.ltp or ce.ltp <= 0 or pe.ltp <= 0:
            continue
        vals.append(k + ce.ltp - pe.ltp)
    if not vals:
        return None
    vals.sort()
    n = len(vals)
    return vals[n // 2] if n % 2 else 0.5 * (vals[n // 2 - 1] + vals[n // 2])


__all__ = ["ClosingTimes", "resolve_closing", "parity_forward"]
