#!/usr/bin/env python3
"""verify_vajra.py — the completion gate (spec §11).

A module is "done" only when these pass:
  1. No-stub scan      — no pass-only bodies / TODO / FIXME / NotImplementedError
                          in any orchestrator-reachable path.
  2. Tape-emission      — replay a synthetic feed; every tape (§9.1-9.7) is
                          produced with the exact columns, dtypes, schema_version,
                          and millisecond timestamps.
  3. Formula golden     — §5.1 & §5.3 worked examples reproduce exactly, plus
                          discount-mode mirrors.
  4. Expiry-mapping     — holiday-Tuesday -> Monday; monthly week -> ACTUAL.
  5. Determinism        — replay.py over a fixed tape yields byte-identical
                          decision_tape across two runs.
  6. Default-on         — no shipped module is reachable only behind a default-off
                          flag (other than LIVE_ORDERS, routing_mode=LEAD, and the
                          decision.thresholds_calibrated status flag — the latter is
                          a calibration STATE marker, not a feature toggle: it only
                          governs whether the THRESHOLDS_PROVISIONAL event fires).

Exit code 0 iff every gate passes.
"""

from __future__ import annotations

import ast
import os
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from vajra import schema as S                                    # noqa: E402
from vajra.mathx import round_to                                 # noqa: E402
from vajra.marketdata import MarketSnapshot, StrikeQuote         # noqa: E402
from vajra.engine.basis_engine import compute_basis             # noqa: E402
from vajra.engine.trigger_engine import compute_band            # noqa: E402
from vajra.expiry import ExpiryCalendar                          # noqa: E402
from vajra.config import load_config                             # noqa: E402

GREEN, RED, DIM, RST = "\033[32m", "\033[31m", "\033[2m", "\033[0m"


def _ok(msg):  print(f"  {GREEN}PASS{RST} {msg}")
def _bad(msg): print(f"  {RED}FAIL{RST} {msg}")


# ---------------------------------------------------------------------------
# Gate 1 — no-stub scan
# ---------------------------------------------------------------------------
SCAN_TARGETS = ["vajra", "replay.py"]
FORBIDDEN_TEXT = ("TODO", "FIXME", "NotImplementedError", "raise NotImplemented")


def _iter_py():
    for t in SCAN_TARGETS:
        p = ROOT / t
        if p.is_dir():
            yield from p.rglob("*.py")
        elif p.exists():
            yield p


def gate_no_stub() -> bool:
    print("Gate 1 — no-stub scan")
    bad = []
    for py in _iter_py():
        src = py.read_text(encoding="utf-8")
        for tok in FORBIDDEN_TEXT:
            if tok in src:
                bad.append(f"{py.relative_to(ROOT)}: contains forbidden token {tok!r}")
        try:
            tree = ast.parse(src)
        except SyntaxError as e:
            bad.append(f"{py.relative_to(ROOT)}: syntax error {e}")
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                body = node.body
                if body and isinstance(body[0], ast.Expr) and isinstance(
                        getattr(body[0], "value", None), ast.Constant) and isinstance(body[0].value.value, str):
                    body = body[1:]  # drop docstring
                if body and all(isinstance(b, ast.Pass) for b in body):
                    bad.append(f"{py.relative_to(ROOT)}:{node.lineno}: pass-only body {node.name!r}")
                if body and all(isinstance(b, ast.Expr) and isinstance(getattr(b, 'value', None), ast.Constant)
                                and b.value.value is Ellipsis for b in body):
                    bad.append(f"{py.relative_to(ROOT)}:{node.lineno}: ellipsis-only body {node.name!r}")
            if isinstance(node, ast.Raise) and node.exc is not None:
                exc = node.exc
                name = None
                if isinstance(exc, ast.Call) and isinstance(exc.func, ast.Name):
                    name = exc.func.id
                elif isinstance(exc, ast.Name):
                    name = exc.id
                if name in ("NotImplementedError", "NotImplemented"):
                    bad.append(f"{py.relative_to(ROOT)}:{node.lineno}: raises {name}")
    if bad:
        for b in bad:
            _bad(b)
        return False
    _ok("no stubs / TODO / FIXME / NotImplementedError in reachable paths")
    return True


# ---------------------------------------------------------------------------
# Gate 3 — formula golden tests
# ---------------------------------------------------------------------------
def _golden_snapshot(spot, ce_atm, pe_atm, vwap_ce_itm, vwap_pe_itm, atm=24100, step=50):
    s = {}
    # ATM quotes drive the implied future
    s[(atm, "CE")] = StrikeQuote(atm, "CE", ce_atm, ce_atm, "feed", 1000, 1000, ce_atm - .5, ce_atm + .5)
    s[(atm, "PE")] = StrikeQuote(atm, "PE", pe_atm, pe_atm, "feed", 1000, 1000, pe_atm - .5, pe_atm + .5)
    # ITM strikes drive the band
    kce, kpe = atm - step, atm + step
    s[(kce, "CE")] = StrikeQuote(kce, "CE", 0, vwap_ce_itm, "feed", 1000, 1000, 0, 0)
    s[(kpe, "PE")] = StrikeQuote(kpe, "PE", 0, vwap_pe_itm, "feed", 1000, 1000, 0, 0)
    return MarketSnapshot(ts_ms=0, instrument="NIFTY", spot=spot, vix=14.0, future_traded=None, strikes=s)


def gate_golden() -> bool:
    print("Gate 3 — formula golden tests")
    ok = True

    # round-half-up trap (the §5.3 precision requirement)
    if round_to(82.5, 5) != 85.0 or round_to(104.5, 5) != 105.0:
        _bad(f"round_to half-up broken: 82.5->{round_to(82.5,5)} 104.5->{round_to(104.5,5)}"); ok = False
    else:
        _ok("round_to is ROUND_HALF_UP (82.5->85, 104.5->105)")

    # §5.1 implied future + basis
    snap = _golden_snapshot(24110, 35, 15, 75, 95)
    br = compute_basis(snap, step=50, basis_mode="IMPLIED")
    if not (br.atm_strike == 24100 and br.future == 24120 and br.basis == 10):
        _bad(f"§5.1 IMPLIED: atm={br.atm_strike} fut={br.future} basis={br.basis} (want 24100/24120/10)"); ok = False
    else:
        _ok("§5.1 IMPLIED future 24120, basis +10")

    # §5.3 band (premium 10)
    band = compute_band(snap, br.atm_strike, br.basis, "IMPLIED", 50,
                        pad_floor=0.10, level_round_step=5, itm_depth_strikes=1)
    if not (band.raw_upper == 24135 and band.upper_level == 24125
            and band.raw_lower == 24045 and band.lower_level == 24035):
        _bad(f"§5.3 premium: raw_up={band.raw_upper} up={band.upper_level} "
             f"raw_lo={band.raw_lower} lo={band.lower_level} (want 24135/24125/24045/24035)"); ok = False
    else:
        _ok("§5.3 premium band: upper 24125, lower 24035")

    # discount mirror (basis = -10): LEVEL = RAW - BASIS adds
    band_d = compute_band(snap, 24100, -10.0, "IMPLIED", 50,
                          pad_floor=0.10, level_round_step=5, itm_depth_strikes=1)
    if not (band_d.upper_level == 24145 and band_d.lower_level == 24055):
        _bad(f"§5.3 discount: up={band_d.upper_level} lo={band_d.lower_level} (want 24145/24055)"); ok = False
    else:
        _ok("§5.3 discount mirror: upper 24145, lower 24055")

    return ok


# ---------------------------------------------------------------------------
# Gate 4 — expiry mapping
# ---------------------------------------------------------------------------
def gate_expiry() -> bool:
    print("Gate 4 — expiry mapping")
    ok = True

    # A weekly Tuesday made a holiday must resolve to Monday.
    # Find a Tuesday that is NOT the last Tuesday of its month (non-monthly).
    cal0 = ExpiryCalendar(holidays=set(), weekly_weekday="TUE")
    tue = date(2026, 9, 1)
    while tue.weekday() != 1:
        tue += timedelta(days=1)            # first Tuesday of Sep 2026
    monthly, _ = cal0.monthly_expiry(2026, 9)
    assert tue != monthly, "pick a non-monthly Tuesday"
    cal = ExpiryCalendar(holidays={tue}, weekly_weekday="TUE")
    monday = tue - timedelta(days=1)
    res = cal.resolve(monday)               # session = the Monday before the holiday Tuesday
    if not (res.expiry_date == monday and res.shifted_from_holiday and res.basis_mode == "IMPLIED"):
        _bad(f"holiday-Tuesday weekly: expiry={res.expiry_date} shifted={res.shifted_from_holiday} "
             f"mode={res.basis_mode} (want {monday}/True/IMPLIED)"); ok = False
    else:
        _ok(f"holiday Tuesday {tue} weekly -> Monday {monday}, IMPLIED")

    # Monthly week: make the last Tuesday a holiday -> monthly expiry shifts to Monday,
    # basis mode ACTUAL on that session.
    m_tue, _ = ExpiryCalendar(holidays=set(), weekly_weekday="TUE").monthly_expiry(2026, 9)
    calm = ExpiryCalendar(holidays={m_tue}, weekly_weekday="TUE")
    m_monthly, m_shift = calm.monthly_expiry(2026, 9)
    res_m = calm.resolve(m_monthly)
    if not (m_shift and m_monthly == m_tue - timedelta(days=1)
            and res_m.is_monthly and res_m.basis_mode == "ACTUAL"):
        _bad(f"monthly holiday: monthly={m_monthly} shift={m_shift} is_monthly={res_m.is_monthly} "
             f"mode={res_m.basis_mode} (want Monday/True/True/ACTUAL)"); ok = False
    else:
        _ok(f"monthly Tuesday {m_tue} holiday -> Monday {m_monthly}, ACTUAL")

    # Non-holiday monthly Tuesday is ACTUAL, plain weekly is IMPLIED.
    calc = ExpiryCalendar(holidays=set(), weekly_weekday="TUE")
    if calc.resolve(m_tue).basis_mode != "ACTUAL":
        _bad("clean monthly Tuesday should be ACTUAL"); ok = False
    else:
        _ok("clean monthly Tuesday -> ACTUAL; weekly -> IMPLIED")
    return ok


# ---------------------------------------------------------------------------
# Gate 2 + 5 — run a synthetic session, verify tapes, verify determinism
# ---------------------------------------------------------------------------
def _run_synthetic(out_root: Path) -> Path:
    from vajra.app import run as app_run
    part = app_run(config_path=str(ROOT / "vajra_config.yaml"), mode="synthetic",
                   with_dashboard=False)
    return Path(part)


def gate_tapes_and_determinism() -> tuple[bool, bool]:
    print("Gate 2 — tape-emission proof  /  Gate 5 — determinism")
    ok2 = True
    ok5 = True
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        # Record a synthetic session into a temp tape root by overriding config root.
        cfg = load_config(str(ROOT / "vajra_config.yaml"))
        cfg.raw["tape"]["root_dir"] = str(tmp / "rec")
        from vajra.app import build_calendar, choose_synthetic_session, write_startup_events
        from vajra.tape import TapeWriter
        from vajra.orchestrator import Orchestrator
        from vajra.synthetic import generate_rounds

        cal = build_calendar(cfg)
        sess = choose_synthetic_session(cal, date(2026, 6, 1))
        res = cal.resolve(sess)
        writer = TapeWriter(tmp / "rec", sess, schema_version=cfg.schema_version)
        write_startup_events(writer, cfg, res, 0)
        orch = Orchestrator(cfg, writer, res, breeze=None)

        class _Static:
            def rounds(self_inner):
                yield from generate_rounds(cfg, sess)
        orch.run(_Static())
        writer.close()
        part = tmp / "rec" / sess.isoformat()

        # --- Gate 2: assert each tape's schema ---
        for name, sch in S.TAPE_SCHEMAS.items():
            f = part / f"{name}.parquet"
            if not f.exists():
                _bad(f"{name}.parquet not produced"); ok2 = False; continue
            t = pq.read_table(f)
            cols = set(t.schema.names)
            need = set(S.required_columns(name))
            missing = need - cols
            extra = cols - need
            if missing:
                _bad(f"{name}: missing columns {missing}"); ok2 = False; continue
            if extra:
                _bad(f"{name}: unexpected extra columns {extra}"); ok2 = False; continue
            # dtype check
            bad_dt = []
            for fld in sch:
                if t.schema.field(fld.name).type != fld.type:
                    bad_dt.append(f"{fld.name}:{t.schema.field(fld.name).type}!={fld.type}")
            if bad_dt:
                _bad(f"{name}: dtype mismatch {bad_dt}"); ok2 = False; continue
            if t.schema.field("ts").type != pa.int64():
                _bad(f"{name}: ts not int64 ms"); ok2 = False; continue
            if t.num_rows == 0:
                _bad(f"{name}: produced but EMPTY (pipeline did not exercise it)"); ok2 = False; continue
            sv = set(t.column("schema_version").to_pylist())
            if sv != {cfg.schema_version}:
                _bad(f"{name}: schema_version {sv} != {cfg.schema_version}"); ok2 = False; continue
            _ok(f"{name}: {t.num_rows} rows, exact columns + dtypes + schema_version + int64 ts")

        # event_tape jsonl
        ev = part / "event_tape.jsonl"
        if not ev.exists():
            _bad("event_tape.jsonl not produced"); ok2 = False
        else:
            import json
            keys_ok = True
            seen_types = set()
            for line in ev.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                row = json.loads(line)
                if set(S.EVENT_TAPE_KEYS) - set(row.keys()):
                    keys_ok = False
                seen_types.add(row.get("event_type"))
            need_ev = {"STARTUP_CONFIG", "EXPIRY_RESOLVED"}
            undeclared = {t for t in seen_types if t not in S.EVENT_TYPES}
            if not keys_ok:
                _bad("event_tape: missing required keys"); ok2 = False
            elif need_ev - seen_types:
                _bad(f"event_tape: missing event types {need_ev - seen_types}"); ok2 = False
            elif undeclared:
                _bad(f"event_tape: UNDECLARED event types {undeclared} (not in schema.EVENT_TYPES)"); ok2 = False
            else:
                _ok(f"event_tape.jsonl: keys ok, all types declared, types {sorted(seen_types)}")

        # --- Gate 5: determinism via replay.py twice ---
        from replay import replay_session
        r1 = replay_session(part, tmp / "rep1", config_path=str(ROOT / "vajra_config.yaml"))
        r2 = replay_session(part, tmp / "rep2", config_path=str(ROOT / "vajra_config.yaml"))
        d1 = (r1 / "decision_tape.jsonl").read_bytes()
        d2 = (r2 / "decision_tape.jsonl").read_bytes()
        if d1 != d2:
            _bad("decision_tape differs across two replays (non-deterministic)"); ok5 = False
        elif len(d1) == 0:
            _bad("decision_tape empty on replay"); ok5 = False
        else:
            _ok(f"replay decision_tape byte-identical across 2 runs ({len(d1)} bytes)")

    return ok2, ok5


# ---------------------------------------------------------------------------
# Gate 6 — default-on check
# ---------------------------------------------------------------------------
# These false booleans are VERIFICATION-STATE markers / opt-in gates, NOT disabled features:
#   LIVE_ORDERS        — paper-always; turning it on routes real ICICI orders (must stay off).
#   thresholds_calibrated — flips true once the operator calibrates bias thresholds on tape.
#   holidays_verified  — flips true once the operator confirms the NSE holiday calendar (same kind).
ALLOWED_FALSE = {("execution", "LIVE_ORDERS"), ("decision", "thresholds_calibrated"),
                 ("expiry", "holidays_verified")}


def gate_default_on() -> bool:
    print("Gate 6 — default-on check")
    cfg = load_config(str(ROOT / "vajra_config.yaml"))
    bad = []

    def walk(node, path):
        if isinstance(node, dict):
            for k, v in node.items():
                walk(v, path + (k,))
        elif node is False:
            leaf = (path[-2], path[-1]) if len(path) >= 2 else (path[-1],)
            if leaf not in ALLOWED_FALSE:
                bad.append(".".join(path))

    walk(cfg.raw, ())
    # routing default must not be LEAD (LEAD is opt-in)
    if cfg.routing_mode == "LEAD":
        bad.append("routing.routing_mode defaults to LEAD (must be opt-in)")
    _lo = (cfg.raw.get("execution") or {}).get("LIVE_ORDERS")
    if _lo is not False or cfg.live_orders:      # paper-only: must be the YAML boolean false (T23)
        _bad(f"execution.LIVE_ORDERS must be the boolean false (paper only); found {_lo!r}")
        return False
    if bad:
        for b in bad:
            _bad(f"feature disabled by default: {b}")
        return False
    _ok("no module disabled behind a default-off flag (LIVE_ORDERS / LEAD excepted)")
    return True


# ---------------------------------------------------------------------------
# Gate 7 (VAJRA 2.0) — non-expiry behaviour on REAL recorded data
# ---------------------------------------------------------------------------
def gate_nonexpiry_real() -> bool:
    """Replay the bundled 2 MB real fixture (2026-10-15, a non-expiry trend day): VAJRA 2.0 must trade
    the morning (narrowed band) and, from 15:15 (NSE closing auction), price NIFTY from the forward
    instead of the frozen / indicative index. (1.x: one morning trade + one trade on the
    garbage 15:20 tick.)"""
    print("Gate 7 — non-expiry behaviour on real data (2026-10-15 fixture)")
    from datetime import datetime, timezone, timedelta
    from vajra.app import build_calendar
    from vajra.orchestrator import Orchestrator
    from vajra.feed import ReplaySource
    fx = ROOT / "tests" / "fixtures" / "2026-10-15"
    if not (fx / "tick_tape.parquet").exists():
        _bad("fixture tests/fixtures/2026-10-15/tick_tape.parquet missing"); return False

    class _W:
        def __init__(self): self.rows, self.events = {}, []
        def append(self, n, r): self.rows.setdefault(n, []).append(r)
        def event(self, t, p=None, ts_ms=None): self.events.append((t, p, ts_ms))
        def flush(self): pass
        def close(self): pass
    cfg = load_config(str(ROOT / "vajra_config.yaml"))
    res = build_calendar(cfg).resolve(date(2026, 10, 15))
    w = _W()
    o = Orchestrator(cfg, w, res)
    o.run(ReplaySource(fx))
    ist = timezone(timedelta(hours=5, minutes=30))
    bad_from = int(datetime(2026, 10, 15, 15, 15, tzinfo=ist).timestamp() * 1000)
    entries = [r for r in w.rows.get("order_tape", []) if r["action"] == "ENTRY"]
    ok = True
    if o.regime.name != "NON_EXPIRY":
        _bad(f"regime {o.regime.name} (want NON_EXPIRY)"); ok = False
    if len(entries) < 1:
        _bad("no entries on the non-expiry trend morning (the idle-day bug)"); ok = False
    else:
        _ok(f"{len(entries)} non-expiry entries on the trend morning ({o.regime.band_method} band, all-in)")
    # 2.0.4: from 15:15 the frozen / indicative index is replaced by forward - basis_ref (NSE closing
    # auction). The engine's spot must track the forward, never the indicative close, and stay healthy.
    late = [r for r in w.rows.get("basis_tape", []) if r["instrument"] == "NIFTY" and r["ts"] >= bad_from]
    if not late or not all(r.get("spot_source") == "CLOSING_PROXY" and abs(r["spot"] - r["future"]) < 5
                           for r in late):
        _bad("15:15+ NIFTY spot does not track the forward (closing-session proxy)"); ok = False
    else:
        _ok(f"15:15+ NIFTY spot = forward - basis_ref ({len(late)} rounds; indicative index ignored, "
            f"max gap {max(r['index_spot'] - r['spot'] for r in late):.0f} pts)")
    if any(t == "FEED_GAP" and (p or {}).get("stage") == "feed_sanity" and ts and ts >= bad_from
           for t, p, ts in w.events):
        _bad("feed-sanity gate tripped in the closing session despite the proxy"); ok = False
    else:
        _ok("closing session: feed stays healthy on the proxy spot")
    return ok


def main() -> int:
    if sys.platform == "win32":                     # 2.0: make ANSI colours work on Windows consoles
        os.system("")
    print(f"{DIM}VAJRA 2.0 completion gate — verify_vajra.py{RST}\n")
    results = {}
    results["1 no-stub"] = gate_no_stub(); print()
    results["3 golden"] = gate_golden(); print()
    results["4 expiry"] = gate_expiry(); print()
    ok2, ok5 = gate_tapes_and_determinism(); print()
    results["2 tapes"] = ok2
    results["5 determinism"] = ok5
    results["6 default-on"] = gate_default_on(); print()
    results["7 non-expiry real"] = gate_nonexpiry_real(); print()

    allok = all(results.values())
    print("Summary:")
    for k in sorted(results):
        tag = f"{GREEN}PASS{RST}" if results[k] else f"{RED}FAIL{RST}"
        print(f"  [{tag}] gate {k}")
    print()
    print(f"{(GREEN+'ALL GATES PASS') if allok else (RED+'GATE FAILURE')}{RST}")
    return 0 if allok else 1


if __name__ == "__main__":
    raise SystemExit(main())
