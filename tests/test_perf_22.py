"""2.2 round-loop speed-ups must not change a single number.

The profile of a 24-instrument round showed 22% in re-sorting strikes for the max-OI pin, 6% in
Decimal rounding and ~4% in datetime construction. Each replacement is checked here against the
original implementation on a wide random sample (and on the ties that motivated HALF-UP)."""
import math
import random
from datetime import datetime, timezone, timedelta
from decimal import Decimal, ROUND_HALF_UP

from vajra.engine.pin_locator import locate_pin
from vajra.marketdata import MarketSnapshot, StrikeQuote
from vajra.mathx import round_to
from vajra.timeutil import ist_seconds_into_day, parse_hhmm, hhmm_reached


def _ref_round_to(value, step):
    q = (Decimal(str(value)) / Decimal(str(step))).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return float(q * Decimal(str(step)))


def test_round_to_fast_path_equals_the_decimal_reference():
    rnd = random.Random(22)
    assert round_to(82.5, 5) == 85.0 and round_to(104.5, 5) == 105.0 and round_to(24110, 50) == 24100.0
    for step in (1, 5, 10, 20, 50, 100, 0.05, 0.5, 2.5):
        for _ in range(4000):
            v = rnd.choice([rnd.uniform(-500, 60000), round(rnd.uniform(0, 30000), 2),
                            rnd.randint(0, 60000) + 0.5 * step, rnd.randint(0, 4000) * step / 2])
            assert round_to(v, step) == _ref_round_to(v, step), (v, step)
    for v in (2.5, 7.5, 12.5, 24125.0, 24175.0, -2.5, 0.0, 1e9 + 2.5):
        assert round_to(v, 5) == _ref_round_to(v, 5)


def test_round_to_still_rejects_a_bad_step():
    import pytest
    with pytest.raises(ValueError):
        round_to(10.0, 0)


def _ref_pin(snapshot):
    def _max_oi(right):
        quotes = sorted((q for (s, r), q in snapshot.strikes.items() if r == right), key=lambda q: q.strike)
        if not quotes:
            return None
        return max(quotes, key=lambda q: (q.oi, -abs(q.strike - snapshot.spot), -q.strike)).strike
    return _max_oi("CE"), _max_oi("PE")


def test_one_pass_pin_locator_equals_the_sorted_reference_including_ties():
    rnd = random.Random(9)
    for _ in range(600):
        atm = rnd.randrange(22000, 26000, 50)
        strikes = {}
        for i in range(-rnd.randint(0, 8), rnd.randint(1, 9)):
            for right in ("CE", "PE"):
                if rnd.random() < 0.9:
                    oi = rnd.choice([0, 0, 1000, 1000, 5000, rnd.randint(0, 9000)])   # many ties
                    k = atm + 50 * i
                    strikes[(k, right)] = StrikeQuote(k, right, 10.0, 10.0, "feed", 1, oi, 9.9, 10.1)
        snap = MarketSnapshot(0, "NIFTY", atm + rnd.uniform(-40, 40), 14.0, None, strikes)
        got = locate_pin(snap, snap.spot + 5.0, 50)
        assert (got.max_oi_ce, got.max_oi_pe) == _ref_pin(snap)
        if got.max_oi_ce is not None:
            assert got.dist_spot_to_max_oi_ce == abs(snap.spot - got.max_oi_ce)
    empty = MarketSnapshot(0, "NIFTY", 24000.0, 14.0, None, {})
    r = locate_pin(empty, 24000.0, 50)
    assert r.max_oi_ce is None and r.max_oi_pe is None and r.dist_spot_to_max_oi_pe is None


def test_ist_clock_arithmetic_equals_datetime():
    ist = timezone(timedelta(hours=5, minutes=30))
    rnd = random.Random(3)
    base = int(datetime(2026, 11, 9, 0, 0, tzinfo=ist).timestamp() * 1000)
    for _ in range(5000):
        ts = base + rnd.randint(-3 * 86_400_000, 3 * 86_400_000)
        d = datetime.fromtimestamp(ts / 1000.0, tz=ist)
        assert ist_seconds_into_day(ts) == d.hour * 3600 + d.minute * 60 + d.second
    nine15 = base + (9 * 3600 + 15 * 60) * 1000
    assert not hhmm_reached(nine15 - 1, "09:15") and hhmm_reached(nine15, "09:15")
    assert parse_hhmm("15:38") == 15 * 3600 + 38 * 60 == parse_hhmm("15:38")     # cached second call


def test_floor_semantics_hold_for_fractional_quotients():
    for v in (24101.0, 24124.999, 24125.001, 24149.0):
        assert round_to(v, 50) == _ref_round_to(v, 50)
        assert math.isfinite(round_to(v, 50))


def test_23_sorted_strikes_volume_sum_and_lazy_std_equal_the_old_code():
    from operator import attrgetter
    from vajra.engine.basis_monitor import BasisMonitor
    rnd = random.Random(31)
    for _ in range(200):
        strikes = {}
        for _i in range(rnd.randint(0, 40)):
            k, r = rnd.randrange(24000, 26000, 50), rnd.choice(["CE", "PE"])
            strikes[(k, r)] = StrikeQuote(k, r, 10.0, 10.0, "feed", rnd.randint(0, 9999), 1, 9.9, 10.1)
        snap = MarketSnapshot(0, "NIFTY", 25000.0, 14.0, None, strikes)
        assert snap.all_strikes_sorted() == sorted(strikes.values(), key=lambda q: (q.strike, q.right))
        assert sum(map(attrgetter("volume"), strikes.values())) == sum(q.volume for q in strikes.values())

    class _Ref(BasisMonitor):                       # the 2.2 update(): std computed on every call
        def update(self, ts_ms, basis):
            from vajra.engine.basis_monitor import BasisMonitorResult
            from vajra.timeutil import basis_signal_weight
            weight = basis_signal_weight(ts_ms, self.full_until, self.near_zero_after)
            if self._last_basis is None:
                self._last_basis = float(basis)
                ewma_v = self._ewma.update(ts_ms / 1000.0, 0.0)
                self._std.push(0.0)
                return BasisMonitorResult(0.0, ewma_v, False, 0, weight)
            d_basis = float(basis) - self._last_basis
            self._last_basis = float(basis)
            d_eff = 0.0 if abs(d_basis) < self.noise_floor_pts else d_basis
            ewma_v = self._ewma.update(ts_ms / 1000.0, d_eff)
            self._std.push(d_eff)
            std = self._std.std()
            abrupt, abrupt_dir = False, 0
            if std > 0 and abs(d_eff) >= self.noise_floor_pts:
                if abs(d_eff - ewma_v) > self.abrupt_k * std:
                    abrupt, abrupt_dir = True, (1 if d_eff > ewma_v else -1)
            return BasisMonitorResult(d_basis, ewma_v, abrupt, abrupt_dir, weight)

    a, b = BasisMonitor(), _Ref()
    basis, ts, flags = 10.0, 1_794_370_000_000, 0
    for i in range(20000):
        basis += rnd.choice([0, 0, 0, 0.3, -0.4, 1.5, -2.5, 6.0, -7.0])
        ts += 250
        ra, rb = a.update(ts, basis), b.update(ts, basis)
        assert ra == rb
        flags += ra.abrupt_flag
    assert flags > 20                                # the comparison did exercise the abrupt branch
