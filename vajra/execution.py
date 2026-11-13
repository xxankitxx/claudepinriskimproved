"""§7 Execution Layer — NIFTY options only, full-capital sizing, trailing-stop exit.

Mirrors MYTHOS's verified live mechanics (confirmed from its recorded trade tape):
  * Lot size 65; starting capital Rs 1,00,000 (resets each day).
  * Every entry deploys ALL capital in hand: lots = floor(capital / (premium*65)).
  * Buy CE for an up-explosion, PE for a down-explosion.
  * Exit = a TRAILING STOP that books a MINIMUM profit then ratchets up to capture
    the maximum (TRAIL SL), plus a hard stop (SL HIT) and the expiry-close flatten.
  * Full order placement is built over the broker wrapper; routed to PAPER while
    LIVE_ORDERS=False (§0.6) — the engine always writes its trades to the tape and
    the human places the real order on ICICI.

Every action -> order_tape rows (returned to the orchestrator to append).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from .breeze import OrderPlacementError
from .marketdata import MarketSnapshot
from .timeutil import hhmm_reached

# exit reason codes
SL_HIT = "SL_HIT"                      # hard stop (premium fell hard_sl_pct below entry)
TRAIL_SL = "TRAIL_SL"                  # trailing stop (books >= min profit, then the max)
BE_STOP = "BE_STOP"                    # 2.2 protective stop (optional): was +arm%, came back to +lock%
EXPIRY_FLATTEN = "EXPIRY_FLATTEN"
END_OF_SESSION = "END_OF_SESSION"


@dataclass
class Position:
    direction: str                 # UP | DOWN
    right: str                     # CE | PE
    strike: int
    entry_ts: int
    entry_fill: float
    entry_spot: float
    lots: int
    lot_size: int
    qty_total: int
    qty_remaining: int
    cost: float                    # capital deployed at entry (qty * entry_fill)
    peak_premium: float = 0.0      # high-water mark of the option premium
    trough_premium: float = 0.0    # low-water mark (2.2: the journal shows how far a trade went each way)
    trail_floor: float = 0.0       # current trailing-stop floor (0 = not yet armed)
    trail_armed: bool = False
    be_armed: bool = False         # 2.2: the protective stop is live (peak reached breakeven_arm_pct)
    last_premium: float = 0.0      # most recent option premium (for live unrealized P&L)
    realized_pnl: float = 0.0
    order_ids: list[str] = field(default_factory=list)
    quote_sig: tuple = ()           # (ltp, bid, ask) last seen -- to detect a FROZEN quote (T15)
    quote_change_ts: int = 0        # when that quote last changed

    @property
    def open(self) -> bool:
        return self.qty_remaining > 0


def _px(v):
    """A usable price: finite and > 0 (NaN / inf / 0 / None -> None). 2.0: a NaN ask used to pass
    the premium floor and size 30,769 lots at a NaN fill (teardown T07)."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) and f > 0 else None


class OrderWrapper:
    """Paper + live order placement.  Live path uses the self-contained Breeze
    wrapper; reached only when live_orders is True (default False)."""

    def __init__(self, live_orders: bool, breeze=None):
        self.live_orders = bool(live_orders)
        self.breeze = breeze
        self._paper_seq = 0

    def place(self, action, side, strike, right, qty, snapshot, expiry_iso=None):
        q = snapshot.quote(strike, right)
        ltp = _px(q.ltp) if q else None
        side_px = (_px(q.ask) if side == "BUY" else _px(q.bid)) if q else None
        if side_px is not None and ltp is not None and not (0.5 * ltp <= side_px <= 1.5 * ltp):
            side_px = None          # 2.0.1 (T29): a garbage bid/ask (0.05 vs LTP 120) must not set the fill
        fill = side_px if side_px is not None else ltp
        if fill is None:            # no finite, positive price at all -> no fill (caller rejects/retries)
            return None, 0.0, 0.0, 0.0
        intended = ltp if ltp is not None else fill
        slippage = fill - intended
        if self.live_orders and self.breeze is not None:
            try:
                order_id = self.breeze.place_option_order(
                    side=side, strike=strike, right=right, qty=qty, expiry_iso=expiry_iso)
            except OrderPlacementError:
                # Broker rejected/failed — return order_id=None so the caller does NOT
                # build a Position from a fill that never happened (phantom-fill guard).
                return None, float(intended), float(fill), float(slippage)
            return str(order_id), float(intended), float(fill), float(slippage)
        self._paper_seq += 1
        return f"PAPER-{self._paper_seq}", float(intended), float(fill), float(slippage)


def _row(ts, action, strike, right, qty, intended, fill, slippage, order_id,
         live_flag, exit_reason, pnl) -> dict:
    return {
        "ts": int(ts), "action": action, "instrument": "NIFTY", "strike": int(strike),
        "right": right, "qty": int(qty), "intended_price": float(intended),
        "fill_price": float(fill), "slippage": float(slippage), "order_id": order_id,
        "live_orders_flag": bool(live_flag), "exit_reason": exit_reason or "",
        "pnl_realized": float(pnl),
    }


class Executor:
    def __init__(self, cfg, breeze=None):
        self.cfg = cfg
        ex = cfg.get("execution", default={}) or {}
        from .config import live_orders_enabled
        self.live_orders = live_orders_enabled(ex)         # strict: never True from a typo (T23)
        self.lot_size = int(ex.get("lot_size", 65))
        self.starting_capital = float(ex.get("starting_capital", 100_000))
        self.capital = self.starting_capital
        self.deploy_full = bool(ex.get("deploy_full_capital", True))
        self.min_profit_pct = float(ex.get("min_profit_pct", 0.07))
        self.trail_activate_pct = float(ex.get("trail_activate_pct", 0.10))
        # Adaptive give-back (operator rule): a flat give-back the INSTANT the trail arms was
        # stopping trades out in 7-8 seconds on the first wobble right after a pin-risk breach turned
        # profitable — exactly when that trade may be the START of a bigger move, not the end. So the
        # give-back now WIDENS right after arming and TIGHTENS only once the move is clearly extended.
        # Three tiers by peak profit: [activate, extend_mid) -> wide; [extend_mid, extend_tight) -> mid;
        # >= extend_tight -> tight (the final/tightest tier — same number as the old flat give-back).
        self.trail_give_pct_wide = float(ex.get("trail_give_pct_wide", 0.12))
        self.trail_give_pct_mid = float(ex.get("trail_give_pct_mid", 0.09))
        self.trail_give_pct = float(ex.get("trail_give_pct", 0.06))          # tight/final tier
        self.trail_extend_mid_pct = float(ex.get("trail_extend_mid_pct", 0.20))
        self.trail_extend_tight_pct = float(ex.get("trail_extend_tight_pct", 0.35))
        self.hard_sl_pct = float(ex.get("hard_sl_pct", 0.12))
        # 2.2 protective stop (OFF unless breakeven_arm_pct > 0; the operator rule was "no breakeven").
        # 15-day tape study: 41% of the stopped trades had been +5% or better first. Once the peak
        # reaches entry*(1+arm) and the trail is not armed yet, the stop moves from -hard_sl to
        # entry*(1+lock). The trail (+10% arm, +7% floor) is unchanged and takes over when it arms.
        self.breakeven_arm_pct = float(ex.get("breakeven_arm_pct", 0) or 0)
        self.breakeven_lock_pct = float(ex.get("breakeven_lock_pct", 0.01) or 0)
        # (no upper premium ceiling and no upper lot cap — operator rule: deploy ALL remaining
        # capital on EVERY trade regardless of how rich the option is.)
        # Minimum entry premium (operator rule): skip an entry whose option is too THIN.
        # Below this premium we do NOT trade — different floor on expiry vs non-expiry day.
        self.min_premium_nonexpiry = float(ex.get("min_premium_nonexpiry", 45))
        self.min_premium_expiry = float(ex.get("min_premium_expiry", 30))
        self.base_lots = int(ex.get("default_qty_lots", 1))
        self.strikes_mode = str(ex.get("strikes", "ATM_or_1ITM"))
        self.flatten_before = str(ex.get("flatten_before", "15:25"))
        self.caution_after = str(ex.get("caution_after", "14:30"))
        self.wrapper = OrderWrapper(self.live_orders, breeze=breeze)
        self.last_exit_quote_age_ms = 0
        self.position: Position | None = None

    # ---- strike + sizing ---------------------------------------------------
    def select_strike(self, direction: str, atm_strike: int, step: float) -> tuple[int, str]:
        right = "CE" if direction == "UP" else "PE"
        if self.strikes_mode == "1ITM":
            strike = atm_strike - int(step) if direction == "UP" else atm_strike + int(step)
        else:
            strike = atm_strike
        return int(strike), right

    def _nearest_itm_above_floor(self, direction, atm_strike, step, snapshot,
                                 min_premium, max_steps=None):
        """Walk IN from the ATM toward the money (CE -> lower strikes, PE -> higher strikes) and
        return the FIRST (strike, premium) whose premium clears the floor and isn't too rich.
        Used on expiry day when the ATM premium is below the floor (operator rule). The walk is
        capped at the subscribed chain depth (strikes_each_side) so it never queries an
        unsubscribed strike."""
        if max_steps is None:
            max_steps = int(self.cfg.strikes_each_side)
        right = "CE" if direction == "UP" else "PE"
        sgn = -1 if direction == "UP" else 1                # ITM side: CE below spot, PE above
        for i in range(1, max_steps + 1):
            k = int(atm_strike + sgn * i * int(step))
            q = snapshot.quote(k, right)
            if q is None:
                continue
            prem = _px(q.ask) or _px(q.ltp)
            if prem is not None and prem >= min_premium:                          # nearest ITM clearing the floor (no upper cap)
                return k, prem
        return None

    def compute_lots(self, entry_premium: float) -> int:
        """Go ALL IN: deploy ALL remaining capital every trade. lots = floor(capital/(premium*65)),
        NO upper cap and NO forced minimum (operator rule). If the remaining capital cannot
        cover even one lot, lots = 0 and we do not trade — never force a lot we can't afford."""
        prem = _px(entry_premium)
        if prem is None or not math.isfinite(self.capital):
            return 0
        prem = max(0.05, prem)
        if not self.deploy_full:
            return self.base_lots
        return max(0, int(self.capital // (prem * self.lot_size)))     # ALL of capital (operator-locked)

    def caution_active(self, ts_ms: int) -> bool:
        return hhmm_reached(ts_ms, self.caution_after)

    # ---- entry --------------------------------------------------------------
    def enter(self, ts_ms, direction, snapshot, band, conviction, threshold, channels,
              is_expiry_day, expiry_iso, step, atm_strike=None, strike_override=None) -> list[dict]:
        if self.position is not None and self.position.open:
            return []
        # 2.0: strike from the CURRENT ATM (1.x used the held band's anchor-time ATM, which on
        # 10-15 10:00 bought 22750 CE with spot at 22855 — 105 pts ITM, not "ATM").
        atm = int(atm_strike) if atm_strike is not None else band.atm_strike
        strike, right = self.select_strike(direction, atm, step)
        if strike_override is not None:             # 2.4: the gamma-ranked strike chosen by the orchestrator
            strike = int(strike_override)
        q = snapshot.quote(strike, right)
        if q is None:
            return [_row(ts_ms, "REJECT", strike, right, 0, 0, 0, 0, "", self.live_orders, "NO_QUOTE", 0.0)]
        entry_prem = _px(q.ask) or _px(q.ltp)
        if entry_prem is None:      # NaN / zero quote -> never size off it
            return [_row(ts_ms, "REJECT", strike, right, 0, 0, 0, 0, "", self.live_orders, "NO_QUOTE", 0.0)]
        min_premium = self.min_premium_expiry if is_expiry_day else self.min_premium_nonexpiry
        # Expiry-day ITM fallback (operator rule): if the ATM premium is below the floor, step IN
        # to the NEAREST ITM strike whose premium clears the floor (deeper ITM carries more
        # premium) instead of skipping the trade. Non-expiry days are unchanged.
        if is_expiry_day and entry_prem < min_premium:
            alt = self._nearest_itm_above_floor(direction, strike, step, snapshot, min_premium)
            if alt is not None:
                strike, entry_prem = alt
        if entry_prem < min_premium:
            return [_row(ts_ms, "REJECT", strike, right, 0, 0, 0, 0, "", self.live_orders, "PREMIUM_TOO_THIN", 0.0)]
        lots = self.compute_lots(entry_prem)
        if lots <= 0:               # remaining capital can't cover even one lot -> out of capital
            return [_row(ts_ms, "REJECT", strike, right, 0, 0, 0, 0, "", self.live_orders, "INSUFFICIENT_CAPITAL", 0.0)]
        qty = lots * self.lot_size
        order_id, intended, fill, slip = self.wrapper.place("ENTRY", "BUY", strike, right, qty, snapshot, expiry_iso)
        if order_id is None:        # live broker order failed -> reject; never record a phantom fill
            return [_row(ts_ms, "REJECT", strike, right, 0, 0, 0, 0, "", self.live_orders,
                         "ORDER_PLACEMENT_FAILED", 0.0)]
        self.position = Position(
            direction=direction, right=right, strike=strike, entry_ts=ts_ms, entry_fill=fill,
            entry_spot=snapshot.spot, lots=lots, lot_size=self.lot_size, qty_total=qty,
            qty_remaining=qty, cost=qty * fill,
            peak_premium=fill, trough_premium=fill, last_premium=fill, order_ids=[order_id])
        return [_row(ts_ms, "ENTRY", strike, right, qty, intended, fill, slip, order_id,
                     self.live_orders, "", 0.0)]

    # ---- management: trailing stop + hard stop (NO breakeven — operator rule) ----------
    def manage(self, ts_ms, snapshot) -> list[dict]:
        pos = self.position
        if pos is None or not pos.open:
            return []
        q = snapshot.quote(pos.strike, pos.right)
        if q is not None:
            sig = (q.ltp, q.bid, q.ask)
            if sig != pos.quote_sig:
                pos.quote_sig, pos.quote_change_ts = sig, ts_ms
        if q is None or q.ltp is None or not math.isfinite(q.ltp) or q.ltp <= 0:
            return []                              # missing/zero/NaN tick -> HOLD; NEVER fabricate a premium.
            #                                        Checked BEFORE the 15:25 flatten so a feed gap at the
            #                                        close HOLDs/retries instead of booking a 0.0 EXIT.
        # Flatten before the close (uses _close which re-quotes the real fill) — only with a live quote.
        if hhmm_reached(ts_ms, self.flatten_before):
            return [self._close(ts_ms, snapshot, EXPIRY_FLATTEN)]
        prem = q.ltp
        pos.peak_premium = max(pos.peak_premium, prem)
        pos.trough_premium = prem if pos.trough_premium <= 0 else min(pos.trough_premium, prem)
        pos.last_premium = prem

        # 2) Trailing stop (PROFIT exit) — arms once the peak reaches entry*(1+activate) (+10%,
        #    operator rule); the floor secures the >=7% minimum and then ratchets up under the
        #    running peak to book the maximum. Once armed, this is the only profit exit.
        #    Give-back is ADAPTIVE by how far the peak has extended (operator rule): wide right after
        #    arming so a fresh breach-driven move can breathe past its first wobble, tightening only
        #    once it is a clearly proven, extended move. peak_pct only ever grows (peak_premium never
        #    falls), so the tier only ever tightens over a position's life, never loosens back up.
        # (1e-9 tolerance: an exact +10% peak must arm — 110.00000000000001 > 110.0 in float, T07)
        if pos.peak_premium >= pos.entry_fill * (1.0 + self.trail_activate_pct) - 1e-9:
            pos.trail_armed = True
        if pos.trail_armed:
            peak_pct = (pos.peak_premium / pos.entry_fill) - 1.0
            if peak_pct >= self.trail_extend_tight_pct:
                give = self.trail_give_pct              # proven big move -> lock in hard (tight/final)
            elif peak_pct >= self.trail_extend_mid_pct:
                give = self.trail_give_pct_mid           # tapering
            else:
                give = self.trail_give_pct_wide          # just armed -> let it breathe
            floor = max(pos.entry_fill * (1.0 + self.min_profit_pct),
                        pos.peak_premium * (1.0 - give))
            pos.trail_floor = floor
            if prem <= floor:
                return [self._close(ts_ms, snapshot, TRAIL_SL)]
            return []                              # in profit, trailing — HOLD to book the max

        # --- Below +10% (trail not armed): the hard stop, and the optional 2.2 protective stop. ---
        if self.breakeven_arm_pct > 0:
            if pos.peak_premium >= pos.entry_fill * (1.0 + self.breakeven_arm_pct) - 1e-9:
                pos.be_armed = True
            if pos.be_armed and prem <= pos.entry_fill * (1.0 + self.breakeven_lock_pct) + 1e-9:
                return [self._close(ts_ms, snapshot, BE_STOP)]
        if prem <= pos.entry_fill * (1.0 - self.hard_sl_pct) + 1e-9:
            return [self._close(ts_ms, snapshot, SL_HIT)]
        return []

    # ---- order helpers ------------------------------------------------------
    def _close(self, ts_ms, snapshot, reason) -> dict:
        pos = self.position
        self.last_exit_quote_age_ms = (ts_ms - pos.quote_change_ts) if pos.quote_change_ts else 0
        qty = pos.qty_remaining
        order_id, intended, fill, slip = self.wrapper.place("EXIT", "SELL", pos.strike, pos.right, qty, snapshot)
        if order_id is None:        # live broker rejected the EXIT -> do NOT book phantom P&L and
            # do NOT mark flat; leave the position OPEN so manage() retries the exit next round.
            return _row(ts_ms, "REJECT", pos.strike, pos.right, 0, 0, 0, 0, "",
                        self.live_orders, "ORDER_PLACEMENT_FAILED", 0.0)
        pnl = round((fill - pos.entry_fill) * qty, 2)      # rupees to the paisa (no -2180.750000000008)
        pos.realized_pnl += pnl
        pos.qty_remaining -= qty
        pos.order_ids.append(order_id)
        self.capital += pnl                        # equity updates with the booked P&L
        return _row(ts_ms, "EXIT", pos.strike, pos.right, qty, intended, fill, slip,
                    order_id, self.live_orders, reason, pnl)

    def force_flatten(self, ts_ms, snapshot, reason=END_OF_SESSION) -> list[dict]:
        if self.position is None or not self.position.open:
            return []
        pos = self.position
        q = snapshot.quote(pos.strike, pos.right)
        if q is None or q.ltp is None or not math.isfinite(q.ltp) or q.ltp <= 0:
            # No live exit quote at the close (feed gap) -> do NOT fabricate a 0.0 EXIT (that books a
            # phantom total loss into the binding tape). Leave the position OPEN + flag it; the operator
            # flattens manually on ICICI. (council: force_flatten could book a phantom 0.0 on a 15:30 gap.)
            return [_row(ts_ms, "REJECT", pos.strike, pos.right, 0, 0, 0, 0, "",
                         self.live_orders, "NO_QUOTE_AT_FLATTEN", 0.0)]
        return [self._close(ts_ms, snapshot, reason)]

    @property
    def position_active(self) -> bool:
        return self.position is not None and self.position.open


__all__ = ["Executor", "OrderWrapper", "Position", "SL_HIT", "TRAIL_SL", "BE_STOP",
           "EXPIRY_FLATTEN", "END_OF_SESSION"]
