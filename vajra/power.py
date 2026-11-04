"""2.1 — machine power hygiene for a live session (2 Nov 2026: the operator's laptop switched off at
15:28 with a paper position open; nothing in VAJRA could have prevented the hard kill, but it CAN keep
Windows from sleeping on its own and shout when the machine is running on battery).

  * keep_awake()  — SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_AWAYMODE_REQUIRED):
                    no idle sleep / hibernate while the engine runs (the display may still blank).
  * release()     — back to the normal policy at shutdown.
  * status()      — {"on_battery": bool|None, "battery_pct": int|None, "ac": bool|None} from
                    GetSystemPowerStatus. None values mean "could not read" (non-Windows / no battery).
Everything is best-effort and never raises.
"""

from __future__ import annotations

import sys

_ES_CONTINUOUS = 0x80000000
_ES_SYSTEM_REQUIRED = 0x00000001
_ES_AWAYMODE_REQUIRED = 0x00000040

_held = False


def keep_awake() -> bool:
    global _held
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        r = ctypes.windll.kernel32.SetThreadExecutionState(
            _ES_CONTINUOUS | _ES_SYSTEM_REQUIRED | _ES_AWAYMODE_REQUIRED)
        if not r:                                   # away mode not supported -> plain system-required
            r = ctypes.windll.kernel32.SetThreadExecutionState(_ES_CONTINUOUS | _ES_SYSTEM_REQUIRED)
        _held = bool(r)
        return _held
    except Exception:
        return False


def release() -> None:
    global _held
    if sys.platform != "win32" or not _held:
        return
    try:
        import ctypes
        ctypes.windll.kernel32.SetThreadExecutionState(_ES_CONTINUOUS)
    except Exception:
        pass
    _held = False


def status() -> dict:
    out = {"on_battery": None, "battery_pct": None, "ac": None}
    if sys.platform != "win32":
        return out
    try:
        import ctypes
        from ctypes import wintypes

        class _SPS(ctypes.Structure):
            _fields_ = [("ACLineStatus", wintypes.BYTE), ("BatteryFlag", wintypes.BYTE),
                        ("BatteryLifePercent", wintypes.BYTE), ("SystemStatusFlag", wintypes.BYTE),
                        ("BatteryLifeTime", wintypes.DWORD), ("BatteryFullLifeTime", wintypes.DWORD)]
        s = _SPS()
        if not ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(s)):
            return out
        ac = s.ACLineStatus & 0xFF
        pct = s.BatteryLifePercent & 0xFF
        flag = s.BatteryFlag & 0xFF
        no_battery = bool(flag & 128)
        out["ac"] = (ac == 1) if ac in (0, 1) else None
        out["battery_pct"] = None if (pct == 255 or no_battery) else int(pct)
        out["on_battery"] = (ac == 0) if ac in (0, 1) else None
        return out
    except Exception:
        return out


__all__ = ["keep_awake", "release", "status"]
