"""Keyboard MCP tools."""

import ctypes
import time

import pyautogui

from .server import mcp

KEY_MAP = {
    "enter": 0x0D, "tab": 0x09, "esc": 0x1B, "escape": 0x1B,
    "backspace": 0x08, "delete": 0x2E, "space": 0x20, "home": 0x24,
    "end": 0x23, "pageup": 0x21, "pagedown": 0x22, "left": 0x25,
    "up": 0x26, "right": 0x27, "down": 0x28,
}


@mcp.tool()
def type_text(text: str, interval: float = 0.05) -> str:
    """Type text into the currently focused Windows input field."""
    if not 0 <= interval <= 1:
        raise ValueError("interval must be between 0 and 1")
    pyautogui.write(text, interval=interval)
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
    user32.keybd_event(vk, 0, 0x0002, 0)
    return f"Pressed key: {key}"


@mcp.tool()
def hotkey(keys: list[str]) -> str:
    """Press a Windows keyboard shortcut, such as [\"ctrl\", \"s\"]."""
    if not keys:
        raise ValueError("keys cannot be empty")
    pyautogui.hotkey(*keys)
    return f"Pressed hotkey: {' + '.join(keys)}"


__all__ = ["hotkey", "press_key", "type_text"]
