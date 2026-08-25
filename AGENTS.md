# AGENTS.md

## Scope

These instructions apply to the whole repository. This project is a Windows-only FastMCP server that controls the interactive desktop, so changes must preserve existing behavior and be tested with strict safety boundaries.

## Compatibility requirements

- Keep `windows_gui_mcp.py` as the backward-compatible stdio entry point.
- Preserve the shared server name `windows-gui` and the exported `mcp` object.
- Do not rename, remove, or change the signature or return shape of an existing `@mcp.tool()` without explicit user approval.
- Import every tool module from the entry point so all 23 tools register exactly once.
- Keep PyAutoGUI `FAILSAFE` enabled.
- Avoid unrelated formatting or refactors while fixing a targeted issue.

## Code organization

- Put mouse and screenshot behavior in `windows_gui/mouse.py`.
- Put keyboard behavior in `windows_gui/keyboard.py`.
- Put top-level window discovery and focus behavior in `windows_gui/windows.py`.
- Put UI Automation, menus, and save-dialog behavior in `windows_gui/uia.py`.
- Keep the FastMCP instance and process-wide settings in `windows_gui/server.py`.
- Re-export public tools from `windows_gui_mcp.py` for import compatibility.

## Safety rules

- Treat all real mouse, keyboard, window-focus, menu, and UIA calls as desktop side effects.
- Unit tests must mock desktop side effects and must not move the mouse, type, click, focus windows, or open dialogs.
- For real GUI validation, use only `tests/smoke_test.py` and its uniquely named Notepad fixture under `tests/smoke_artifacts/`.
- Never use a user's existing document, browser tab, editor buffer, email, chat, or save dialog as a test target.
- Never delete files, send messages, close applications, confirm destructive dialogs, or overwrite an existing path during smoke testing.
- Leave smoke-test Notepad windows open for `MANUAL CHECK` unless the user explicitly requests cleanup.
- Keep potentially blocking UIA operations bounded. Do not restore full-Desktop `windows()` plus `descendants()` traversal for popup menus.
- Prefer exact window-title matches before substring matches so confirmation dialogs are not confused with their parent dialogs.

## Required verification

Use the same Python interpreter that has `fastmcp`, `pyautogui`, `pywin32`, `pywinauto`, and `pillow` installed.

Run syntax checks:

```powershell
python -m compileall -q windows_gui_mcp.py windows_gui tests
```

Run the complete side-effect-free test suite:

```powershell
python -m unittest discover -s tests -t . -v
```

Confirm that FastMCP registers exactly the documented 23 tools. The unit tests must fail if a tool is missing or unexpectedly added.

When the task authorizes real desktop testing, run:

```powershell
python tests/smoke_test.py
```

Report every automated `PASS` and `FAIL` and preserve all `MANUAL CHECK` instructions. A timeout, skipped prerequisite, or native UIA worker exit is a failure and must not be described as success.

## Generated files

Do not commit or rely on generated runtime files:

- `screen.png`
- `tests/smoke_artifacts/`
- `__pycache__/`
- `*.pyc`, `*.pyo`, or other `*.py[cod]` files

Do not remove existing smoke artifacts or screenshots unless the user explicitly requests cleanup.
