"""§4 Data layer — vajra_feed: a thin feed over the self-contained Breeze wrapper.

Produces evaluation *rounds*: ``(round_ts_ms, {instrument: MarketSnapshot})``.
All instruments in one round share the round timestamp so the tape replays
deterministically (one decision per round).

Three sources (config ``feed.mode``):
  * ``live``      — LiveBreezeSource over BreezeWrapper (real websocket).
  * ``replay``    — ReplaySource over a recorded tick_tape (THE binding artifact;
                    used by replay.py / determinism gate).
  * ``synthetic`` — SyntheticSource (DEV-ONLY, non-binding — see operator note).

VWAP is mandatory for the trigger band (§4.2): if the feed does not stream VWAP
for a strike, a rolling session VWAP is computed from (price x volume) deltas and
tagged ``vwap_source="computed"``.
"""

from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path
from typing import Iterator

from .marketdata import MarketSnapshot, StrikeQuote
from .mathx import round_to
from .timeutil import hhmm_reached, IST


class VWAPTracker:
    """Per (instrument,strike,right) rolling session VWAP from volume deltas."""

    def __init__(self):
        self._cum_pv: dict[tuple, float] = {}
        self._cum_vol: dict[tuple, float] = {}
        self._last_vol: dict[tuple, float] = {}

    def resolve(self, instrument, strike, right, ltp, volume, feed_vwap):
        if feed_vwap is not None and feed_vwap > 0:
            return float(feed_vwap), "feed"
        key = (instrument, strike, right)
        vol = float(volume or 0)
        last = self._last_vol.get(key, vol)        # first sight -> no delta
        dvol = max(0.0, vol - last)
        self._last_vol[key] = vol
        self._cum_pv[key] = self._cum_pv.get(key, 0.0) + (ltp or 0.0) * dvol
        self._cum_vol[key] = self._cum_vol.get(key, 0.0) + dvol
        if self._cum_vol[key] > 0:
            return self._cum_pv[key] / self._cum_vol[key], "computed"
        return float(ltp or 0.0), "computed"


def _snapshot_from_row(row: dict) -> MarketSnapshot:
    """Rebuild a MarketSnapshot from a tick_tape row (replay)."""
    strikes: dict[tuple[int, str], StrikeQuote] = {}
    for s in row.get("strikes", []) or []:
        q = StrikeQuote(
            strike=int(s["strike"]), right=s["right"], ltp=float(s["ltp"]),
            vwap=float(s["vwap"]), vwap_source=s["vwap_source"],
            volume=int(s["volume"]), oi=int(s["oi"]),
            bid=float(s["bid"]), ask=float(s["ask"]),
        )
        strikes[(q.strike, q.right)] = q
    return MarketSnapshot(
        ts_ms=int(row["ts"]), instrument=row["instrument"], spot=float(row["spot"]),
        vix=float(row.get("vix") or 0.0),
        future_traded=row.get("future") if row.get("basis_mode") == "ACTUAL" else None,
        strikes=strikes,
    )


# =============================================================================
# Replay source — reads a recorded tick_tape (jsonl sidecar preferred).
# =============================================================================
def _hhmm_ms(ts_ms: int, hhmm: str) -> int:
    """Epoch ms of HH:MM IST on the day of ts_ms."""
    import datetime as _dt
    ist = _dt.timezone(_dt.timedelta(hours=5, minutes=30))
    d = _dt.datetime.fromtimestamp(ts_ms / 1000, ist)
    h, m = map(int, hhmm.split(":"))
    return int(d.replace(hour=h, minute=m, second=0, microsecond=0).timestamp() * 1000)


class ReplaySource:
    def __init__(self, partition_dir: str | Path):
        self.partition_dir = Path(partition_dir)

    def _iter_rows(self):
        """2.0: STREAM the tape (1.x loaded the whole day into a Python list — ~17 KB/row, i.e.
        11-34 GB for a recorded day, which cannot run on the operator's machine). The parquet is
        preferred (it is what the operator keeps); the jsonl sidecar is the fallback. Rows are
        written in ts order by the live engine, so no global sort is needed."""
        from .tape import parquet_sources
        jsonl = self.partition_dir / "tick_tape.jsonl"
        # a same-day restart of a 2.0 session leaves the earlier part of the day in
        # tick_tape.parquet.*.bak while its jsonl sidecar holds the WHOLE day -> prefer the jsonl then
        # (teardown T06). 2.1 sessions have no big jsonl: parquet_sources() returns every readable
        # file of the day (leftover parts merged, .bak + main) in time order.
        has_bak = any(p for p in self.partition_dir.glob("tick_tape.parquet.*.bak")
                      if not p.name.endswith(".unreadable.bak"))   # a 2.0 footer-less file is not a restart
        files = [] if (has_bak and jsonl.exists()) else parquet_sources(self.partition_dir, "tick_tape")
        if files:
            import pyarrow.parquet as pq
            for f in files:
                with pq.ParquetFile(str(f)) as pf:
                    for batch in pf.iter_batches(batch_size=2000):
                        yield from batch.to_pylist()
        elif jsonl.exists():
            import sys as _s
            print("  vajra · replaying the jsonl sidecar (restarted day or no readable parquet)",
                  file=_s.stderr, flush=True)
            with open(jsonl, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line:
                        try:
                            yield json.loads(line)
                        except ValueError:
                            continue                   # a torn last line from a crash

    def _read_rows(self) -> list[dict]:          # kept for small fixtures / tests
        return list(self._iter_rows())

    def rounds(self) -> Iterator[tuple[int, dict[str, MarketSnapshot]]]:
        rows = self._iter_rows()
        cur_ts: int | None = None
        bucket: dict[str, MarketSnapshot] = {}
        for r in rows:
            ts = int(r["ts"])
            if cur_ts is None:
                cur_ts = ts
            if ts != cur_ts:
                yield cur_ts, bucket
                bucket = {}
                cur_ts = ts
            bucket[r["instrument"]] = _snapshot_from_row(r)
        if bucket and cur_ts is not None:
            yield cur_ts, bucket


# =============================================================================
# Live source — real Breeze websocket.
# =============================================================================
class LiveBreezeSource:
    def __init__(self, breeze, cfg, expiry_iso: str, basis_mode: str,
                 future_expiry_iso: str | None = None, event_sink=None):
        self.breeze = breeze
        self.cfg = cfg
        self.expiry_iso = expiry_iso                       # weekly options expiry (NIFTY only)
        # §4.1: subscribe the near-month FUTURE (the tradeable monthly contract),
        # which exists in every week as a sanity cross-check; in the monthly week
        # it equals the weekly expiry and drives ACTUAL-mode basis.
        self.future_expiry_iso = future_expiry_iso or expiry_iso
        self.basis_mode = basis_mode
        self.vwap = VWAPTracker()
        self._subscribed_options: set[tuple] = set()
        self._sub_sent_ms: dict[tuple, int] = {}      # 2.0.3: when each option subscribe was sent
        self._sub_retry_at: dict[tuple, int] = {}     # failed subscribe -> earliest retry
        self._sub_failed_reported: set[tuple] = set()
        self.sub_failures = 0
        self._pinned: set[tuple] = set()
        self._ws_silent = False
        self._build_err_seen: dict = {}
        self._ws_last_reconnect = 0.0     # (instrument, strike, right) always streamed (held position)
        self._stop = False
        self._all_gapped = False                           # latch: FEED_GAP all-gapped fires on the edge
        self._event_sink = event_sink                      # optional (event_type, payload, ts_ms=) sink
        self._unmapped_reported: set = set()               # ws names already event-taped (once per name)
        self._step_retry: dict = {}                        # inst -> next retry ts for failed step resolution
        self._step_obs: dict = {}                          # inst -> consecutive rounds the streamed grid contradicted the step
        self._step_corrected: set = set()                  # insts whose step was adopted from the streamed grid (once)
        # 2.0: steps listed in vajra_config.yaml are MEASURED and never auto-changed (1.x "corrected"
        # 8 of 17 steps wrongly in 3 days — the observed grid right after a start is a multiple).
        self._step_mis: dict = {}
        self._step_warned: set = set()
        self._fixed_steps = {k for k in (cfg.get("strike_step", default={}) or {}) if k != "resolve_from_chain"}
        self._step_futs: dict = {}                         # 2.1: in-flight step resolutions (never block a round)
        self._step_pool = None
        self._silent_other: dict = {}                      # 2.1: (inst,strike,right) -> re-subscribe count (sisters)
        self.stale_resubscribes = 0                         # 2.1.1: near-money quotes re-joined because they went quiet
        self._stale_reported: set = set()
        self._ws_req_reported = 0

    def _pool(self):
        if self._step_pool is None:
            from concurrent.futures import ThreadPoolExecutor
            self._step_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="vajra-step")
        return self._step_pool

    def stop(self):
        self._stop = True
        pool = getattr(self, "_step_pool", None)
        if pool is not None:
            try:
                pool.shutdown(wait=False, cancel_futures=True)
            except Exception:
                pass

    def health(self) -> dict:
        """2.1: feed-side health for the round tape and the cockpit (broker socket + subscriptions)."""
        h = {}
        fn = getattr(self.breeze, "health", None)
        if callable(fn):
            try:
                h = dict(fn())
            except Exception:
                h = {}
        h["silent"] = bool(self._ws_silent)
        h["option_subs"] = len(self._subscribed_options)
        h["nifty_silent_strikes"] = len(self._sub_failed_reported)
        h["other_resubscribes"] = sum(self._silent_other.values()) if self._silent_other else 0
        h["stale_resubscribes"] = self.stale_resubscribes
        return h

    def _options_expiry_for(self, instrument: str) -> str:
        """Which expiry an instrument's OPTION chain actually trades on. Per NSE's Nov-2024
        rationalization, ONLY NIFTY retained a weekly index-options expiry; BANKNIFTY, FINNIFTY, and
        every stock are MONTHLY-ONLY now. Subscribing/quoting them against NIFTY's weekly date
        requests a contract that does not exist -> Breeze ACKs the subscribe ('subscribed
        successfully') but never streams a tick, so the ATM quote is permanently missing (confirmed
        live 2026-07-01: basis_tape had zero rows all session for every non-NIFTY instrument)."""
        return self.expiry_iso if instrument == "NIFTY" else self.future_expiry_iso

    def _feed_event(self, event_type, payload, ts_ms):
        """Route a feed-side event (FEED_GAP / SESSION_ERROR) to the engine's tape+cockpit
        when wired; never let a logging failure unwind the feed loop."""
        if self._event_sink is None:
            return
        try:
            self._event_sink(event_type, payload, ts_ms=ts_ms)
        except Exception:
            pass

    def _snapshot_from_tick_row(self, row) -> MarketSnapshot | None:
        """Rebuild a NIFTY MarketSnapshot from a recorded tick_tape row (VAJRA's own prior live
        capture) — the proper prev-close source going forward."""
        strikes: dict[tuple[int, str], StrikeQuote] = {}
        for s in row.get("strikes", []) or []:
            if s.get("right") in ("CE", "PE") and s.get("ltp") is not None:
                k = (int(s["strike"]), s["right"])
                strikes[k] = StrikeQuote(int(s["strike"]), s["right"], float(s["ltp"]),
                                         float(s.get("vwap") or s["ltp"]), s.get("vwap_source") or "feed",
                                         int(s.get("volume") or 0), int(s.get("oi") or 0),
                                         float(s.get("bid") or 0.0), float(s.get("ask") or 0.0))
        if not strikes:
            return None
        return MarketSnapshot(ts_ms=int(row["ts"]), instrument="NIFTY", spot=float(row["spot"]),
                              vix=float(row.get("vix") or 0.0),
                              future_traded=row.get("future"), strikes=strikes)

    def prev_close_snapshot(self):
        """(operator rule a) Seed pre-open NIFTY levels from the previous trading day's close.
        Priority: (1) VAJRA's OWN most-recent prior live tick_tape (so each live day seeds from the
        last live day), then (2) the NEWEST recorded real_session file (by filename, NOT the stale
        config date). Sets self._seed_source so the orchestrator can announce it LOUDLY. Returns
        None only if nothing exists (orchestrator then emits a CRITICAL no-seed event)."""
        self._seed_source = None
        today = datetime.now(IST).date().isoformat()
        # 1) VAJRA's own prior live capture. 2.0: the PARQUET is read (the manual tells the operator to
        # delete the multi-GB jsonl), and rows from the broken 15:15+ cash feed (|basis| > 45) are skipped
        # so the seed is the last PLAUSIBLE NIFTY close, not the garbage tail.
        try:
            snap = self._seed_from_parquet(today)
            if snap is not None:
                return snap
        except Exception:
            pass
        try:
            root = self.cfg.tape_root
            days = sorted(p.name for p in root.glob("*") if p.is_dir()
                          and (p / "tick_tape.jsonl").exists() and p.name < today)
            if days:
                lastn = None
                p = root / days[-1] / "tick_tape.jsonl"
                # TAIL-read only the last few MB (the prev-close NIFTY row is at EOF). A full
                # read_text() of a multi-GB cumulative tape would OOM / hang the live startup.
                size = p.stat().st_size
                chunk = min(size, 8_000_000)                       # ~8 MB >> one round (a few KB)
                with open(p, "rb") as fh:
                    fh.seek(size - chunk)
                    tail = fh.read().decode("utf-8", errors="ignore")
                lines = tail.splitlines()
                if chunk < size and lines:
                    lines = lines[1:]                             # drop the partial first line
                for line in lines:
                    if '"NIFTY"' in line:
                        try:
                            r = json.loads(line)
                        except Exception:
                            continue
                        if r.get("instrument") == "NIFTY" and r.get("strikes"):
                            lastn = r                              # keep the LAST NIFTY row = the close
                snap = self._snapshot_from_tick_row(lastn) if lastn else None
                if snap is not None:
                    self._seed_source = f"live_tape:{days[-1]}"
                    return snap
        except Exception:
            pass
        # 2) Fall back to the NEWEST recorded real_session file (ignore the stale config date).
        try:
            from .realfeed import RealSessionSource
            ddir = Path(__file__).resolve().parent / "data"
            files = sorted(ddir.glob("real_session_*.jsonl"))
            if not files:
                return None
            newest = files[-1].stem.replace("real_session_", "")  # real_session_YYYY-MM-DD.jsonl -> date
            rs = RealSessionSource(self.cfg, session_date=newest)
            last = next((fr for fr in reversed(rs.frames) if fr.get("opts")), None)
            if last is None:
                return None
            ts = int(last["ts"]); iv = float(last.get("iv") or 0.12) or 0.12
            fut = last.get("fut")
            self._seed_source = f"recorded:{newest}(STALE — no prior live tape)"
            return rs._snap("NIFTY", last["spot"], self.cfg.strike_step("NIFTY"), ts,
                            rs._t_years(ts), iv, float(last.get("vix") or 0.0), int(last.get("vol") or 0),
                            fut=(fut if fut and fut > 0 else None),
                            real_oi=last.get("oi"), real_opts=last.get("opts"))
        except Exception:
            return None

    def _seed_from_parquet(self, today_iso):
        """2.1: scan the newest prior day's READABLE parquet files (leftover parts of a killed
        session are merged first; a footer-less 2.0 file is skipped) and fall back to older days."""
        import pyarrow.parquet as pq
        from .tape import parquet_sources
        root = self.cfg.tape_root
        days = sorted(p.name for p in root.glob("*") if p.is_dir() and p.name < today_iso
                      and len(p.name) == 10 and p.name[:4].isdigit())
        lim = float((self.cfg.get("feed_sanity", default={}) or {}).get("max_abs_basis_pts", 45))
        frozen_from = "15:15"                           # Breeze cash feed froze 15:15+ every day (T08)
        try:
            frozen_from = self._closing_for(today_iso).index_frozen_from
        except Exception:
            pass
        for day in reversed(days[-5:]):
            files = parquet_sources(root / day, "tick_tape")
            best = None
            for f in reversed(files):
                try:
                    with pq.ParquetFile(str(f)) as pf:
                        for gi in range(pf.num_row_groups - 1, max(-1, pf.num_row_groups - 400), -1):
                            rows = pf.read_row_group(gi).to_pylist()
                            for r in reversed(rows):
                                if r.get("instrument") == "NIFTY" and r.get("strikes") and \
                                        r.get("basis") is not None and abs(float(r["basis"])) <= lim and \
                                        not hhmm_reached(int(r["ts"]), frozen_from):   # index frozen after this
                                    best = r
                                    break
                            if best is not None:
                                break
                except Exception:
                    continue
                if best is not None:
                    break
            snap = self._snapshot_from_tick_row(best) if best else None
            if snap is not None:
                self._seed_source = f"live_tape_parquet:{day}"
                return snap
        return None

    def _closing_for(self, day_iso: str):
        from datetime import date as _date
        from .closing import resolve_closing
        d = _date.fromisoformat(day_iso)
        return resolve_closing(self.cfg, d, False)

    def _ensure_base_subscriptions(self):
        # Resolve heavyweight stock codes at runtime (get_names), with the operator override map as
        # a fallback — never guess broker codes. A subscribe/resolve failure must NOT unwind the
        # session (NIFTY resolves via INDEX_CODE independently): log a FEED_GAP and continue.
        ts0 = int(time.time_ns() // 1_000_000)
        override = self.cfg.get("breeze", "isec_override", default={}) or {}
        try:
            self.breeze.resolve_codes(self.cfg.stocks, override)
        except Exception as e:
            self._feed_event("FEED_GAP", {"stage": "code_resolution", "reason": repr(e)}, ts0)
        # Resolve the REAL near-ATM strike step per stock from the live chain (authoritative) and
        # write it into config so BOTH the feed subscriptions AND the engine's basis/band use the
        # correct strikes (stocks have wildly different steps; guessing/inferring fails -> the
        # MISSING_ATM_QUOTE flood). Indices keep their explicit config steps.
        ssteps = self.cfg.raw.setdefault("strike_step", {})
        todo = [inst for inst in self.cfg.stocks if inst not in ssteps]
        if todo:                                          # PARALLEL: 20 sequential get_option_chain_quotes
            from concurrent.futures import ThreadPoolExecutor   # took ~55s at startup -> ~5s in threads.

            def _resolve(inst):
                try:
                    return inst, self.breeze.option_step(inst, self._options_expiry_for(inst))
                except Exception:
                    return inst, None
            with ThreadPoolExecutor(max_workers=min(8, len(todo))) as ex:
                for inst, st in ex.map(_resolve, todo):
                    if st:
                        ssteps[inst] = st
                    else:
                        # Pre-market this ALWAYS fails (chains only answer after the open) — queue a
                        # runtime retry (see _step_maintenance) instead of silently defaulting to 5.0
                        # for the whole day (2026-07-01: only a lucky mid-morning restart resolved them).
                        self._step_retry[inst] = 0
                        self._feed_event("FEED_GAP", {"instrument": inst, "stage": "step_resolution",
                                                      "reason": "no_chain"}, ts0)
        many = getattr(self.breeze, "subscribe_many", None)
        if callable(many) and hasattr(self.breeze, "kw_spot"):     # 2.1.1: 49 streams in ONE packet
            kws = []
            for inst in self.cfg.universe:
                try:
                    kws.append(self.breeze.kw_spot(inst))
                    kws.append(self.breeze.kw_future(inst, self.future_expiry_iso))
                except Exception as e:
                    self._feed_event("FEED_GAP", {"instrument": inst, "stage": "subscription", "reason": repr(e)}, ts0)
            kws.append(self.breeze.kw_vix())
            try:
                many(kws)
            except Exception as e:
                self._feed_event("FEED_GAP", {"stage": "subscription_batch", "reason": repr(e)}, ts0)
        else:
            for inst in self.cfg.universe:
                try:
                    self.breeze.subscribe_spot(inst)
                    self.breeze.subscribe_future(inst, self.future_expiry_iso)
                except Exception as e:
                    self._feed_event("FEED_GAP", {"instrument": inst, "stage": "subscription", "reason": repr(e)}, ts0)
            try:
                self.breeze.subscribe_vix()
            except Exception as e:
                self._feed_event("FEED_GAP", {"stage": "subscription_vix", "reason": repr(e)}, ts0)

    def _step_maintenance(self, ts_ms, bucket):
        """Runtime strike-step self-healing (DATA-CAPTURE only — NIFTY execution uses NIFTY's own
        explicitly-configured step and is untouched by anything here):
        (1) RETRY: pre-market step resolution always fails (option chains only answer after the
            open), which used to leave every stock on the 5.0-pt default for the entire day unless
            the engine happened to restart mid-session. Failed stocks now retry every ~90s until
            the chain answers — on a normal 09:10 start they resolve right at the 09:15 open,
            before the 09:20 first calc.
        (2) OBSERVED-GRID CORRECTION: some stocks resolve a step from the full exchange list that
            is FINER than the grid that actually streams (FEDERALBNK/KOTAKBANK resolved 2.5 vs the
            real 5 -> ATM±6 spanned only ~7 real strikes). When the STREAMED chain's spacing is a
            clean >=2x multiple of the configured step for 3 consecutive rounds, adopt the streamed
            spacing — evidence from real ticks, never a guess (operator rule)."""
        ssteps = self.cfg.raw.setdefault("strike_step", {})
        # (1) retry chain-API resolution for stocks that failed at startup. 2.1: the broker REST call
        # runs on a pool thread and is collected on a LATER round -- it used to block the round loop
        # (1-18 s stalls, teardown T14) with no stop management while it waited.
        due = [i for i, nxt in self._step_retry.items() if ts_ms >= nxt and i not in self._step_futs]
        for inst in due:
            self._step_retry[inst] = ts_ms + 90_000          # next attempt if this one fails
            try:
                self._step_futs[inst] = self._pool().submit(self.breeze.option_step, inst,
                                                            self._options_expiry_for(inst))
            except Exception:
                pass
        if self._step_futs:                              # 2.1.1: never wait on the round path (was 20 ms/round)
            for inst, fut in list(self._step_futs.items()):
                if not fut.done():
                    continue
                del self._step_futs[inst]
                try:
                    st = fut.result()
                except Exception:
                    st = None
                if st:
                    ssteps[inst] = st
                    self._step_retry.pop(inst, None)
                    self._feed_event("ANOMALY", {"kind": "STEP_RESOLVED_LATE", "instrument": inst,
                                                 "step": st}, ts_ms)
        # (2) adopt the observed streamed grid when it contradicts the configured/default step
        for inst, snap in bucket.items():
            if inst in self._fixed_steps and inst not in self.cfg.indices and inst not in self._step_warned:
                obs = snap.inferred_step()
                cur = float(self.cfg.strike_step(inst))
                if obs is not None and len(snap.strikes) >= 6 and cur > 0 and abs(obs - cur) > 1e-6:
                    self._step_mis[inst] = self._step_mis.get(inst, 0) + 1
                    if self._step_mis[inst] >= 40:          # ~10 s of consistent contradiction -> say so ONCE
                        self._step_warned.add(inst)
                        self._feed_event("ANOMALY", {"kind": "STRIKE_STEP_TABLE_CONTRADICTED",
                                                     "instrument": inst, "table_step": cur,
                                                     "observed_step": float(obs),
                                                     "note": "listed step kept; check strike_step in vajra_config.yaml"},
                                         ts_ms)
                else:
                    self._step_mis[inst] = 0
            if inst in self.cfg.indices or inst in self._step_corrected or inst in self._fixed_steps:
                continue
            obs = snap.inferred_step()
            cur = float(self.cfg.strike_step(inst))            # resolved value, or the 5.0 stock default
            ok = (obs is not None and len(snap.strikes) >= 4 and obs >= 5 and cur > 0
                  and obs >= 2 * cur and abs(obs / cur - round(obs / cur)) < 1e-6)
            self._step_obs[inst] = (self._step_obs.get(inst, 0) + 1) if ok else 0
            if self._step_obs[inst] >= 3:
                ssteps[inst] = float(obs)
                self._step_corrected.add(inst)
                self._step_retry.pop(inst, None)               # real-tick evidence supersedes the retry
                self._feed_event("ANOMALY", {"kind": "STRIKE_STEP_CORRECTED", "instrument": inst,
                                             "from_step": cur, "to_step": float(obs),
                                             "note": "streamed grid contradicted the resolved step"}, ts_ms)

    def _closing(self):
        """2.0.4: today's NSE closing-session times (cached)."""
        ct = getattr(self, "_closing_times", None)
        if ct is None:
            from datetime import datetime as _dt
            from .closing import resolve_closing
            from .timeutil import IST as _IST
            today = _dt.now(tz=_IST).date()
            is_exp = str(getattr(self, "expiry_iso", "") or "") == today.isoformat()
            ct = self._closing_times = resolve_closing(self.cfg, today, is_exp)
        return ct

    def _ws_watchdog(self, ts_ms: int) -> None:
        """2.0.1 (teardown T08): no websocket tick at all for ws_silent_secs during market hours ->
        FEED_GAP (once) and a reconnect, re-requested every ws_reconnect_every_secs.
        2.1: the reconnect itself runs on the broker wrapper's supervisor thread; this method only
        REQUESTS it and copies the supervisor's log into the tape (the engine thread never waits on
        the socket -- 2.0 stalled 3-5.5 s per reconnect, 2 Nov tape)."""
        bz = self.breeze
        drain = getattr(bz, "drain_events", None)
        if callable(drain):
            try:
                for _wall, stage, payload in drain():
                    self._feed_event("FEED_GAP", {"stage": stage, **(payload or {})}, ts_ms)
            except Exception:
                pass
        last = getattr(bz, "last_tick_mono", None)
        if last is None:
            return
        if not (hhmm_reached(ts_ms, "09:15") and not hhmm_reached(ts_ms, self._closing().session_close)):
            return
        silent = time.monotonic() - last
        lim = float(self.cfg.get("feed", "ws_silent_secs", default=20))
        if silent < lim:
            if self._ws_silent:
                self._ws_silent = False
                self._feed_event("FEED_GAP", {"stage": "ws_resumed"}, ts_ms)
            return
        if not self._ws_silent:
            self._ws_silent = True
            self._feed_event("FEED_GAP", {"stage": "ws_silent", "silent_secs": round(silent, 1)}, ts_ms)
        every = float(self.cfg.get("feed", "ws_reconnect_every_secs", default=30))
        if time.monotonic() - self._ws_last_reconnect >= every:
            self._ws_last_reconnect = time.monotonic()
            req = getattr(bz, "request_reconnect", None)
            if callable(req):
                try:
                    accepted = bool(req(f"ws silent {silent:.0f}s"))
                except Exception:
                    accepted = False
                self._feed_event("FEED_GAP", {"stage": "ws_reconnect_requested", "silent_secs": round(silent, 1),
                                              "accepted": accepted}, ts_ms)
                if silent >= 3 * lim:
                    from .console import warn
                    self._ws_req_reported += 1
                    if self._ws_req_reported in (1, 5, 20):
                        warn(f"  !! BREEZE websocket silent for {silent:.0f} s (reconnect requested). If this "
                             f"persists the session key may have been invalidated (new login elsewhere) -- "
                             f"restart VAJRA with the current key.")
            elif hasattr(bz, "reconnect_ws"):          # legacy / test wrappers without a supervisor
                try:
                    ok, n = bz.reconnect_ws()
                except Exception:
                    ok, n = False, 0
                self._feed_event("FEED_GAP", {"stage": "ws_reconnect", "ok": bool(ok), "resubscribed": n}, ts_ms)

    def pin_strikes(self, keys: set) -> None:
        """Strikes that must stay in the snapshot regardless of the ATM±N window (the open position)."""
        self._pinned = set(keys)

    def _ensure_option_subscriptions(self, instrument, atm, step, n, ts_ms=None):
        expiry = self._options_expiry_for(instrument)
        now = int(ts_ms if ts_ms is not None else time.time() * 1000)
        # 2.0.3 (21 Oct tape): Breeze refused EVERY new subscription after 09:50 ("Could not authenticate
        # credentials"; the SDK only prints it). Subscribe NIFTY wide up front so a mid-day refusal
        # cannot leave the ATM without quotes on a trending day.
        if instrument == "NIFTY":
            n = max(n, int(self.cfg.get("chain", "nifty_prefetch_each_side", default=14)))
        batch: list = []
        many = getattr(self.breeze, "subscribe_many", None)
        kwf = getattr(self.breeze, "kw_option", None)
        for k in range(-n, n + 1):
            strike = int(atm + k * step)
            for right in ("CE", "PE"):
                key = (instrument, strike, right)
                if key not in self._subscribed_options and now >= self._sub_retry_at.get(key, 0):
                    if callable(many) and callable(kwf):
                        try:
                            batch.append(kwf(instrument, expiry, strike, right))
                        except Exception:
                            pass
                    else:
                        try:
                            self.breeze.subscribe_option(instrument, expiry, strike, right)
                        except Exception:
                            pass
                    self._subscribed_options.add(key)
                    self._sub_sent_ms[key] = now
        if batch:                                        # 2.1.1: one socket packet, not 2-5 ms per strike
            try:
                many(batch)
            except Exception:
                pass

    def _verify_option_data(self, instrument, code, want, raw, ts_ms):
        """2.0.3: a subscribe the broker silently refused never sends a tick. After 30 s in market
        hours with no tick, report it once (tape + console), forget it and retry every 60 s.
        2.1: sisters/stocks get the same re-subscribe for their ATM±1 strikes (the ones the basis and
        band need), silently and at most every 2 min -- thin option series may legitimately be quiet."""
        if not (hhmm_reached(ts_ms, "09:16") and not hhmm_reached(ts_ms, "15:15")):
            return
        # 2.1.1 (3 Nov tape): a stream that dies AFTER its first tick was never detected -- "a tick ever
        # arrived" counted as verified for the rest of the day. A near-the-money quote older than
        # quote_stale_resub_secs is re-subscribed (silently for sisters; a FEED_GAP once per NIFTY strike).
        _cfg = getattr(self, "cfg", None)
        stale_ms = int(float(_cfg.get("feed", "quote_stale_resub_secs", default=45)) * 1000) if _cfg else 45_000
        atm_n = getattr(self, "_last_atm", {}).get(instrument)
        step_n = float(_cfg.strike_step(instrument)) if (atm_n is not None and _cfg) else None
        if not hasattr(self, "stale_resubscribes"):
            self.stale_resubscribes, self._stale_reported = 0, set()

        def _stale(strike, right) -> bool:
            if atm_n is None or step_n is None or abs(strike - atm_n) > 2 * step_n:
                return False                             # only the strikes the engine trades on
            t = raw.get(self.breeze._key(code, "option", strike, right))
            rm = t.get("_recv_ms") if isinstance(t, dict) else None
            return rm is not None and ts_ms - rm > stale_ms

        if instrument != "NIFTY":
            atm = atm_n
            if atm is None:
                return
            step = float(self.cfg.strike_step(instrument))
            near = {int(atm + k * step) for k in (-1, 0, 1)}
            for strike, right in want:
                if strike not in near:
                    continue
                key = (instrument, strike, right)
                sent = self._sub_sent_ms.get(key)
                if sent is None:
                    if _stale(strike, right) and ts_ms >= self._sub_retry_at.get(key, 0):
                        self._subscribed_options.discard(key)          # re-subscribe on the next round
                        self._sub_retry_at[key] = ts_ms + 120_000
                        self._silent_other[key] = self._silent_other.get(key, 0) + 1
                        self.stale_resubscribes += 1
                    continue
                if ts_ms - max(sent, _hhmm_ms(ts_ms, "09:15")) < 60_000:
                    continue
                if raw.get(self.breeze._key(code, "option", strike, right)):
                    self._sub_sent_ms.pop(key, None)
                    continue
                self._subscribed_options.discard(key)
                self._sub_sent_ms.pop(key, None)
                self._sub_retry_at[key] = ts_ms + 120_000
                self._silent_other[key] = self._silent_other.get(key, 0) + 1
            return
        for strike, right in want:
            key = (instrument, strike, right)
            sent = self._sub_sent_ms.get(key)
            if sent is None:
                if _stale(strike, right) and ts_ms >= self._sub_retry_at.get(key, 0):
                    self._subscribed_options.discard(key)              # re-subscribe on the next round
                    self._sub_retry_at[key] = ts_ms + 60_000
                    self.stale_resubscribes += 1
                    if key not in self._stale_reported:
                        self._stale_reported.add(key)
                        self._feed_event("FEED_GAP", {"stage": "quote_stale_resubscribe", "instrument": instrument,
                                                      "strike": strike, "right": right,
                                                      "age_secs": round((ts_ms - raw[self.breeze._key(code, "option", strike, right)]["_recv_ms"]) / 1000, 1)}, ts_ms)
                continue
            if ts_ms - max(sent, _hhmm_ms(ts_ms, "09:15")) < 30_000:
                continue
            if raw.get(self.breeze._key(code, "option", strike, right)):
                self._sub_sent_ms.pop(key, None)           # data arrived: verified
                continue
            self._subscribed_options.discard(key)
            self._sub_sent_ms.pop(key, None)
            self._sub_retry_at[key] = ts_ms + 60_000
            self.sub_failures += 1
            if key not in self._sub_failed_reported:
                self._sub_failed_reported.add(key)
                self._feed_event("FEED_GAP", {"stage": "subscribe_no_data", "instrument": instrument,
                                              "strike": strike, "right": right,
                                              "note": "broker sent no data for this strike 30 s after "
                                                      "subscribing; retrying every 60 s"}, ts_ms)
                if len(self._sub_failed_reported) in (1, 10, 50):
                    import sys as _s
                    print(f"  !! BREEZE: no data for new subscriptions ({len(self._sub_failed_reported)} strikes, "
                          f"e.g. {instrument} {strike} {right}). If websocketLogs.log says 'Could not "
                          f"authenticate credentials', the session key was replaced (new login / key "
                          f"generated elsewhere) -- restart VAJRA with the current key.", file=_s.stderr, flush=True)

    def _window_center(self, instrument, spot, future, ts_ms) -> float:
        """2.0.4: the strike window follows the index -- except NIFTY in NSE's closing auction, where
        the index is frozen and then jumps to the indicative close (+435 pts on 21 Oct). Centred on that,
        the window dropped every near-the-money strike and the engine lost its quotes. From
        index_frozen_from it follows the FUTURE instead: index at the freeze + the future's move since."""
        if instrument != "NIFTY":
            return spot
        ct = self._closing()
        if not ct.spot_proxy or not hhmm_reached(ts_ms, ct.index_frozen_from):
            if future is not None:
                self._freeze_ref = (spot, future)
            elif getattr(self, "_freeze_ref", None) is None or self._freeze_ref[1] is None:
                self._freeze_ref = (spot, None)
            return spot
        ref = getattr(self, "_freeze_ref", None)
        if ref is None:
            return spot if future is None else future        # started inside the auction: best effort
        if ref[1] is None or future is None:
            return ref[0]
        return ref[0] + (future - ref[1])

    def _build_snapshot(self, instrument, ts_ms, raw=None) -> MarketSnapshot | None:
        from .breeze import INDIA_VIX_CODE
        if raw is None:                              # one snapshot_raw() per ROUND (shared across all
            raw = self.breeze.snapshot_raw()         # 23 instruments) -> 23x fewer lock grabs vs the tick firehose
        code = self.breeze.code_for(instrument)
        spot_tick = raw.get(self.breeze._key(code, "cash", None, None))
        if not spot_tick or spot_tick.get("ltp") is None:
            return None
        spot = float(spot_tick["ltp"])
        vix_tick = raw.get(self.breeze._key(INDIA_VIX_CODE, "cash", None, None))
        vix = float(vix_tick["ltp"]) if vix_tick and vix_tick.get("ltp") is not None else 0.0
        fut_tick = raw.get(self.breeze._key(code, "future", None, None))
        future = float(fut_tick["ltp"]) if fut_tick and fut_tick.get("ltp") is not None else None

        step = self.cfg.strike_step(instrument)
        n = self.cfg.strikes_each_side
        atm = int(round_to(self._window_center(instrument, spot, future, ts_ms), step))
        if not hasattr(self, "_last_atm"):
            self._last_atm = {}
        self._last_atm[instrument] = atm
        self._ensure_option_subscriptions(instrument, atm, step, n, ts_ms)

        strikes: dict[tuple[int, str], StrikeQuote] = {}
        want = [(int(atm + k * step), right) for k in range(-n, n + 1) for right in ("CE", "PE")]
        for (pi, pk, pr) in self._pinned:
            if pi == instrument and (pk, pr) not in want:
                key = (instrument, pk, pr)
                if key not in self._subscribed_options:
                    try:                                # a broker hiccup must not drop this instrument's round
                        self.breeze.subscribe_option(instrument, self._options_expiry_for(instrument), pk, pr)
                    except Exception:
                        pass
                    self._subscribed_options.add(key)
                    self._sub_sent_ms[key] = ts_ms
                want.append((pk, pr))
        for strike, right in want:
            if True:
                t = raw.get(self.breeze._key(code, "option", strike, right))
                if not t or t.get("ltp") is None:
                    continue
                vwap, src = self.vwap.resolve(instrument, strike, right,
                                              t.get("ltp"), t.get("volume"), t.get("vwap"))
                strikes[(strike, right)] = StrikeQuote(
                    strike=strike, right=right, ltp=float(t["ltp"]),
                    vwap=float(vwap), vwap_source=src,
                    volume=int(t.get("volume") or 0), oi=int(t.get("oi") or 0),
                    bid=float(t.get("bid") or 0.0), ask=float(t.get("ask") or 0.0),
                )
        try:
            self._verify_option_data(instrument, code, want, raw, ts_ms)
        except Exception:
            pass
        return MarketSnapshot(ts_ms=ts_ms, instrument=instrument, spot=spot,
                              vix=vix, future_traded=future, strikes=strikes)

    def rounds(self) -> Iterator[tuple[int, dict[str, MarketSnapshot]]]:
        self._ensure_base_subscriptions()
        poll = float(self.cfg.get("feed", "poll_interval_secs", default=1))
        # 2.0.4: run to the F&O close (15:40 under CAS); expiry.session_close can only EXTEND it
        session_close = max(self._closing().session_close,
                            str(self.cfg.get("expiry", "session_close", default="15:30")))
        while not self._stop:
            start = time.perf_counter()              # ADAPTIVE pacing: the round PERIOD is ~poll, not
            ts_ms = int(time.time_ns() // 1_000_000)  # poll + build (the build/process no longer stacks
            if hhmm_reached(ts_ms, session_close):    # ON TOP of a fixed sleep -> true 4/sec at 0.25s)
                break                                 # stop cleanly at the F&O close -> orchestrator finalize()
            raw = self.breeze.snapshot_raw()     # ONE lock-grab per round, shared across all instruments
            self._ws_watchdog(ts_ms)
            # R19 tape-durable warnings: the ws callback thread only collects unmapped tick names
            # into breeze._unmapped_warned (stderr); the EVENT is emitted here on the round thread
            # (thread-safe, once per name) so the failure is provable from the tape alone.
            _unmapped = getattr(self.breeze, "_unmapped_warned", None)
            if _unmapped:
                for _name in list(_unmapped):
                    if _name not in self._unmapped_reported:
                        self._unmapped_reported.add(_name)
                        self._feed_event("ANOMALY", {"kind": "UNMAPPED_WS_NAME", "name": _name,
                                                     "note": "tick name maps to no tracked instrument; "
                                                             "add to WS_NAME_TO_INSTRUMENT"}, ts_ms)
            bucket: dict[str, MarketSnapshot] = {}
            for inst in self.cfg.universe:
                try:
                    snap = self._build_snapshot(inst, ts_ms, raw)
                except Exception as e:          # a malformed tick must skip ONE instrument, not kill the session
                    _k = (inst, type(e).__name__)
                    if ts_ms - self._build_err_seen.get(_k, -10**12) >= 60_000:   # once a minute per (inst, error) (T21)
                        self._build_err_seen[_k] = ts_ms
                        self._feed_event("FEED_GAP", {"instrument": inst, "stage": "build_snapshot",
                                                      "reason": repr(e)}, ts_ms)
                    snap = None
                if snap is not None:
                    bucket[inst] = snap
            # strike-step self-healing: retry failed resolutions + adopt the observed streamed grid
            try:
                self._step_maintenance(ts_ms, bucket)
            except Exception as e:               # maintenance must never kill the feed loop
                if ts_ms - self._build_err_seen.get(("_maint", type(e).__name__), -10**12) >= 60_000:
                    self._build_err_seen[("_maint", type(e).__name__)] = ts_ms
                    self._feed_event("FEED_GAP", {"stage": "step_maintenance", "reason": repr(e)}, ts_ms)
            if bucket:
                if self._all_gapped:             # recovered -> announce ONCE, then resume
                    self._feed_event("FEED_GAP", {"stage": "feed_recovered"}, ts_ms)
                    self._all_gapped = False
                yield ts_ms, bucket              # process_round runs here (synchronous generator)
            else:                                # a 100%-gap poll: EDGE-fire once on entry (not every
                if not self._all_gapped:         # poll), still advance the clock so a stall != a hang
                    self._feed_event("FEED_GAP", {"stage": "all_instruments_gapped"}, ts_ms)
                    self._all_gapped = True
                yield ts_ms, {}
            dt = time.perf_counter() - start     # sleep only the REMAINDER of the poll after the round's work
            if dt < poll:
                time.sleep(poll - dt)


__all__ = ["ReplaySource", "LiveBreezeSource", "VWAPTracker", "MarketSnapshot", "StrikeQuote"]
