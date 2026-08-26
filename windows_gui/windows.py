"""Top-level window discovery, focus, and focused-action MCP tools."""

import ctypes
import time
from contextlib import contextmanager
from typing import Iterator

import pyautogui
import win32api
import win32con
import win32gui

from .keyboard import _type_text
from .mouse import _validate_scroll_amount
from .server import mcp


def _find_window(window_title: str) -> tuple[int, str]:
    if not window_title:
        raise ValueError("window_title cannot be empty")
    exact_matches: list[tuple[int, str]] = []
    partial_matches: list[tuple[int, str]] = []

    def enum_windows(hwnd, _):
        if win32gui.IsWindowVisible(hwnd):
            title = win32gui.GetWindowText(hwnd)
            if title.lower() == window_title.lower():
                exact_matches.append((hwnd, title))
            elif window_title.lower() in title.lower():
                partial_matches.append((hwnd, title))

    win32gui.EnumWindows(enum_windows, None)
    matches = exact_matches or partial_matches
    if not matches:
        raise ValueError(
            f"No visible window found containing title: {window_title}"
        )
    return matches[0]


@contextmanager
def _attached_input_threads(hwnd: int) -> Iterator[None]:
    """Temporarily attach input queues needed by SetForegroundWindow."""
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    foreground_hwnd = user32.GetForegroundWindow()
    current_thread = kernel32.GetCurrentThreadId()
    foreground_thread = user32.GetWindowThreadProcessId(foreground_hwnd, None)
    target_thread = user32.GetWindowThreadProcessId(hwnd, None)
    attached: list[int] = []
    try:
        for thread_id in dict.fromkeys((foreground_thread, target_thread)):
            if thread_id and thread_id != current_thread:
                if user32.AttachThreadInput(current_thread, thread_id, True):
                    attached.append(thread_id)
        yield
    finally:
        for thread_id in reversed(attached):
            user32.AttachThreadInput(current_thread, thread_id, False)


@contextmanager
def _focused_window(
    window_title: str, delay: float = 0.3
) -> Iterator[tuple[int, str]]:
    hwnd, actual_title = _find_window(window_title)
    _focus_window_handle(hwnd, delay)
    yield hwnd, actual_title


def _focus_window_handle(hwnd: int, delay: float = 0.3) -> str:
    """Focus one already-identified HWND without title re-matching."""
    if not win32gui.IsWindow(hwnd) or not win32gui.IsWindowVisible(hwnd):
        raise ValueError(f"Window handle is not visible: {hwnd}")
    user32 = ctypes.windll.user32
    with _attached_input_threads(hwnd):
        if win32gui.IsIconic(hwnd):
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        user32.ShowWindow(hwnd, win32con.SW_RESTORE)
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
        time.sleep(delay)
    return win32gui.GetWindowText(hwnd)


@mcp.tool()
def list_windows() -> list[str]:
    """List titles of visible top-level Windows windows."""
    titles: list[str] = []

    def enum_windows(hwnd, _):
        if win32gui.IsWindowVisible(hwnd):
            title = win32gui.GetWindowText(hwnd).strip()
            if title:
                titles.append(title)

    win32gui.EnumWindows(enum_windows, None)
    return list(dict.fromkeys(titles))


@mcp.tool()
def focus_window(window_title: str) -> str:
    """Find a visible Windows window by title and bring it to the foreground."""
    with _focused_window(window_title) as (_, actual_title):
        pass
    return f"Focused window '{actual_title}'"


@mcp.tool()
def focus_window_and_press(window_title: str, key: str) -> str:
    """Focus a visible window by title, then press one keyboard key."""
    with _focused_window(window_title, delay=0.5) as (_, actual_title):
        pyautogui.press(key)
    return f"Focused window '{actual_title}' and pressed {key}"


@mcp.tool()
def focus_window_and_hotkey(window_title: str, shortcut: str) -> str:
    """Focus a visible window by title and execute a keyboard shortcut."""
    keys = [key.strip().lower() for key in shortcut.split("+") if key.strip()]
    if not keys:
        raise ValueError("shortcut cannot be empty")
    with _focused_window(window_title, delay=0.4) as (_, actual_title):
        pyautogui.hotkey(*keys)
    return f"Focused '{actual_title}' and pressed {shortcut}"


@mcp.tool()
def focus_window_and_type(
    window_title: str, text: str, interval: float = 0.05
) -> str:
    """Focus a visible window by title, then type text into it."""
    if not 0 <= interval <= 1:
        raise ValueError("interval must be between 0 and 1")
    with _focused_window(window_title, delay=0.4) as (_, actual_title):
        _type_text(text, interval)
    return f"Focused '{actual_title}' and typed {len(text)} characters"


@mcp.tool()
def focus_window_and_scroll(window_title: str, amount: int) -> str:
    """Focus a visible window, move into it, then scroll its mouse wheel."""
    _validate_scroll_amount(amount)
    with _focused_window(window_title, delay=0.4) as (hwnd, actual_title):
        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
        pyautogui.moveTo((left + right) // 2, (top + bottom) // 2, duration=0.2)
        time.sleep(0.2)
        win32api.mouse_event(
            win32con.MOUSEEVENTF_WHEEL, 0, 0, amount * 120, 0
        )
    return f"Focused '{actual_title}' and scrolled {amount} units"


__all__ = [
    "focus_window", "focus_window_and_hotkey", "focus_window_and_press",
    "focus_window_and_scroll", "focus_window_and_type", "list_windows",
]
