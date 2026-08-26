"""Side-effect-free tests for mouse, screenshot, and keyboard tools."""

import ctypes
import unittest
from unittest.mock import MagicMock, patch

from tests.test_windows_gui_mcp import _install_dependency_stubs

_install_dependency_stubs()

from windows_gui import keyboard, mouse


class MouseToolTests(unittest.TestCase):
    def test_move_click_double_right_and_drag(self):
        with (
            patch.object(mouse.pyautogui, "position", return_value=(10, 20)),
            patch.object(mouse.pyautogui, "moveTo") as move_to,
            patch.object(mouse.pyautogui, "click") as click,
            patch.object(mouse.pyautogui, "doubleClick") as double_click,
            patch.object(mouse.pyautogui, "rightClick") as right_click,
            patch.object(mouse.pyautogui, "dragTo") as drag_to,
        ):
            self.assertEqual("Mouse moved to (30, 40)", mouse.move_mouse(30, 40))
            move_to.assert_called_once_with(30, 40, duration=0.3)
            self.assertIn("Clicked left", mouse.click_mouse())
            click.assert_called_once_with(button="left")
            self.assertIn("Double-clicked middle", mouse.double_click("middle", 0.2))
            double_click.assert_called_once_with(button="middle", interval=0.2)
            self.assertEqual("Right-clicked at (10, 20)", mouse.right_click())
            right_click.assert_called_once_with()
            self.assertIn("from (10, 20) to (50, 60)", mouse.drag_mouse(50, 60))
            drag_to.assert_called_once_with(50, 60, duration=0.5, button="left")

    def test_screenshot_saves_to_configured_path(self):
        captured = MagicMock()
        with patch.object(mouse.pyautogui, "screenshot", return_value=captured):
            result = mouse.screenshot()
        captured.save.assert_called_once_with(mouse.SCREENSHOT_PATH)
        self.assertEqual(str(mouse.SCREENSHOT_PATH), str(result.path))

    def test_scroll_sends_native_wheel_delta(self):
        with patch.object(mouse.win32api, "mouse_event") as event:
            self.assertEqual("Sent Windows wheel event: -2", mouse.scroll(-2))
        event.assert_called_once_with(mouse.win32con.MOUSEEVENTF_WHEEL, 0, 0, -240, 0)

    def test_focus_and_press_sequence_and_validation(self):
        with (
            patch.object(mouse.pyautogui, "moveTo") as move_to,
            patch.object(mouse.pyautogui, "click") as click,
            patch.object(mouse.pyautogui, "sleep") as sleep,
            patch.object(mouse.pyautogui, "press") as press,
        ):
            self.assertIn("pressed tab", mouse.focus_and_press(1, 2, "tab", 0.1))
            move_to.assert_called_once_with(1, 2, duration=0.3)
            click.assert_called_once_with()
            sleep.assert_called_once_with(0.1)
            press.assert_called_once_with("tab")
        for delay in (-0.1, 3.1):
            with self.assertRaises(ValueError):
                mouse.focus_and_press(1, 2, "tab", delay)

    def test_drag_validation(self):
        for duration in (-0.1, 10.1):
            with self.assertRaises(ValueError):
                mouse.drag_mouse(1, 2, duration=duration)


class KeyboardToolTests(unittest.TestCase):
    def test_native_input_structure_has_windows_union_size(self):
        expected = 40 if ctypes.sizeof(ctypes.c_void_p) == 8 else 28
        self.assertEqual(expected, ctypes.sizeof(keyboard._INPUT))

    def test_type_text_and_hotkey(self):
        with (
            patch.object(keyboard.pyautogui, "write") as write,
            patch.object(keyboard.pyautogui, "hotkey") as hotkey,
        ):
            self.assertEqual("Typed 3 characters", keyboard.type_text("abc", 0.2))
            write.assert_called_once_with("abc", interval=0.2)
            self.assertIn("ctrl + s", keyboard.hotkey(["ctrl", "s"]))
            hotkey.assert_called_once_with("ctrl", "s")
        with self.assertRaises(ValueError):
            keyboard.type_text("x", 1.1)
        with self.assertRaises(ValueError):
            keyboard.hotkey([])

    def test_type_text_supports_unicode_without_clipboard(self):
        samples = (
            "你好，Codex",
            "English 与中文 mixed",
            "é à ç",
            "日本語 한국어",
            "🙂",
            "第一行\nDeuxième ligne 🙂",
        )
        for sample in samples:
            with self.subTest(sample=sample):
                with (
                    patch.object(keyboard.pyautogui, "write") as write,
                    patch.object(keyboard, "_send_unicode_code_unit") as send_unicode,
                    patch.object(keyboard, "_send_virtual_key") as send_virtual,
                    patch.object(keyboard.time, "sleep"),
                ):
                    self.assertEqual(
                        f"Typed {len(sample)} characters",
                        keyboard.type_text(sample, 0.01),
                    )
                    write.assert_not_called()
                    self.assertTrue(send_unicode.called)
                    if "\n" in sample:
                        send_virtual.assert_called_with(keyboard.KEY_MAP["enter"])

    def test_emoji_is_sent_as_utf16_surrogate_pair(self):
        with patch.object(keyboard, "_send_unicode_code_unit") as send_unicode:
            keyboard._send_unicode_text("🙂", 0)
        self.assertEqual(
            [
                unittest.mock.call(0xD83D),
                unittest.mock.call(0xD83D, key_up=True),
                unittest.mock.call(0xDE42),
                unittest.mock.call(0xDE42, key_up=True),
            ],
            send_unicode.call_args_list,
        )

    def test_press_key_down_and_up(self):
        user32 = MagicMock()
        with (
            patch.object(keyboard.ctypes, "windll", create=True) as windll,
            patch.object(keyboard.time, "sleep") as sleep,
        ):
            windll.user32 = user32
            self.assertEqual("Pressed key: enter", keyboard.press_key("ENTER"))
        self.assertEqual(
            [unittest.mock.call(0x0D, 0, 0, 0), unittest.mock.call(0x0D, 0, 2, 0)],
            user32.keybd_event.call_args_list,
        )
        sleep.assert_called_once_with(0.1)
        with self.assertRaisesRegex(ValueError, "Unsupported key"):
            keyboard.press_key("f24")


if __name__ == "__main__":
    unittest.main()
