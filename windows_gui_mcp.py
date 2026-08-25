"""Backward-compatible entry point for the Windows GUI MCP server."""

from windows_gui.keyboard import hotkey, press_key, type_text
from windows_gui.mouse import (
    click_mouse, double_click, drag_mouse, focus_and_press,
    get_mouse_position, move_mouse, right_click, screenshot, scroll,
)
from windows_gui.server import mcp
from windows_gui.uia import (
    click_control, click_menu_item, click_save_button, list_controls,
    set_save_dialog_filename,
)
from windows_gui.windows import (
    focus_window, focus_window_and_hotkey, focus_window_and_press,
    focus_window_and_scroll, focus_window_and_type, list_windows,
)

__all__ = [
    "click_control", "click_menu_item", "click_mouse", "click_save_button",
    "double_click", "drag_mouse", "focus_and_press", "focus_window",
    "focus_window_and_hotkey", "focus_window_and_press",
    "focus_window_and_scroll", "focus_window_and_type",
    "get_mouse_position", "hotkey", "list_controls", "list_windows", "mcp",
    "move_mouse", "press_key", "right_click", "screenshot", "scroll",
    "set_save_dialog_filename", "type_text",
]


if __name__ == "__main__":
    mcp.run()
