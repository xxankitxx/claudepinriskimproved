"""Config loader — vajra_config.yaml (§12) + nifty_weights.csv (§3).

Weights are NEVER hard-coded in the engine; they are read from the CSV here so
the operator can refresh them on each semi-annual rebalance (spec §3).
"""

from __future__ import annotations

import csv
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent


@dataclass
class InstrumentWeight:
    instrument: str
    type: str                       # index | stock
    nifty_weight_pct: float | None
    bank_weight_pct: float | None
    fin_weight_pct: float | None
    beta_vs_nifty: float | None
    role: str
    channel: str

    @property
    def decision_weight(self) -> float:
        """Importance of this stock in the cross-instrument vote (spec §5.6).

        Every tracked stock contributes per its own index weightage / 100:
        the NIFTY-50 weight where published (shared banks + the other NIFTY
        heavyweights), else the stock's BANK or FIN weight for constituents
        shown only in those factsheets.  Indices use the bloc/self weights from
        config, so 0.0 here for indices.
        """
        # Operator rule (unchanged from 1.x): NIFTY weight, else BANK, else FIN factsheet weight.
        # (Review finding 07 questions this for non-NIFTY names; left as the operator decided.)
        if self.type == "stock":
            w = self.nifty_weight_pct
            if w is None:
                w = self.bank_weight_pct
            if w is None:
                w = self.fin_weight_pct
            if w is not None:
                return w / 100.0
        return 0.0


@dataclass
class VajraConfig:
    raw: dict[str, Any]
    weights: dict[str, InstrumentWeight] = field(default_factory=dict)
    config_path: Path = REPO_ROOT / "vajra_config.yaml"

    # ---- generic access -----------------------------------------------------
    def get(self, *keys: str, default: Any = None) -> Any:
        node: Any = self.raw
        for k in keys:
            if not isinstance(node, dict) or k not in node:
                return default
            node = node[k]
        return node

    # ---- typed convenience accessors ---------------------------------------
    @property
    def schema_version(self) -> str:
        return self.raw.get("schema_version", "1.0.0")

    @property
    def indices(self) -> list[str]:
        return list(self.get("universe", "indices", default=[]))

    @property
    def stocks(self) -> list[str]:
        return list(self.get("universe", "stocks", default=[]))

    @property
    def universe(self) -> list[str]:
        return self.indices + self.stocks

    def strike_step(self, instrument: str, chain_inferred: float | None = None) -> float:
        steps = self.get("strike_step", default={}) or {}
        if instrument in steps and instrument != "resolve_from_chain":
            return float(steps[instrument])
        if steps.get("resolve_from_chain", True) and chain_inferred:
            return float(chain_inferred)
        # Sensible fallback by type; stocks vary, default to 5 if unknown.
        return 50.0 if instrument in self.indices else (chain_inferred or 5.0)

    @property
    def strikes_each_side(self) -> int:
        return int(self.get("chain", "strikes_each_side", default=6))

    @property
    def live_orders(self) -> bool:
        return live_orders_enabled(self.get("execution", default={}) or {})

    @property
    def routing_mode(self) -> str:
        return str(self.get("routing", "routing_mode", default="CONFIRM"))

    @property
    def nonexpiry_mode(self) -> str:
        return str(self.get("routing", "nonexpiry_mode", default="trade_small"))

    @property
    def thresholds_calibrated(self) -> bool:
        return bool(self.get("decision", "thresholds_calibrated", default=False))

    @property
    def server_port(self) -> int:
        return int(self.get("server", "port", default=8766))

    @property
    def server_host(self) -> str:
        return str(self.get("server", "host", default="127.0.0.1"))

    @property
    def tape_root(self) -> Path:
        root = self.get("tape", "root_dir", default="tapes")
        p = Path(root)
        return p if p.is_absolute() else (REPO_ROOT / p)

    # ---- secrets (from credentials.py, mirroring MYTHOS; never from yaml) ---
    def breeze_secret(self, which: str) -> str | None:
        """Read a Breeze secret.

        Primary source is the local ``vajra/credentials.py`` module
        (API_KEY / API_SECRET / SESSION_KEY) — same mechanism as MYTHOS.
        Falls back to an environment variable if the constant is blank, so CI
        or a container can still inject secrets without editing the file.
        """
        attr = {"api_key": "API_KEY", "api_secret": "API_SECRET",
                "session_token": "SESSION_KEY"}.get(which)
        # 2.0: the DAILY session token no longer requires editing a .py file. Precedence:
        # --token (set by app into env) > env VAJRA_SESSION_KEY > vajra/.token (git-ignored) >
        # credentials.py > legacy env fallback.
        if which == "session_token":
            v = os.environ.get("VAJRA_SESSION_KEY", "").strip()
            if v:
                self.session_token_source = "--token / env VAJRA_SESSION_KEY"
                return v
            tok = Path(__file__).resolve().parent / ".token"
            if tok.exists():
                txt = _read_text_any(tok).strip()
                if txt:
                    self.session_token_source = "vajra/.token"
                    return txt.splitlines()[0].strip()
        try:
            from . import credentials as _creds
            val = getattr(_creds, attr, None) if attr else None
            if val:
                if which == "session_token":
                    self.session_token_source = "vajra/credentials.py"
                return val
        except SyntaxError as e:                 # a typo while pasting the daily token must be LOUD
            import sys
            print(f"[VAJRA ERROR] vajra/credentials.py has a syntax error: {e}", file=sys.stderr, flush=True)
        except Exception:
            pass
        env_key = self.get("breeze", f"{which}_env", default=None)
        return os.environ.get(env_key) if env_key else None


def live_orders_enabled(ex: dict) -> bool:
    """Real broker orders ONLY when LIVE_ORDERS is the YAML boolean `true` AND the explicit opt-in
    `LIVE_ORDERS_CONFIRM: "I UNDERSTAND REAL MONEY"` is present. 1.x used bool(), so a quoted
    "false" / "no" / "off" turned LIVE orders ON (teardown T23). Anything else = paper."""
    return (ex.get("LIVE_ORDERS") is True
            and str(ex.get("LIVE_ORDERS_CONFIRM", "")).strip() == "I UNDERSTAND REAL MONEY")


def _read_text_any(path) -> str:
    """Read a small text file written by Notepad / PowerShell 5.1 (`>` writes UTF-16 LE with BOM,
    Out-File/Set-Content may add a UTF-8 BOM). 1.x/2.0-draft read strict UTF-8 -> crash or a token
    with a hidden BOM char (teardown T09)."""
    b = Path(path).read_bytes()
    if b.startswith(b"\xff\xfe") or b.startswith(b"\xfe\xff"):
        t = b.decode("utf-16")
    elif len(b) >= 2 and b[1:2] == b"\x00":            # UTF-16 LE without BOM
        t = b.decode("utf-16-le", errors="ignore")
    else:
        t = b.decode("utf-8-sig", errors="replace")
    return t.replace("\ufeff", "").replace("\x00", "")


def _to_float(s: str) -> float | None:
    s = (s or "").strip()
    if s == "":
        return None
    return float(s)


def load_weights(csv_path: Path) -> dict[str, InstrumentWeight]:
    out: dict[str, InstrumentWeight] = {}
    with open(csv_path, "r", newline="", encoding="utf-8") as fh:
        # Skip leading comment lines beginning with '#'
        lines = [ln for ln in fh if not ln.lstrip().startswith("#")]
    reader = csv.DictReader(lines)
    for row in reader:
        inst = row["instrument"].strip()
        out[inst] = InstrumentWeight(
            instrument=inst,
            type=row["type"].strip(),
            nifty_weight_pct=_to_float(row.get("nifty_weight_pct", "")),
            bank_weight_pct=_to_float(row.get("bank_weight_pct", "")),
            fin_weight_pct=_to_float(row.get("fin_weight_pct", "")),
            beta_vs_nifty=_to_float(row.get("beta_vs_nifty", "")),
            role=row.get("role", "").strip(),
            channel=row.get("channel", "").strip(),
        )
    return out


def load_config(config_path: str | Path | None = None) -> VajraConfig:
    cfg_path = Path(config_path) if config_path else (REPO_ROOT / "vajra_config.yaml")
    with open(cfg_path, "r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    weights_rel = raw.get("universe", {}).get("refresh_weights_from", "nifty_weights.csv")
    weights_path = Path(weights_rel)
    if not weights_path.is_absolute():
        weights_path = cfg_path.parent / weights_path
    weights = load_weights(weights_path)
    cfg = VajraConfig(raw=raw, weights=weights, config_path=cfg_path)
    return cfg


__all__ = ["VajraConfig", "InstrumentWeight", "load_config", "load_weights", "REPO_ROOT"]
