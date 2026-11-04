"""Real recorded-session replay (the data the operator trusts).

Replays a REAL session captured by MYTHOS — exactly as recorded — copied into this
folder (vajra/data/real_session_<day>.jsonl, extracted from MYTHOS's _snap_*.db frames):
real NIFTY spot, real India VIX, real near-month future, the real OI chain, real
sister indices (BANKNIFTY/FINNIFTY) and real heavyweight spots.  NOTHING in the
price path or IV is invented or tuned.

The NIFTY option chain replays the REAL recorded per-strike prices (ltp/bid/ask/oi/vol)
from the MYTHOS frame `opts`; a strike missing from the recording is SKIPPED (a real gap),
NEVER fabricated.

Sisters/stocks: this old MYTHOS tape recorded only their SPOT (+ aggregate OI/PCR), NOT an
option chain — so in REPLAY they are N/A (we do not invent a Black-Scholes chain; operator
rule: never fabricate data).  LIVE (vajra/feed.py LiveBreezeSource) subscribes to their REAL
Breeze option chains, so from the next live session they show on real data with no gap.
"""

from __future__ import annotations

import json
import math
from datetime import datetime, time as dtime
from pathlib import Path

from .marketdata import MarketSnapshot, StrikeQuote
from .mathx import round_to
from .timeutil import IST, ist_datetime

_R = 0.065                                   # carry (so the implied basis breathes, ->0 into expiry)
_T_FLOOR_MIN = 5.0                           # never let time-to-expiry hit zero
# (sister/stock step maps removed — replay is NIFTY-only; live uses cfg.strike_step per instrument)


def _ncdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_price(S, K, T, sigma, right, r=_R) -> float:
    if T <= 1e-9 or sigma <= 1e-9 or S <= 0:
        return max(0.0, S - K) if right == "CE" else max(0.0, K - S)
    vt = sigma * math.sqrt(T)
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / vt
    d2 = d1 - vt
    disc = math.exp(-r * T)
    if right == "CE":
        return S * _ncdf(d1) - K * disc * _ncdf(d2)
    return K * disc * _ncdf(-d2) - S * _ncdf(-d1)


class RealSessionSource:
    def __init__(self, cfg, session_date=None, data_dir: str | Path | None = None):
        self.cfg = cfg
        ddir = Path(data_dir) if data_dir else (Path(__file__).resolve().parent / "data")
        if session_date is None:
            session_date = cfg.get("feed", "real_session_date", default=None)
        if session_date is None:                              # latest available recorded day
            files = sorted(ddir.glob("real_session_*.jsonl"))
            if not files:
                raise FileNotFoundError(f"no recorded sessions in {ddir} (run the extractor)")
            path = files[-1]
        else:
            sd = session_date if isinstance(session_date, str) else session_date.isoformat()
            path = ddir / f"real_session_{sd}.jsonl"
            if not path.exists():
                avail = sorted(p.name[14:24] for p in ddir.glob("real_session_*.jsonl"))
                raise FileNotFoundError(f"no recorded session for {sd}; available: {avail}")
        self.frames = []
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    self.frames.append(json.loads(line))
        # expiry = 15:30 IST on the session date
        sess = ist_datetime(self.frames[0]["ts"]).date()
        self.expiry_ms = int(datetime.combine(sess, dtime(15, 30), tzinfo=IST).timestamp() * 1000)
        self.session_date = sess
        self._last_spot: dict[str, float] = {}

    def _t_years(self, ts_ms: int) -> float:
        secs = max(_T_FLOOR_MIN * 60.0, (self.expiry_ms - ts_ms) / 1000.0)
        return secs / (365.0 * 24.0 * 3600.0)

    def _chain(self, spot, atm, step, n, T, iv, vol, real_oi=None, real_opts=None) -> dict:
        """Build the option chain. For NIFTY (`real_opts` supplied) every strike uses the REAL
        recorded MYTHOS quote [ltp, bid, ask, oi, vol] — NOTHING is invented; a strike missing
        from the recording is SKIPPED (a real gap), never fabricated. Sisters/stocks have no
        recorded chain at all, so they are reconstructed by Black-Scholes off the real spot/IV."""
        is_nifty = real_opts is not None
        strikes: dict[tuple[int, str], StrikeQuote] = {}
        for k in range(-n, n + 1):
            K = int(atm + k * step)
            rec = (real_opts or {}).get(str(K)) or {}
            for side, code in (("c", "CE"), ("p", "PE")):
                r = rec.get(side)
                if r:                               # REAL recorded NIFTY quote
                    ltp, bid, ask, oi, v = r[0], r[1], r[2], int(r[3]), int(r[4])
                elif is_nifty:
                    continue                        # NIFTY strike missing from the tape -> gap, do NOT fabricate
                else:                               # sister/stock: BS reconstruction (no recorded chain)
                    px = bs_price(spot, K, T, iv, code)
                    ltp, bid, ask = round(px, 2), round(px - 0.4, 2), round(px + 0.4, 2)
                    oi = int((real_oi or {}).get(str(K), {}).get(side) or (1500 * (n + 1 - abs(k))))
                    v = int(vol)
                strikes[(K, code)] = StrikeQuote(K, code, round(ltp, 2), round(ltp, 4), "feed",
                                                 v, oi, round(bid, 2), round(ask, 2))
        return strikes

    def _snap(self, inst, spot, step, ts_ms, T, iv, vix, vol, fut=None, real_oi=None,
              real_opts=None) -> MarketSnapshot:
        atm = int(round_to(spot, step))
        n = self.cfg.strikes_each_side
        return MarketSnapshot(ts_ms=ts_ms, instrument=inst, spot=round(spot, 2), vix=vix,
                              future_traded=fut,
                              strikes=self._chain(spot, atm, step, n, T, iv, vol, real_oi, real_opts))

    def prev_close_snapshot(self):
        """(operator rule a) The PREVIOUS recorded day's LAST frame as a NIFTY snapshot
        (real spot + real ITM CE/PE), to seed pre-open levels. None if no prior day exists."""
        ddir = Path(__file__).resolve().parent / "data"
        cur = f"real_session_{self.session_date.isoformat()}.jsonl"
        prevs = [p for p in sorted(ddir.glob("real_session_*.jsonl")) if p.name < cur]
        if not prevs:
            return None
        last = None
        for line in prevs[-1].read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                last = json.loads(line)
        if not last or not last.get("opts"):
            return None
        ts = int(last["ts"])
        iv = float(last.get("iv") or 0.12) or 0.12
        fut = last.get("fut")
        return self._snap("NIFTY", last["spot"], self.cfg.strike_step("NIFTY"), ts,
                          self._t_years(ts), iv, float(last.get("vix") or 0.0), int(last.get("vol") or 0),
                          fut=(fut if fut and fut > 0 else None),
                          real_oi=last.get("oi"), real_opts=last.get("opts"))

    def rounds(self):
        n_step = max(1, int(self.cfg.get("feed", "real_round_stride", default=1)))
        for fi, fr in enumerate(self.frames):
            if fi % n_step != 0:
                continue
            ts = int(fr["ts"])
            T = self._t_years(ts)
            iv = float(fr.get("iv") or 0.12) or 0.12
            vix = float(fr.get("vix") or 0.0)
            vol = int(fr.get("vol") or 0)             # real cumulative NIFTY session volume
            bucket: dict[str, MarketSnapshot] = {}
            # NIFTY — real spot, real OI, real volume, and the REAL near-month future
            # (used by ACTUAL-mode basis in the monthly-expiry week; ignored in IMPLIED).
            self._last_spot["NIFTY"] = fr["spot"]
            fut = fr.get("fut")
            bucket["NIFTY"] = self._snap("NIFTY", fr["spot"], self.cfg.strike_step("NIFTY"),
                                         ts, T, iv, vix, vol, fut=(fut if fut and fut > 0 else None),
                                         real_oi=fr.get("oi"), real_opts=fr.get("opts"))
            # Sisters/stocks: the recorded MYTHOS tape has NO option chain for them, only spot
            # (+ aggregate OI/PCR). We DO NOT fabricate a Black-Scholes chain (operator rule:
            # never invent data). They are therefore N/A in REPLAY of this old tape. LIVE
            # (LiveBreezeSource) subscribes to their REAL Breeze chains, so tomorrow they show
            # on real data with no gap.
            yield ts, bucket


__all__ = ["RealSessionSource", "bs_price"]
