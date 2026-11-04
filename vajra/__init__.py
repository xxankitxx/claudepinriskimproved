"""VAJRA 2.0 — Cross-Instrument Pin-Risk / Gamma-Explosion Engine.

Expiry-day specialist sibling to MYTHOS. Tracks the whole universe
(NIFTY / BANKNIFTY / FINNIFTY + heavyweight constituents); executes on
NIFTY options only. See VAJRA_PIN_RISK_ENGINE_SPEC.md.

This package is fully self-contained — it does NOT import from the MYTHOS
folder. The Breeze integration *patterns* are re-implemented here (spec §0.7
reconciled with the operator's independence requirement).
"""

SCHEMA_VERSION = "2.0.0"
VERSION = "2.0.0"

__all__ = ["SCHEMA_VERSION", "VERSION"]
