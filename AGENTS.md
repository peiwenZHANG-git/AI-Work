# AGENTS.md

## Scope

These instructions apply to the whole repository. This project is a Windows-only FastMCP server that controls the interactive desktop, so changes must preserve existing behavior and be tested with strict safety boundaries.

## Compatibility requirements

- Keep `windows_gui_mcp.py` as the backward-compatible stdio entry point.
- Preserve the shared server name `windows-gui` and the exported `mcp` object.
- Do not rename, remove, or change the signature or return shape of an existing `@mcp.tool()` without explicit user approval.
- Import every tool module from the entry point so all 25 tools register exactly once.
- Keep PyAutoGUI `FAILSAFE` enabled.
- Preserve the native `SendInput` Unicode path for non-ASCII text and the existing PyAutoGUI path for ASCII text.
- Avoid unrelated formatting or refactors while fixing a targeted issue.

## Code organization

- Put mouse and screenshot behavior in `windows_gui/mouse.py`.
- Put keyboard behavior in `windows_gui/keyboard.py`.
- Put top-level window discovery and focus behavior in `windows_gui/windows.py`.
- Put UI Automation, menus, and save-dialog behavior in `windows_gui/uia.py`.
- Put fixed mailbox identities and Edge Profile launch behavior in `windows_gui/mailboxes.py`.
- Put read-only mailbox page verification, list parsing, summaries, and classification in `windows_gui/mail_summary.py`.
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

## Mailbox safety

- Mailbox configuration may contain only non-secret identity metadata. Never store passwords, Cookie values, sid values, tokens, session URLs, or other credentials.
- Treat the process-local runtime binding created by an explicit `--profile-directory` launch as the primary Profile identity. Never infer Profile identity from page text or general UIA labels.
- Bind a newly launched Edge HWND to exactly one mailbox identity in memory. Do not persist this binding; stop with an unknown-window status if no unique window can be bound.
- `bachelor_mail` and `master_mail` allow READ, DRAFT, and SEND; `qq_mail` allows READ only.
- Every SEND must first create a draft and then wait for explicit user confirmation.
- Never enter passwords automatically.
- Reading may include sender, subject, time, and body only when the user requests it.
- Require explicit user confirmation before deleting, moving, marking, or archiving mail.
- `open_all_mailboxes` is launch-only: it must use `--new-window` and the configured `--profile-directory`, and must not inspect or modify mailbox content.
- Route all mailbox window acquisition through `get_or_open_mailbox_window`; do not duplicate Edge launch logic in tools.
- Reuse a valid runtime HWND first. After process restart, prefer an exact `--profile-directory` found through the Edge window PID and process command line. Because Edge may share one browser PID whose command line omits Profile, an exact configured Profile display-name suffix in the normalized Edge window title is the allowed fallback. Never use page UIA text to infer Profile or log the complete command line.
- Use one per-mailbox lock and launch cooldown so concurrent or repeated calls cannot create duplicate Agent-managed windows.
- Existing duplicate user windows may be inspected for Profile and hostname selection but must not be closed. Prefer the matching service-domain window and bind only one HWND per mailbox.
- Do not invent a mailbox URL. When no stable URL is configured, open only the confirmed Profile and return a clear incomplete-navigation status.
- `summarize_all_mailboxes_today` must process identities in configured order and return `IDENTITY_MISMATCH` before reading mail when the bound Profile and expected service domain disagree.
- Mail summary UIA traversal must remain bounded and read-only. Do not focus, invoke, select, or click a mail row merely to obtain a summary.
- Prefer list metadata summaries so unread state cannot change. If a future implementation opens a body, it must report the possible read-state change explicitly and requires targeted safety tests.
- UIA may read the address bar only inside a bounded worker to extract the hostname. Immediately discard the complete value; never retain, return, log, construct, or reuse a URL containing sid or other session material.

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

Confirm that FastMCP registers exactly the documented 25 tools. The unit tests must fail if a tool is missing or unexpectedly added.

When the task authorizes real desktop testing, run:

```powershell
python tests/smoke_test.py
```

Run explicit mailbox launch and read-only identity/domain smoke coverage only when authorized:

```powershell
python tests/smoke_test.py --mailbox-readonly
```

Report every automated `PASS` and `FAIL` and preserve all `MANUAL CHECK` instructions. A timeout, skipped prerequisite, or native UIA worker exit is a failure and must not be described as success.

## Generated files

Do not commit or rely on generated runtime files:

- `screen.png`
- `tests/smoke_artifacts/`
- `__pycache__/`
- `*.pyc`, `*.pyo`, or other `*.py[cod]` files

Do not remove existing smoke artifacts or screenshots unless the user explicitly requests cleanup.
