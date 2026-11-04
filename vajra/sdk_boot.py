"""2.1.1 — import the Breeze SDK without paying for its security-master download three times.

breeze_connect downloads ``SecurityMaster.zip`` (tens of MB) at MODULE IMPORT (``urlopen``), then again
twice inside ``get_stock_script_list`` (``urlopen`` + ``requests.get``). On 3 Nov 2026 the import alone
took 3.5 minutes while the engine was waiting to restart. The master changes once a day, so it is
cached under ``tapes/_cache`` and served from disk for the rest of the trading day; the SDK's own code
is untouched (the two fetch functions are wrapped for that one URL only).
"""
from __future__ import annotations

import io
import os
import time
import threading
from pathlib import Path

MASTER_URL = "https://directlink.icicidirect.com/MotherAppMaster/SecurityMaster.zip"
_CACHE_DIR = Path(os.environ.get("VAJRA_SDK_CACHE", "tapes/_cache"))
_CACHE = _CACHE_DIR / "SecurityMaster.zip"
_lock = threading.Lock()
_patched = False
stats = {"served_from_cache": 0, "downloaded": 0, "download_secs": None}


def cache_fresh(path: Path = _CACHE, max_age_h: float = 12.0) -> bool:
    try:
        st = path.stat()
    except OSError:
        return False
    if st.st_size < 1_000_000:                      # a torn / empty download is not a cache
        return False
    age_ok = (time.time() - st.st_mtime) < max_age_h * 3600
    same_day = time.localtime(st.st_mtime).tm_yday == time.localtime().tm_yday
    return age_ok and same_day


def _download(timeout: float = 120.0) -> bytes:
    t0 = time.monotonic()
    with _ORIG_URLOPEN(MASTER_URL, timeout=timeout) as r:        # the real urlopen
        data = r.read()
    stats["downloaded"] += 1
    stats["download_secs"] = round(time.monotonic() - t0, 1)
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _CACHE.with_suffix(".zip.tmp")
    tmp.write_bytes(data)
    os.replace(tmp, _CACHE)
    return data


def master_bytes(max_age_h: float = 12.0) -> bytes:
    with _lock:
        if cache_fresh(_CACHE, max_age_h):
            stats["served_from_cache"] += 1
            return _CACHE.read_bytes()
        try:
            return _download()
        except Exception:
            if _CACHE.exists() and _CACHE.stat().st_size > 1_000_000:   # stale beats nothing
                stats["served_from_cache"] += 1
                return _CACHE.read_bytes()
            raise


_ORIG_URLOPEN = None


class _Resp(io.BytesIO):
    status = 200
    status_code = 200

    @property
    def content(self):
        return self.getvalue()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()
        return False

    def raise_for_status(self):
        return None


def patch() -> None:
    """Wrap urllib.request.urlopen and requests.get for the security-master URL only. Idempotent."""
    global _patched, _ORIG_URLOPEN
    if _patched:
        return
    import urllib.request
    import requests
    _ORIG_URLOPEN = urllib.request.urlopen
    orig_get = requests.get

    def urlopen(url, *a, **kw):
        if _is_master(url):
            return _Resp(master_bytes())
        return _ORIG_URLOPEN(url, *a, **kw)

    def get(url, *a, **kw):
        if _is_master(url):
            return _Resp(master_bytes())
        return orig_get(url, *a, **kw)

    urllib.request.urlopen = urlopen
    requests.get = get
    _patched = True


def _is_master(url) -> bool:
    try:
        s = url if isinstance(url, str) else getattr(url, "full_url", "")
    except Exception:
        return False
    return s == MASTER_URL


def import_sdk():
    """`import breeze_connect` with the cache in place. Returns the module."""
    patch()
    import breeze_connect
    return breeze_connect
