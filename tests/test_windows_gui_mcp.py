"""Unit tests that do not send real mouse or keyboard input."""

import importlib
import sys
import types
import unittest
from unittest.mock import MagicMock, patch


def _install_dependency_stubs() -> None:
    """Allow logic tests to run where Windows GUI dependencies are absent."""
    try:
        import fastmcp  # noqa: F401
        import pyautogui  # noqa: F401
        import win32api  # noqa: F401
        import win32con  # noqa: F401
        import win32gui  # noqa: F401
        import pywinauto  # noqa: F401
        return
    except ImportError:
        pass

    class FastMCP:
        def __init__(self, name):
            self.name = name
            self._tools = {}

        def tool(self):
            def decorate(function):
                self._tools[function.__name__] = function
                return function
            return decorate

        async def get_tools(self):
            return self._tools

        async def list_tools(self):
            return [types.SimpleNamespace(name=name) for name in self._tools]

        def run(self):
            return None

    class Image:
        def __init__(self, path):
            self.path = path

    fastmcp = types.ModuleType("fastmcp")
    fastmcp.FastMCP = FastMCP
    utilities = types.ModuleType("fastmcp.utilities")
    utility_types = types.ModuleType("fastmcp.utilities.types")
    utility_types.Image = Image
    sys.modules.update({
        "fastmcp": fastmcp,
        "fastmcp.utilities": utilities,
        "fastmcp.utilities.types": utility_types,
    })

    pyautogui = types.ModuleType("pyautogui")
    for name in (
        "click", "doubleClick", "dragTo", "hotkey", "moveTo", "press",
        "rightClick", "sleep", "write",
    ):
        setattr(pyautogui, name, MagicMock())
    pyautogui.position = MagicMock(return_value=(0, 0))
    pyautogui.screenshot = MagicMock()
    sys.modules["pyautogui"] = pyautogui

    win32api = types.ModuleType("win32api")
    win32api.mouse_event = MagicMock()
    sys.modules["win32api"] = win32api
    win32con = types.ModuleType("win32con")
    win32con.MOUSEEVENTF_WHEEL = 0x0800
    win32con.SW_RESTORE = 9
    sys.modules["win32con"] = win32con
    win32gui = types.ModuleType("win32gui")
    for name in (
        "BringWindowToTop", "EnumWindows", "GetWindowRect", "GetWindowText",
        "IsIconic", "IsWindowVisible", "SetForegroundWindow", "ShowWindow",
    ):
        setattr(win32gui, name, MagicMock())
    sys.modules["win32gui"] = win32gui
    pywinauto = types.ModuleType("pywinauto")
    pywinauto.Desktop = MagicMock()
    sys.modules["pywinauto"] = pywinauto


_install_dependency_stubs()


EXPECTED_TOOLS = {
    "click_control", "click_menu_item", "click_mouse", "click_save_button",
    "double_click", "drag_mouse", "focus_and_press", "focus_window",
    "focus_window_and_hotkey", "focus_window_and_press",
    "focus_window_and_scroll", "focus_window_and_type",
    "get_mouse_position", "hotkey", "list_controls", "list_windows",
    "move_mouse", "press_key", "right_click", "screenshot", "scroll",
    "set_save_dialog_filename", "type_text", "open_all_mailboxes",
    "summarize_all_mailboxes_today",
}


class InterfaceTests(unittest.IsolatedAsyncioTestCase):
    async def test_all_existing_tools_are_registered(self):
        module = importlib.import_module("windows_gui_mcp")
        if hasattr(module.mcp, "list_tools"):
            tools = await module.mcp.list_tools()
            names = {tool.name for tool in tools}
        else:
            names = set(await module.mcp.get_tools())
        self.assertEqual(EXPECTED_TOOLS, names)

    def test_entrypoint_reexports_existing_functions(self):
        module = importlib.import_module("windows_gui_mcp")
        for name in EXPECTED_TOOLS:
            self.assertTrue(callable(getattr(module, name)), name)


class ValidationTests(unittest.TestCase):
    def test_invalid_mouse_button_is_rejected_before_input(self):
        from windows_gui.mouse import click_mouse

        with patch("windows_gui.mouse.pyautogui.click") as click:
            with self.assertRaisesRegex(ValueError, "left, right, or middle"):
                click_mouse("side")
            click.assert_not_called()

    def test_scroll_bounds_apply_to_both_scroll_tools(self):
        from windows_gui.mouse import scroll
        from windows_gui.windows import focus_window_and_scroll

        with self.assertRaisesRegex(ValueError, "between -100 and 100"):
            scroll(101)
        with self.assertRaisesRegex(ValueError, "between -100 and 100"):
            focus_window_and_scroll("anything", -101)

    def test_empty_window_title_is_rejected(self):
        from windows_gui.windows import focus_window

        with self.assertRaisesRegex(ValueError, "cannot be empty"):
            focus_window("")

    def test_mouse_position_result_is_unchanged(self):
        from windows_gui.mouse import get_mouse_position

        with patch("windows_gui.mouse.pyautogui.position", return_value=(12, 34)):
            self.assertEqual({"x": 12, "y": 34}, get_mouse_position())

    def test_save_dialog_uia_actions_use_edit_and_button_controls(self):
        from windows_gui.uia import click_save_button, set_save_dialog_filename

        window = MagicMock()
        window.window_text.return_value = "另存为"
        edit_control = MagicMock()
        button_control = MagicMock()

        edit_info = MagicMock()
        edit_info.name = "文件名"
        edit_info.control_type = "Edit"
        button_info = MagicMock()
        button_info.name = "保存"
        button_info.control_type = "Button"

        edit_control.element_info = edit_info
        button_control.element_info = button_info
        window.descendants.return_value = [edit_control, button_control]

        with patch(
            "windows_gui.uia._find_uia_window",
            return_value=(MagicMock(), window),
        ):

            self.assertEqual(
                "Set save dialog file name to 'report.txt' in '另存为'",
                set_save_dialog_filename("另存为", "report.txt"),
            )
            edit_control.set_focus.assert_called_once_with()
            self.assertEqual(
                [unittest.mock.call("^a{BACKSPACE}"),
                 unittest.mock.call("report.txt", with_spaces=True)],
                edit_control.type_keys.call_args_list,
            )

            self.assertEqual(
                "Activated save button '保存' in dialog '另存为' using UIA invoke",
                click_save_button("另存为", "保存"),
            )
            button_control.invoke.assert_called_once_with()

    def test_uia_activate_falls_back_to_click_input(self):
        from windows_gui.uia import _activate

        control = MagicMock()
        control.invoke.side_effect = RuntimeError("no InvokePattern")
        self.assertEqual("click_input", _activate(control))
        control.click_input.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
