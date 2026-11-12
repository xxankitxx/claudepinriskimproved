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


def _set_priority_class(cls: int) -> bool:
    """SetPriorityClass on our own process. The handle types are declared: with ctypes' default int
    types the 64-bit pseudo-handle from GetCurrentProcess() is truncated and the call fails silently."""
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.GetCurrentProcess.restype = wintypes.HANDLE
        k32.GetCurrentProcess.argtypes = []
        k32.SetPriorityClass.restype = wintypes.BOOL
        k32.SetPriorityClass.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        return bool(k32.SetPriorityClass(k32.GetCurrentProcess(), cls))
    except Exception:
        return False


def disable_power_throttling() -> bool:
    """2.2.2: opt this process out of Windows power throttling (EcoQoS). A console window in the
    background is otherwise a candidate for the slow efficiency cores at reduced clock on this laptop's
    hybrid CPU -- the opposite of what a live feed needs. Execution speed and timer resolution both."""
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        from ctypes import wintypes

        class _PPTS(ctypes.Structure):
            _fields_ = [("Version", wintypes.ULONG), ("ControlMask", wintypes.ULONG),
                        ("StateMask", wintypes.ULONG)]
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.GetCurrentProcess.restype = wintypes.HANDLE
        k32.SetProcessInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        k32.SetProcessInformation.restype = wintypes.BOOL
        st = _PPTS(1, 0x1 | 0x4, 0)            # control EXECUTION_SPEED + IGNORE_TIMER_RESOLUTION, both OFF
        return bool(k32.SetProcessInformation(k32.GetCurrentProcess(), 4,      # ProcessPowerThrottling
                                              ctypes.byref(st), ctypes.sizeof(st)))
    except Exception:
        return False


def current_priority_class() -> int | None:
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.GetCurrentProcess.restype = wintypes.HANDLE
        k32.GetPriorityClass.restype = wintypes.DWORD
        k32.GetPriorityClass.argtypes = [wintypes.HANDLE]
        return int(k32.GetPriorityClass(k32.GetCurrentProcess())) or None
    except Exception:
        return None


_PRIORITY = {"normal": 0x00000020, "above_normal": 0x00008000, "high": 0x00000080}


def raise_priority(level: str = "above_normal") -> bool:
    """2.2 (9 Nov 2026): other jobs on the laptop took the CPU and the engine's rounds ran 4x slower
    while its socket fell 50 s behind. Give the LIVE engine process a higher scheduling class so
    batch work elsewhere waits for it, not the other way round. Own process only; never realtime."""
    if sys.platform != "win32":
        return False
    cls = _PRIORITY.get(str(level or "").lower())
    if cls is None or cls == _PRIORITY["normal"]:
        return False
    return _set_priority_class(cls)


__all__ = ["keep_awake", "release", "status", "raise_priority", "disable_power_throttling"]
