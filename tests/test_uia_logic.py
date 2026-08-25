"""Side-effect-free tests for UI Automation tools."""

import time
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from tests.test_windows_gui_mcp import _install_dependency_stubs

_install_dependency_stubs()

from windows_gui import uia


def control(name="", control_type="", automation_id=""):
    item = MagicMock()
    item.element_info = SimpleNamespace(
        name=name, control_type=control_type, automation_id=automation_id
    )
    return item


class UiaLogicTests(unittest.TestCase):
    def test_find_window_and_empty_title(self):
        window = MagicMock()
        window_spec = MagicMock()
        window_spec.wrapper_object.return_value = window
        desktop = MagicMock()
        desktop.window.return_value = window_spec

        def enumerate_windows(callback, extra):
            titles = {41: "Confirm Test - Notepad", 42: "Test - Notepad"}
            for hwnd, title in titles.items():
                with (
                    patch.object(uia.win32gui, "IsWindowVisible", return_value=True),
                    patch.object(uia.win32gui, "GetWindowText", return_value=title),
                ):
                    callback(hwnd, extra)

        with (
            patch.object(uia, "Desktop", return_value=desktop) as desktop_cls,
            patch.object(uia.win32gui, "EnumWindows", side_effect=enumerate_windows),
            patch.object(uia.win32gui, "GetForegroundWindow", return_value=41),
        ):
            self.assertEqual((desktop, window), uia._find_uia_window("Test - Notepad"))
        desktop_cls.assert_called_once_with(backend="uia")
        desktop.window.assert_called_once_with(handle=42)
        with self.assertRaisesRegex(ValueError, "cannot be empty"):
            uia._find_uia_window("")

    def test_list_controls_filters_empty_and_caps_at_150(self):
        window = MagicMock()
        useful = [control(f"item-{index}", "Button", str(index)) for index in range(160)]
        window.descendants.return_value = [control(), *useful]
        with patch.object(uia, "_find_uia_window", return_value=(MagicMock(), window)):
            result = uia.list_controls("target")
        self.assertEqual(150, len(result))
        self.assertEqual(
            {"name": "item-0", "control_type": "Button", "automation_id": "0"},
            result[0],
        )

    def test_click_control_prefers_exact_and_honors_type(self):
        partial = control("Save as", "Button")
        exact_wrong_type = control("Save", "Text")
        exact = control("Save", "Button")
        window = MagicMock()
        window.window_text.return_value = "Dialog"
        window.descendants.return_value = [partial, exact_wrong_type, exact]
        with (
            patch.object(uia, "_find_uia_window", return_value=(MagicMock(), window)),
            patch.object(uia, "_focus_uia_window"),
        ):
            result = uia.click_control("Dialog", "save", "button")
        self.assertIn("using UIA invoke", result)
        exact.invoke.assert_called_once_with()
        partial.invoke.assert_not_called()

    def test_click_control_missing_reports_reason(self):
        window = MagicMock()
        window.descendants.return_value = []
        with (
            patch.object(uia, "_find_uia_window", return_value=(MagicMock(), window)),
            patch.object(uia, "_focus_uia_window"),
        ):
            with self.assertRaisesRegex(ValueError, "No control found"):
                uia.click_control("Dialog", "missing")

    def test_click_menu_item_opens_menu_and_activates_popup_item(self):
        menu = control("View", "MenuItem")
        item = control("Zoom", "MenuItem")
        target = MagicMock()
        target.element_info.handle = 42
        desktop = MagicMock()
        with (
            patch.object(uia, "_find_uia_window", return_value=(desktop, target)),
            patch.object(uia, "_focus_uia_window"),
            patch.object(uia, "_wait_for_menu_control", side_effect=[menu, item]),
            patch.object(uia, "_click_menu_control", return_value="click_input") as click,
        ):
            result = uia.click_menu_item("Notepad", "View", "Zoom")
        self.assertIn("Opened menu 'View'", result)
        self.assertEqual([unittest.mock.call(menu, timeout=3.0, description="opening menu 'View'"),
                          unittest.mock.call(item, timeout=3.0, description="activating menu item 'Zoom'")],
                         click.call_args_list)
        desktop.windows.assert_not_called()
        target.descendants.assert_not_called()

    def test_click_menu_item_missing_menu_or_item(self):
        target = MagicMock()
        target.element_info.handle = 42
        desktop = MagicMock()
        with (
            patch.object(uia, "_find_uia_window", return_value=(desktop, target)),
            patch.object(uia, "_focus_uia_window"),
            patch.object(uia, "_wait_for_menu_control", side_effect=TimeoutError),
        ):
            with self.assertRaisesRegex(ValueError, "Menu not found"):
                uia.click_menu_item("Notepad", "View", "Zoom")

        menu = control("View", "MenuItem")
        with (
            patch.object(uia, "_find_uia_window", return_value=(desktop, target)),
            patch.object(uia, "_focus_uia_window"),
            patch.object(
                uia, "_wait_for_menu_control",
                side_effect=[menu, TimeoutError(), TimeoutError()],
            ),
            patch.object(uia, "_click_menu_control", return_value="click_input"),
        ):
            with self.assertRaisesRegex(ValueError, "Menu item not found"):
                uia.click_menu_item("Notepad", "View", "Zoom")

    def test_bounded_call_times_out_without_waiting_forever(self):
        import threading

        blocker = threading.Event()
        started = time.monotonic()
        with self.assertRaisesRegex(TimeoutError, "while blocked UIA call"):
            uia._run_bounded(
                lambda: blocker.wait(10), 0.05, "blocked UIA call"
            )
        self.assertLess(time.monotonic() - started, 0.5)

    def test_save_filename_fallback_and_validation(self):
        edit = control("File name", "Edit")
        edit.type_keys.side_effect = RuntimeError("unsupported")
        window = MagicMock()
        window.window_text.return_value = "Save As"
        window.descendants.return_value = [edit]
        with (
            patch.object(uia, "_find_uia_window", return_value=(MagicMock(), window)),
            patch.object(uia, "_focus_uia_window"),
        ):
            uia.set_save_dialog_filename("Save As", "report.txt")
        edit.set_focus.assert_called_once_with()
        edit.set_text.assert_called_once_with("report.txt")
        with self.assertRaisesRegex(ValueError, "file_name cannot be empty"):
            uia.set_save_dialog_filename("Save As", "")

    def test_save_filename_escapes_type_keys_metacharacters(self):
        self.assertEqual(
            r"report{(}1{)}{+}{^}{%}{~}{{}{}}.txt",
            uia._escape_type_keys_text("report(1)+^%~{}.txt"),
        )

    def test_save_button_missing(self):
        window = MagicMock()
        window.descendants.return_value = [control("Cancel", "Button")]
        with (
            patch.object(uia, "_find_uia_window", return_value=(MagicMock(), window)),
            patch.object(uia, "_focus_uia_window"),
        ):
            with self.assertRaisesRegex(ValueError, "Save button not found"):
                uia.click_save_button("Save As")


if __name__ == "__main__":
    unittest.main()
