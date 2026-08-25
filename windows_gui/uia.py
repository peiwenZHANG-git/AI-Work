"""UI Automation control and menu MCP tools."""

import time
from queue import Queue
from threading import Thread

import win32gui
from pywinauto import Desktop

from .server import mcp


def _find_uia_window(window_title: str):
    if not window_title:
        raise ValueError("window_title cannot be empty")
    exact_matches = []
    partial_matches = []

    def enum_windows(hwnd, _):
        if win32gui.IsWindowVisible(hwnd):
            title = win32gui.GetWindowText(hwnd)
            if title.lower() == window_title.lower():
                exact_matches.append(hwnd)
            elif window_title.lower() in title.lower():
                partial_matches.append(hwnd)

    win32gui.EnumWindows(enum_windows, None)
    matches = exact_matches or partial_matches
    if not matches:
        raise ValueError(f"No UIA window found containing title: {window_title}")

    foreground = win32gui.GetForegroundWindow()
    if foreground in matches:
        matches.remove(foreground)
        matches.insert(0, foreground)

    desktop = Desktop(backend="uia")
    try:
        window = desktop.window(handle=matches[0]).wrapper_object()
    except Exception as error:
        raise ValueError(
            f"Could not access UIA window containing title: {window_title}"
        ) from error
    return desktop, window


def _focus_uia_window(window) -> None:
    try:
        window.set_focus()
        time.sleep(0.3)
    except Exception:
        pass


def _iter_window_controls(window):
    try:
        return [window, *window.descendants()]
    except Exception:
        return [window]


def _activate(control) -> str:
    try:
        control.invoke()
        return "UIA invoke"
    except Exception:
        control.click_input()
        return "click_input"


def _escape_type_keys_text(text: str) -> str:
    """Escape pywinauto type_keys metacharacters for literal text entry."""
    special = "+^%~(){}"
    return "".join(f"{{{char}}}" if char in special else char for char in text)


def _run_bounded(action, timeout: float, description: str):
    """Run a potentially blocking UIA call without blocking the MCP request."""
    outcomes = Queue(maxsize=1)

    def run():
        pythoncom = None
        try:
            try:
                import pythoncom

                pythoncom.CoInitialize()
            except ImportError:
                pythoncom = None
            outcomes.put((True, action()))
        except BaseException as error:
            outcomes.put((False, error))
        finally:
            if pythoncom is not None:
                pythoncom.CoUninitialize()

    worker = Thread(target=run, name=f"uia-{description}", daemon=True)
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        raise TimeoutError(f"Timed out after {timeout:g}s while {description}")
    succeeded, value = outcomes.get_nowait()
    if not succeeded:
        raise value
    return value


def _wait_for_menu_control(specification, timeout: float, description: str):
    def resolve():
        specification.wait(
            "exists visible enabled", timeout=timeout, retry_interval=0.1
        )
        return specification.wrapper_object()

    return _run_bounded(resolve, timeout + 0.5, description)


def _click_menu_control(control, timeout: float, description: str) -> str:
    """Use physical input for WinUI menus because InvokePattern may block."""
    _run_bounded(control.click_input, timeout, description)
    return "click_input"


@mcp.tool()
def list_controls(window_title: str) -> list[dict]:
    """List useful UI Automation controls inside a matching window."""
    _, window = _find_uia_window(window_title)
    results = []
    for control in window.descendants():
        try:
            info = control.element_info
            name = (info.name or "").strip()
            automation_id = info.automation_id or ""
            if not name and not automation_id:
                continue
            results.append({
                "name": name,
                "control_type": info.control_type or "",
                "automation_id": automation_id,
            })
            if len(results) >= 150:
                break
        except Exception:
            continue
    return results


@mcp.tool()
def set_save_dialog_filename(window_title: str, file_name: str) -> str:
    """Set the filename field in a Windows save dialog using UI Automation."""
    if not file_name:
        raise ValueError("file_name cannot be empty")

    _, window = _find_uia_window(window_title)
    _focus_uia_window(window)

    edit_control = None
    file_name_tokens = ("file name", "filename", "\u6587\u4ef6\u540d")
    for control in _iter_window_controls(window):
        try:
            info = control.element_info
            name = (info.name or "").strip().lower()
            ctype = (info.control_type or "").strip().lower()
            if ctype != "edit":
                continue
            if any(token in name for token in file_name_tokens):
                edit_control = control
                break
        except Exception:
            continue

    if edit_control is None:
        for control in _iter_window_controls(window):
            try:
                info = control.element_info
                if (info.control_type or "").strip().lower() == "edit":
                    edit_control = control
                    break
            except Exception:
                continue

    if edit_control is None:
        raise ValueError(
            f"File name input not found in save dialog containing title: {window_title}"
        )

    try:
        edit_control.set_focus()
        edit_control.type_keys("^a{BACKSPACE}")
        edit_control.type_keys(
            _escape_type_keys_text(file_name), with_spaces=True
        )
    except Exception:
        edit_control.set_text(file_name)

    return f"Set save dialog file name to '{file_name}' in '{window.window_text()}'"


@mcp.tool()
def click_save_button(window_title: str, button_name: str = "\u4fdd\u5b58") -> str:
    """Click the save button in a Windows save dialog using UI Automation."""
    _, window = _find_uia_window(window_title)
    _focus_uia_window(window)

    button = None
    target_names = {button_name.lower(), "save", "\u4fdd\u5b58"}
    for control in _iter_window_controls(window):
        try:
            info = control.element_info
            name = (info.name or "").strip().lower()
            ctype = (info.control_type or "").strip().lower()
            if ctype not in {"button", "splitbutton"}:
                continue
            if name in target_names or any(token in name for token in ("save", "\u4fdd\u5b58")):
                button = control
                break
        except Exception:
            continue

    if button is None:
        raise ValueError(
            f"Save button not found in save dialog containing title: {window_title}"
        )

    method = _activate(button)
    return (
        f"Activated save button '{button_name}' in dialog "
        f"'{window.window_text()}' using {method}"
    )


@mcp.tool()
def click_control(
    window_title: str, control_name: str, control_type: str = ""
) -> str:
    """Find a UI Automation control by name and activate it."""
    _, window = _find_uia_window(window_title)
    _focus_uia_window(window)
    exact_matches = []
    partial_matches = []
    for control in window.descendants():
        try:
            info = control.element_info
            name = (info.name or "").strip()
            ctype = info.control_type or ""
            if control_type and ctype.lower() != control_type.lower():
                continue
            if name.lower() == control_name.lower():
                exact_matches.append(control)
            elif control_name.lower() in name.lower():
                partial_matches.append(control)
        except Exception:
            continue
    matches = exact_matches or partial_matches
    if not matches:
        raise ValueError(
            f"No control found: name='{control_name}', "
            f"type='{control_type or 'any'}'"
        )
    method = _activate(matches[0])
    return (
        f"Activated control '{control_name}' "
        f"in window '{window.window_text()}' using {method}"
    )


@mcp.tool()
def click_menu_item(window_title: str, menu_name: str, item_name: str) -> str:
    """Open a menu in a visible window, then activate a menu item by name."""
    desktop, target_window = _find_uia_window(window_title)
    _focus_uia_window(target_window)
    handle = target_window.element_info.handle
    window_spec = desktop.window(handle=handle)

    try:
        menu_control = _wait_for_menu_control(
            window_spec.child_window(title=menu_name, control_type="MenuItem"),
            timeout=3.0,
            description=f"finding menu '{menu_name}'",
        )
    except Exception as error:
        raise ValueError(f"Menu not found: {menu_name}") from error

    _click_menu_control(
        menu_control, timeout=3.0, description=f"opening menu '{menu_name}'"
    )

    item_specs = [
        window_spec.child_window(title=item_name, control_type="MenuItem"),
        desktop.window(
            title=item_name, control_type="MenuItem", top_level_only=False
        ),
    ]
    item_control = None
    errors = []
    for item_spec in item_specs:
        try:
            item_control = _wait_for_menu_control(
                item_spec,
                timeout=3.0,
                description=f"finding menu item '{item_name}'",
            )
            break
        except Exception as error:
            errors.append(error)
    if item_control is None:
        raise ValueError(
            f"Menu item not found after opening '{menu_name}': {item_name}"
        ) from errors[-1]

    method = _click_menu_control(
        item_control,
        timeout=3.0,
        description=f"activating menu item '{item_name}'",
    )
    return (
        f"Opened menu '{menu_name}' and activated "
        f"'{item_name}' using {method}"
    )


__all__ = [
    "click_control",
    "click_menu_item",
    "click_save_button",
    "list_controls",
    "set_save_dialog_filename",
]
