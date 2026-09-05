"""Read-only foreground, battery, local disk, display and cursor aggregation."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from datetime import datetime, timezone

import win32api
import win32file
import win32gui

from .server import mcp


class _PowerStatus(ctypes.Structure):
    _fields_ = [('ac', wintypes.BYTE), ('flags', wintypes.BYTE),
                ('percent', wintypes.BYTE), ('reserved', wintypes.BYTE),
                ('life_seconds', wintypes.DWORD), ('full_seconds', wintypes.DWORD)]


class NativeStatus:
    def foreground(self) -> dict:
        hwnd = win32gui.GetForegroundWindow()
        if not hwnd:
            return {'status': 'unknown', 'code': 'no_foreground_window'}
        title = win32gui.GetWindowText(hwnd)
        return {'status': 'ok', 'hwnd': int(hwnd), 'title': title[:256],
                'title_truncated': len(title) > 256}

    def battery(self) -> dict:
        value = _PowerStatus()
        api = ctypes.WinDLL('kernel32', use_last_error=True).GetSystemPowerStatus
        api.argtypes = [ctypes.POINTER(_PowerStatus)]
        api.restype = wintypes.BOOL
        if not api(ctypes.byref(value)):
            return {'status': 'unknown', 'code': 'query_failed'}
        return power_result(value.ac, value.flags, value.percent)

    def disks(self) -> dict:
        drives = win32api.GetLogicalDriveStrings().split('\0')
        results = []
        for drive in drives[:26]:
            if not drive:
                continue
            if win32file.GetDriveType(drive) != 3:
                continue  # no network/removable media query
            try:
                available, total, _ = win32api.GetDiskFreeSpaceEx(drive)
                results.append({'drive': drive[:2], 'status': 'ok',
                                'free_bytes': available, 'total_bytes': total})
            except Exception:
                results.append({'drive': drive[:2], 'status': 'unknown', 'code': 'query_failed'})
        return {'status': 'ok' if results and all(x['status'] == 'ok' for x in results) else 'unknown',
                'volumes': results}

    def screen(self) -> dict:
        width, height = win32api.GetSystemMetrics(0), win32api.GetSystemMetrics(1)
        if width <= 0 or height <= 0:
            return {'status': 'unknown', 'code': 'query_failed'}
        return {'status': 'ok', 'primary': {'width': width, 'height': height},
                'virtual': {'left': win32api.GetSystemMetrics(76), 'top': win32api.GetSystemMetrics(77),
                            'width': win32api.GetSystemMetrics(78), 'height': win32api.GetSystemMetrics(79)},
                'coordinate_space': 'process_dpi_awareness'}

    def mouse(self) -> dict:
        x, y = win32api.GetCursorPos()
        return {'status': 'ok', 'x': x, 'y': y}


def power_result(ac: int, flags: int, percent: int) -> dict:
    if flags == 255:
        return {'status': 'unknown', 'code': 'battery_unknown'}
    if flags & 128:
        return {'status': 'ok', 'state': 'not_present', 'percent': None,
                'charging': None, 'ac_connected': ac == 1 if ac in (0, 1) else None}
    return {'status': 'ok' if percent <= 100 else 'unknown', 'state': 'present',
            'percent': percent if percent <= 100 else None,
            'charging': bool(flags & 8), 'ac_connected': ac == 1 if ac in (0, 1) else None}


def collect_status(*, backend=None) -> dict:
    backend = backend or NativeStatus()
    parts = {}
    for name, method in [('foreground_window', 'foreground'), ('battery', 'battery'),
                         ('disks', 'disks'), ('screen', 'screen'), ('mouse', 'mouse')]:
        try:
            parts[name] = getattr(backend, method)()
        except Exception:
            parts[name] = {'status': 'unknown', 'code': 'query_failed'}
    partial = any(value['status'] != 'ok' for value in parts.values())
    return {'status': 'partial' if partial else 'ok', 'code': 'partial_status' if partial else 'ok',
            'observed_at': datetime.now(timezone.utc).isoformat(), **parts}


@mcp.tool()
def get_system_status() -> dict:
    """Read foreground title, battery, fixed local disk free space, screen size and mouse.

    No focus/input/network probes/clipboard/credentials/process command lines.
    Missing battery is not_present; failed queries are unknown, never invented zero.
    Titles are bounded and not logged. This is a sequential observation, not an atomic snapshot.
    """
    return collect_status()
