"""Mouse and screenshot MCP tools."""

from pathlib import Path

import pyautogui
import win32api
import win32con
from fastmcp.utilities.types import Image

from .server import mcp

SCREENSHOT_PATH = Path(__file__).resolve().parent.parent / "screen.png"
VALID_BUTTONS = ("left", "right", "middle")


def _validate_button(button: str) -> None:
    if button not in VALID_BUTTONS:
        raise ValueError("button must be left, right, or middle")


def _validate_scroll_amount(amount: int) -> None:
    if amount < -100 or amount > 100:
        raise ValueError("amount must be between -100 and 100")


@mcp.tool()
def get_mouse_position() -> dict:
    """Get the current Windows mouse cursor position."""
    x, y = pyautogui.position()
    return {"x": x, "y": y}


@mcp.tool()
def move_mouse(x: int, y: int) -> str:
    """Move the Windows mouse cursor to the specified screen coordinates."""
    pyautogui.moveTo(x, y, duration=0.3)
    return f"Mouse moved to ({x}, {y})"


@mcp.tool()
def click_mouse(button: str = "left") -> str:
    """Click the mouse at its current position."""
    _validate_button(button)
    x, y = pyautogui.position()
    pyautogui.click(button=button)
    return f"Clicked {button} mouse button at ({x}, {y})"


@mcp.tool()
def screenshot():
    """Take a screenshot and return it as an image to the AI."""
    image = pyautogui.screenshot()
    image.save(SCREENSHOT_PATH)
    return Image(path=str(SCREENSHOT_PATH))


@mcp.tool()
def double_click(button: str = "left", interval: float = 0.15) -> str:
    """Double-click the mouse at its current position."""
    _validate_button(button)
    x, y = pyautogui.position()
    pyautogui.doubleClick(button=button, interval=interval)
    return f"Double-clicked {button} mouse button at ({x}, {y})"


@mcp.tool()
def right_click() -> str:
    """Right-click the mouse at its current position."""
    x, y = pyautogui.position()
    pyautogui.rightClick()
    return f"Right-clicked at ({x}, {y})"


@mcp.tool()
def scroll(amount: int) -> str:
    """Scroll the mouse wheel; positive is up and negative is down."""
    _validate_scroll_amount(amount)
    win32api.mouse_event(win32con.MOUSEEVENTF_WHEEL, 0, 0, amount * 120, 0)
    return f"Sent Windows wheel event: {amount}"


@mcp.tool()
def drag_mouse(
    x: int, y: int, duration: float = 0.5, button: str = "left"
) -> str:
    """Drag the mouse from its current position to the specified coordinates."""
    _validate_button(button)
    if duration < 0 or duration > 10:
        raise ValueError("duration must be between 0 and 10 seconds")
    start_x, start_y = pyautogui.position()
    pyautogui.dragTo(x, y, duration=duration, button=button)
    return (
        f"Dragged {button} mouse button "
        f"from ({start_x}, {start_y}) to ({x}, {y})"
    )


@mcp.tool()
def focus_and_press(x: int, y: int, key: str, delay: float = 0.3) -> str:
    """Click a screen position to focus it, then press one key."""
    if delay < 0 or delay > 3:
        raise ValueError("delay must be between 0 and 3 seconds")
    pyautogui.moveTo(x, y, duration=0.3)
    pyautogui.click()
    pyautogui.sleep(delay)
    pyautogui.press(key)
    return f"Focused ({x}, {y}) and pressed {key}"


__all__ = [
    "click_mouse", "double_click", "drag_mouse", "focus_and_press",
    "get_mouse_position", "move_mouse", "right_click", "screenshot", "scroll",
]
