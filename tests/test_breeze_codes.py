"""Offline-safe tests for the Breeze integration constants/format helpers.

These do NOT need credentials or a session — they guard the locale-safe expiry
format and the VIX decimal-shift fix carried over from MYTHOS's live integration.
"""

import sys
import types

import pytest

from vajra.breeze import (ws_expiry, normalize_vix, INDEX_CODE, INDIA_VIX_CODE,
                          BreezeWrapper, BreezeCredentials, BreezeConnectionError,
                          OrderPlacementError)


def test_ws_expiry_is_locale_safe_dd_mon_yyyy():
    # Must be the explicit English month form, not strftime('%b') (locale bug).
    assert ws_expiry("2026-06-16") == "16-Jun-2026"
    assert ws_expiry("2026-12-29") == "29-Dec-2026"
    assert ws_expiry("2026-01-06") == "06-Jan-2026"


def test_vix_decimal_shift_fix():
    assert normalize_vix(0.25) == 25.0        # shifted form -> corrected
    assert normalize_vix(14.0) == 14.0        # already correct -> passthrough
    assert normalize_vix(0.087) == 8.7


def test_index_codes_match_mythos_verified():
    assert INDEX_CODE["NIFTY"] == "NIFTY"
    assert INDEX_CODE["BANKNIFTY"] == "CNXBAN"
    assert INDEX_CODE["FINNIFTY"] == "NIFFIN"
    assert INDIA_VIX_CODE == "INDVIX"


def test_connect_retries_then_raises_clean_error(monkeypatch):
    """A failing Breeze connect retries the configured number of times and then raises a
    clean BreezeConnectionError (so the caller can log SESSION_ERROR), never an uncaught crash."""
    calls = {"n": 0}
    fake = types.ModuleType("breeze_connect")

    class _BC:
        def __init__(self, *a, **k):
            calls["n"] += 1
            raise RuntimeError("simulated broker/network failure")

    fake.BreezeConnect = _BC
    monkeypatch.setitem(sys.modules, "breeze_connect", fake)

    bw = BreezeWrapper(BreezeCredentials("k", "s", "t"),
                       reconnect_max_retries=3, reconnect_backoff_secs=0.0)
    with pytest.raises(BreezeConnectionError):
        bw.connect()
    assert calls["n"] == 3            # retried exactly the configured number of times
    assert bw.connected is False      # left in a clean disconnected state
