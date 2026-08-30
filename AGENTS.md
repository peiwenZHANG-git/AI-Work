# AGENTS.md

## Scope

These instructions apply to the whole repository. This project is a Windows-only FastMCP server that controls the interactive desktop, so changes must preserve existing behavior and be tested with strict safety boundaries.

## Project state maintenance

- Before starting a significant development task, read `PROJECT_STATE.md` to restore current project context.
- After completing work that changes goals, architecture, features, backend behavior, known issues, or the verification baseline, update `PROJECT_STATE.md` so it reflects the real repository state.
- Verify `PROJECT_STATE.md` claims against the working tree, Git history, and required verification output before recording them; do not rely on conversation memory alone.
- Record only what a future AI/Codex session needs to resume work; do not log every minor edit.
- Keep the project name and repository path at the top of `PROJECT_STATE.md`, and update its last-updated date whenever its content changes.

## Compatibility requirements

- Keep `windows_gui_mcp.py` as the backward-compatible stdio entry point.
- Preserve the shared server name `windows-gui` and the exported `mcp` object.
- Do not rename, remove, or change the signature or return shape of an existing `@mcp.tool()` without explicit user approval.
- Import every tool module from the entry point so all 28 tools register exactly once.
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
- Put QQ/NetEase Browser DOM/CDP transport and sanitized list parsing in `windows_gui/browser_mail.py`.
- Put shared QQ/NetEase IMAP READ-only transport and header parsing in `windows_gui/imap_mail.py`.
- Put backend-neutral mailbox search dispatch and safe result references in `windows_gui/mail_search.py`.
- Put backend-neutral mailbox draft creation and no-send safety checks in `windows_gui/mail_draft.py`.
- Put confirmed sending of existing mailbox drafts in `windows_gui/mail_send.py`.
- Put shared digest collection, Outlook refresh rotation, GLM enrichment, and digest rendering in `windows_gui/mail_digest.py`.
- Put one-time Outlook authorization-code login, PKCE, and loopback callback validation in `windows_gui/master_oauth.py`; expose it through `scripts/authenticate_master_mail.py`, not a FastMCP tool.
- Put natural-language draft generation and local assistant draft/SMTP actions in `windows_gui/mail_assistant.py`; put the loopback HTTP UI in `scripts/mail_assistant_server.py`.
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
- `bachelor_mail` and `master_mail` allow READ, DRAFT, and SEND; `qq_mail` allows READ and DRAFT but never SEND.
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
- For `qq_mail` and `bachelor_mail`, UIA may verify only window/Profile/domain/login readiness. Never restore UIA mail-row reconstruction; when the opt-in browser fallback is used, route list metadata through `BrowserDomReadonlyBackend`.
- Prefer `QqImapReadonlyBackend` for QQ summaries. It must use SSL on `imap.qq.com:993`, EXAMINE/read-only selection, UID commands, and BODY.PEEK headers only; never call STORE, MOVE, COPY, or EXPUNGE.
- Store the QQ authorization code only in the dedicated Windows Credential Manager entry `AI-Work/windows-gui/mailboxes` / `qq_mail_imap_authorization_code`. Never reuse the Graph token entry or use this credential for Draft or Send.
- Prefer `BachelorImapReadonlyBackend` for bachelor summaries using SSL on `imaphz.qiye.163.com:993` with system CA and hostname verification. Read its username from `AI_WORK_BACHELOR_IMAP_USERNAME` and its authorization code only from `AI-Work/windows-gui/mailboxes` / `bachelor_mail_imap_authorization_code`; never reuse QQ/Graph credentials or use it for Search, Draft, Send, or SMTP.
- The local assistant must use separate Credential Manager usernames under the same service: `qq_mail_assistant_draft_authorization_code`, `bachelor_mail_assistant_draft_authorization_code`, and `bachelor_mail_assistant_smtp_authorization_code`. A missing assistant credential is an explicit configuration error; never fall back to read-only summary credentials.
- Outlook interactive login must use authorization code + PKCE, bind only to `127.0.0.1`, require exact `/callback` path and Host, verify OAuth `state`, and never print or retain authorization codes or tokens. Its token exchange and refresh-token writeback must use the same cross-process Graph refresh lock as automated refresh. Only the rotated refresh token is written to `master_mail_graph_refresh_token`; failed token exchange must not overwrite or erase the existing entry. Browser launch is allowed only through the explicit login command; unit tests must mock it.
- CDP attachment is opt-in and loopback-only. Never automatically close or restart Edge, reuse a locked daily Profile in a second process, copy a Profile, expose a debugging port to the LAN, or log a CDP websocket URL.
- Browser DOM extraction may return only sender, subject, received time, and a hashed opaque reference. It must not query body content, click rows, mutate read state, or access cookies/local storage/session tokens.
- Prefer list metadata summaries so unread state cannot change. If a future implementation opens a body, it must report the possible read-state change explicitly and requires targeted safety tests.
- Never translate zero parsed rows directly into zero messages. Use `MAIL_LIST_NOT_FOUND` when no trustworthy list container is exposed and `MAIL_ITEMS_NOT_PARSED` when rows exist but parsing fails. Return `EMPTY_TODAY` only after at least one mail row was parsed and none belongs to today.
- Mail diagnostics may report control-type and structural counts only. Do not include sender, subject, body text, complete URLs, or session material in diagnostic output.
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

Confirm that FastMCP registers exactly the documented 28 tools. The unit tests must fail if a tool is missing or unexpectedly added.

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
