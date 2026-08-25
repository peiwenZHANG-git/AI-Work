"""Shared FastMCP server instance and process-wide safety settings."""

import pyautogui
from fastmcp import FastMCP

mcp = FastMCP("windows-gui")

# Moving the pointer quickly to the upper-left corner aborts PyAutoGUI actions.
pyautogui.FAILSAFE = True
pyautogui.PAUSE = 0.2
