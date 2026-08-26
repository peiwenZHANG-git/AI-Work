"""Side-effect-free tests for window discovery and focused actions."""

import unittest
from contextlib import contextmanager
from unittest.mock import patch

from tests.test_windows_gui_mcp import _install_dependency_stubs

_install_dependency_stubs()

from windows_gui import windows


def _enum_with(items):
    def enum_windows(callback, extra):
        for hwnd, title, visible in items:
            with patch.object(windows.win32gui, "IsWindowVisible", return_value=visible):
                with patch.object(windows.win32gui, "GetWindowText", return_value=title):
                    callback(hwnd, extra)
    return enum_windows


@contextmanager
def _fake_focus(_title, delay=0.3):
    yield 99, "Smoke Target - Notepad"


class WindowLogicTests(unittest.TestCase):
    def test_find_window_is_case_insensitive_and_list_deduplicates(self):
        items = [(1, "Alpha", True), (2, "alpha", True), (3, "Hidden", False)]
        with patch.object(windows.win32gui, "EnumWindows", side_effect=_enum_with(items)):
            self.assertEqual((1, "Alpha"), windows._find_window("ALP"))
            self.assertEqual(["Alpha", "alpha"], windows.list_windows())
        with patch.object(windows.win32gui, "EnumWindows", side_effect=_enum_with([])):
            with self.assertRaisesRegex(ValueError, "No visible window"):
                windows._find_window("missing")

    def test_find_window_prefers_exact_title_over_earlier_partial_match(self):
        items = [
            (1, "Confirm - Smoke Target", True),
            (2, "Smoke Target", True),
        ]
        with patch.object(windows.win32gui, "EnumWindows", side_effect=_enum_with(items)):
            self.assertEqual((2, "Smoke Target"), windows._find_window("smoke target"))

    def test_focused_keyboard_actions(self):
        with (
            patch.object(windows, "_focused_window", _fake_focus),
            patch.object(windows.pyautogui, "press") as press,
            patch.object(windows.pyautogui, "hotkey") as hotkey,
            patch.object(windows, "_type_text") as type_text,
        ):
            self.assertIn("Focused window", windows.focus_window("target"))
            self.assertIn("pressed home", windows.focus_window_and_press("target", "home"))
            press.assert_called_once_with("home")
            self.assertIn("ctrl + a", windows.focus_window_and_hotkey("target", " ctrl + a "))
            hotkey.assert_called_once_with("ctrl", "a")
            self.assertIn("typed 4", windows.focus_window_and_type("target", "test", 0.1))
            type_text.assert_called_once_with("test", 0.1)
        with self.assertRaisesRegex(ValueError, "shortcut cannot be empty"):
            windows.focus_window_and_hotkey("target", " + ")
        with self.assertRaisesRegex(ValueError, "interval"):
            windows.focus_window_and_type("target", "x", -0.1)

    def test_focused_scroll_uses_window_center(self):
        with (
            patch.object(windows, "_focused_window", _fake_focus),
            patch.object(windows.win32gui, "GetWindowRect", return_value=(0, 10, 100, 210)),
            patch.object(windows.pyautogui, "moveTo") as move_to,
            patch.object(windows.time, "sleep"),
            patch.object(windows.win32api, "mouse_event") as event,
        ):
            self.assertIn("scrolled 3", windows.focus_window_and_scroll("target", 3))
        move_to.assert_called_once_with(50, 110, duration=0.2)
        event.assert_called_once_with(windows.win32con.MOUSEEVENTF_WHEEL, 0, 0, 360, 0)


if __name__ == "__main__":
    unittest.main()
