"""Keyboard MCP tools."""

import ctypes
import time
from ctypes import wintypes

import pyautogui

from .server import mcp

KEY_MAP = {
    "enter": 0x0D, "tab": 0x09, "esc": 0x1B, "escape": 0x1B,
    "backspace": 0x08, "delete": 0x2E, "space": 0x20, "home": 0x24,
    "end": 0x23, "pageup": 0x21, "pagedown": 0x22, "left": 0x25,
    "up": 0x26, "right": 0x27, "down": 0x28,
}

KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
INPUT_KEYBOARD = 1


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", wintypes.WPARAM),
    ]


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", wintypes.WPARAM),
    ]


class _HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    ]


class _INPUTUNION(ctypes.Union):
    # INPUT must use the native union's full size. On 64-bit Windows the
    # MOUSEINPUT member is larger than KEYBDINPUT; omitting it makes cbSize
    # invalid and SendInput returns zero even for keyboard events.
    _fields_ = [
        ("mi", _MOUSEINPUT),
        ("ki", _KEYBDINPUT),
        ("hi", _HARDWAREINPUT),
    ]


class _INPUT(ctypes.Structure):
    _anonymous_ = ("union",)
    _fields_ = [("type", wintypes.DWORD), ("union", _INPUTUNION)]


def _send_unicode_code_unit(code_unit: int, key_up: bool = False) -> None:
    flags = KEYEVENTF_UNICODE | (KEYEVENTF_KEYUP if key_up else 0)
    event = _INPUT(
        type=INPUT_KEYBOARD,
        ki=_KEYBDINPUT(wVk=0, wScan=code_unit, dwFlags=flags),
    )
    sent = ctypes.windll.user32.SendInput(
        1, ctypes.byref(event), ctypes.sizeof(event)
    )
    if sent != 1:
        raise OSError(ctypes.get_last_error(), "SendInput failed for Unicode text")


def _send_virtual_key(vk: int) -> None:
    user32 = ctypes.windll.user32
    user32.keybd_event(vk, 0, 0, 0)
    user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)


def _send_unicode_text(text: str, interval: float) -> None:
    previous_was_cr = False
    for character in text:
        if character == "\r":
            _send_virtual_key(KEY_MAP["enter"])
            previous_was_cr = True
        elif character == "\n":
            if not previous_was_cr:
                _send_virtual_key(KEY_MAP["enter"])
            previous_was_cr = False
        elif character == "\t":
            _send_virtual_key(KEY_MAP["tab"])
            previous_was_cr = False
        else:
            previous_was_cr = False
            encoded = character.encode("utf-16-le")
            for offset in range(0, len(encoded), 2):
                code_unit = int.from_bytes(encoded[offset:offset + 2], "little")
                _send_unicode_code_unit(code_unit)
                _send_unicode_code_unit(code_unit, key_up=True)
        if interval:
            time.sleep(interval)


def _type_text(text: str, interval: float) -> None:
    if text.isascii():
        pyautogui.write(text, interval=interval)
    else:
        _send_unicode_text(text, interval)


@mcp.tool()
def type_text(text: str, interval: float = 0.05) -> str:
    """Type text into the currently focused Windows input field."""
    if not 0 <= interval <= 1:
        raise ValueError("interval must be between 0 and 1")
    _type_text(text, interval)
    return f"Typed {len(text)} characters"


@mcp.tool()
def press_key(key: str) -> str:
    """Press a Windows keyboard key using a native Windows keyboard event."""
    key = key.lower()
    if key not in KEY_MAP:
        raise ValueError(f"Unsupported key: {key}")
    vk = KEY_MAP[key]
    user32 = ctypes.windll.user32
    user32.keybd_event(vk, 0, 0, 0)
    time.sleep(0.1)
    user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)
    return f"Pressed key: {key}"


@mcp.tool()
def hotkey(keys: list[str]) -> str:
    """Press a Windows keyboard shortcut, such as [\"ctrl\", \"s\"]."""
    if not keys:
        raise ValueError("keys cannot be empty")
    pyautogui.hotkey(*keys)
    return f"Pressed hotkey: {' + '.join(keys)}"


__all__ = ["hotkey", "press_key", "type_text"]
