# Windows GUI MCP Server

English | [中文](README.zh-CN.md)

A Windows-only FastMCP server that gives AI clients mouse, keyboard, window focus, screenshot, Windows UI Automation, and menu control capabilities.

`windows_gui_mcp.py` is kept as the backward-compatible entry point; the implementation lives in the `windows_gui/` package, organized by responsibility:

- `server.py`: the shared FastMCP instance and PyAutoGUI safety settings.
- `mouse.py`: mouse movement, scroll wheel, dragging, and screenshots.
- `keyboard.py`: text input, key presses, and hotkeys.
- `windows.py`: window enumeration, focusing, and input after focusing.
- `uia.py`: UI Automation controls, menus, and Save dialog operations.
- `mail_backends.py`: a unified mailbox backend abstraction with Graph and Edge adapters; summary and search stay READ-only, and drafts are saved but never sent.
- `browser_mail.py`: a READ-only Browser DOM/CDP summary adapter for QQ Mail and the undergraduate NetEase mailbox; it extracts list metadata only.
- `imap_mail.py`: a standard-library IMAP READ-only summary adapter shared by QQ Mail and the undergraduate NetEase mailbox; it uses SSL, EXAMINE, UIDs, and BODY.PEEK.
- `mailboxes.py`: fixed mailbox identities, permission boundaries, and Edge profile launch logic.
- `mail_search.py`: unified READ-only mail search, backend dispatch, and safe result references.
- `mail_draft.py`: unified draft creation, Graph/Edge backend dispatch, and no-send safety checks.
- `mail_send.py`: unified sending of existing drafts, with explicit confirmation and pre-send metadata validation.
- `mail_summary.py`: mailbox identity and page verification, read-only list parsing, daily summaries, and important-item classification.
- `mail_digest.py`: scheduled-task digests, Outlook refresh-token rotation, GLM summarization/translation, and local HTML rendering.
- `master_oauth.py` / `scripts/authenticate_master_mail.py`: one-time Outlook OAuth sign-in and secure storage of the refresh token.
- `mail_assistant.py` / `scripts/mail_assistant_server.py`: the local AI-Work unified entry point and the backward-compatible mail assistant page, bound only to `127.0.0.1:8931`.
- `workflows.py` / `orchestrator.py` / `mcp_executor.py`: five fixed intents, bounded task orchestration, a single executor, and a long-lived stdio MCP subprocess.
- `activity_history.py` / `tray.py`: bounded, redacted activity history, the tray entry point, and the fixed `Win+Alt+A` hotkey.
- `health_events.py` / `system_health.py`: bounded, redacted health events and the four-state read-only health model shared with the assistant page.
- `scripts/configure_mail_credentials.py`: interactively writes allowlisted credentials; input is not echoed, and secrets are never passed via the command line or logs.
- `scripts/system_health.py`: a local read-only health check that verifies configuration/credential presence, MCP registration, scheduled tasks, the assistant service, and the status of the latest digest run.
- `scripts/install_scheduled_tasks.py`: idempotently restores the daily digest scheduled task; it only registers the task and never triggers mail reading.
- `scripts/install_agent_startup.py`: generates, checks (read-only), or explicitly installs the current user's AI-Work sign-in startup definition.

## Requirements

- Windows 10 or Windows 11
- Python 3.10 or later
- An interactive desktop session

Install dependencies:

```powershell
python -m pip install fastmcp pyautogui pywin32 pywinauto pillow requests keyring
```

The QQ / NetEase Browser DOM summary also needs the optional `playwright` dependency. This adapter only uses
`connect_over_cdp` to attach to an Edge instance where the user has explicitly enabled remote debugging; it never downloads or launches a new browser:

```powershell
python -m pip install playwright
```

## General web pages and file downloads

General browser capabilities are exposed both as a CLI and as semantic MCP tools. Opening a web page creates a new
Edge window. Opening and downloading both resolve and check the target host address, rejecting localhost, private,
link-local, and other non-public addresses; public downloads disable automatic redirects and validate each redirect
hop. Downloads allow HTTPS only by default, never overwrite existing files, and are written atomically through a
temporary file; the result includes the size and SHA-256:

```powershell
python scripts/browser_download.py open https://example.com
python scripts/browser_download.py download https://example.com/report.pdf D:\Downloads
```

Use `--filename` to specify a safe filename and `--max-bytes` to change the default 256 MiB limit. Use `--allow-http`
or `--overwrite` only when explicitly needed. The target directory must already exist. Cookies are never copied into
this downloader for signed-in pages; signed-in downloads use the controlled browser session described below.

The persistent session is owned by a dedicated worker thread and uses a separate profile directory at
`%LOCALAPPDATA%\AI-Work\browser-agent-profile`; it does not reuse any of the three mailbox profiles. The tools start a
session, navigate, inspect the page, click a uniquely matched element, save a browser download, and stop the session.
Playwright validates DNS/private-network boundaries per request at the context level; inspection results strip URL
query strings and fragments and never return cookies or input values; clicking buttons and form controls requires
explicit confirmation. Signed-in downloads keep the 256 MiB limit, do not overwrite existing files by default, and
clean up temporary files on failure or when the limit is exceeded.

New MCP tools: `open_webpage`, `download_web_file`, `start_browser_session`,
`navigate_browser`, `inspect_browser`, `click_browser_element`,
`download_browser_element`, `stop_browser_session`. The server currently registers exactly 42 tools.

PyAutoGUI's fail-safe is enabled. Quickly moving the mouse to the top-left corner of the screen aborts PyAutoGUI actions.

## v1 Goal A: Local files and apps

Goal A adds `inspect_path`, `open_path`, `manage_path`, and `open_app`, bringing the total to 40 at that stage; the total is 42 after Goal B.
The shared path policy lives in `windows_gui/local_paths.py`, file operations in `files.py`, and app launching in `applications.py`.
Dependencies are the existing pywin32, FastMCP, and its Pydantic 2; no new services, indexes, or background tasks are needed.

The following are MCP tool arguments, not shell commands. Paths use the Windows Known Folders aliases
`Downloads/...` and `Documents/...`, or absolute paths inside those roots; return values only contain root-relative paths.
Do not hard-code the user profile. Use the actual filename casing returned by Windows; case-ambiguous paths are rejected.
The Desktop, network shares, removable drives, reparse points/OneDrive placeholder folders, and hard links are out of scope for this version.

| Task | Tool | Arguments |
|---|---|---|
| Open VS Code | `open_app` | `{"app":"vscode"}` |
| Find the newest PDF in Downloads | `inspect_path` | `{"request":{"operation":"search","path":"Downloads","extension":".pdf","max_depth":0,"sort":"modified_desc","limit":1}}` |
| Open the path returned by the previous step | `open_path` | `{"path":"Downloads/report.pdf"}` |
| Create a course folder | `manage_path` | `{"request":{"operation":"mkdir","path":"Documents/HCI"}}` |
| Move a specific file on the same volume | `manage_path` | `{"request":{"operation":"move","source":"Downloads/report.pdf","destination":"Documents/HCI/report.pdf"}}` |
| Copy a regular file | `manage_path` | `{"request":{"operation":"copy","source":"Downloads/report.pdf","destination":"Documents/report-copy.pdf"}}` |
| Rename the basename only | `manage_path` | `{"request":{"operation":"rename","source":"Documents/report-copy.pdf","new_name":"reading.pdf"}}` |
| Explicitly read text | `inspect_path` | `{"request":{"operation":"read_text","path":"Documents/notes.txt","encoding":"utf-8","max_chars":16000}}` |

The `inspect_path` request is a strict per-operation union that rejects fields that do not apply. `stat` returns only
type, size, and modification time; `list` covers a single directory; `search` has `max_depth` defaulting to 2 with a
range of 0–5, where 0 searches only the given directory. The extension filter is a simple case-insensitive suffix such
as `.pdf`; there are no arbitrary globs. Sorting is by `name` (default) or `modified_desc`; `limit` defaults to 100 with
a maximum of 200. A scan covers at most 10,000 entries with a cooperative 3-second time budget. Results are scanned and
sorted first, then limited; `results_truncated=true` only means the output count was trimmed, while
`partial=true`/`scan_complete=false` means the scan was incomplete (including permissions, reparse points, and
time/count limits), so it must not claim to have found the newest item across the full scope.
`latest_in_scope_verified=true` only means ordering within the fully observed scope; it is not a transactional snapshot of a concurrent file system.

Text files are limited to 1 MiB; by default up to 16,000 characters are returned, configurable up to 64,000. Exceeding
the character limit returns `truncated=true`; exceeding the file size limit is rejected. UTF-8 is decoded strictly and
accepts a BOM; UTF-16 requires a BOM; GB18030 must be selected explicitly. The entire bounded file is validated,
rejecting binary control characters and invalid encodings, including in the tail beyond the returned character range;
content is never cached, logged, or written to the audit log. Returned text enters the calling client's context.

`manage_path`'s `mkdir` creates only one level, and the parent directory must exist. Copy is limited to 256 MiB with a
cooperative 15-second budget, implemented by exclusively creating a temporary object and publishing it with an atomic
no-replace operation; on failure, only the temporary handles owned by this operation are cleaned up.
Move accepts only regular files on the same volume; cross-volume moves always return `cross_volume_not_supported` and
never fall back to copy+delete. Rename accepts only a new basename and cannot be used to change the parent directory.
Every operation fails if the target already exists; `overwrite`, `replace`, `delete`, recursive operations, and ACL
modification parameters are not accepted.

In this first version, `open_path` only allows `.pdf/.txt/.md/.png/.jpg/.jpeg/.bmp`, and directories are handed to Explorer.
Office files are not yet supported; executables, scripts, `.lnk/.url`, and other redirecting types are always rejected.
Documents are opened with the fixed `open` verb of the Windows association API, and apps are launched with an argv list
and `shell=False`; no shell command strings are assembled. `open_app` only accepts
`notepad/calculator/explorer/edge/vscode`, resolved from fixed install locations under system directories/Known Folders.
If an app is not installed it returns `app_not_installed`; it never searches an arbitrary PATH or launches an interpreter.

All four tools return a fixed `status` and `code` and never return raw exceptions; common errors include
`invalid_request`, `invalid_path`, `outside_allowed_roots`, `not_found`,
`reparse_point_not_supported`, `path_changed`, `destination_exists`, `file_busy`, and
`permission_denied`. The success codes for opening/launching are `open_requested`/`launch_requested`, which only
confirm the operation was handed to the system, not that a window is ready.

### Path races and residual risk

Each component of the parent chain is opened by handle without allowing delete sharing; source files deny write/delete
sharing while being read or moved. Attribute-read handles do not provide sufficient Windows sharing locks, so
read-data/list-directory access is actually requested. File management uses parent-relative `NtCreateFile` +
`OBJ_DONT_REPARSE`, and publishing uses a parent-relative no-replace rename via `NtSetInformationFile`; native failures
stop immediately, without relaxing locks to retry or falling back to a full-path rename. Native junctions, parent
directory replacement after checks, concurrent target creation, and last-moment reparse changes all have dedicated fixture tests.

The security implementation follows Microsoft's [NtCreateFile](https://learn.microsoft.com/en-us/windows/win32/api/winternl/nf-winternl-ntcreatefile)
and [NtSetInformationFile](https://learn.microsoft.com/en-us/windows-hardware/drivers/ddi/ntifs/nf-ntifs-ntsetinformationfile)
contracts. It is not comprehensive isolation from administrators, the kernel, malicious file system drivers, or processes running as the same user.
Time budgets are checked at loop boundaries and cannot forcibly interrupt kernel I/O that is already blocked; a complete scan is not a transactional snapshot either.
Native behavior is verified on local Windows/NTFS; other file systems or unusual sharing locks may be safely rejected.
File associations and known install paths are trusted local configuration and cannot prove that document/app content is not malicious.
File-open handles are held only until the system dispatch completes, so the target may still change before the app reads it asynchronously;
this is why results say "requested" rather than claiming the app read verified content. Do not automatically open untrusted downloads.

### Goal A smoke tests

First run the full automated verification, then, in an authorized desktop session, run:

```powershell
python tests/smoke_test.py --local-files
python tests/smoke_test.py --local-files-open
```

Both only touch the `tests/smoke_artifacts/local-files-<uuid>/` created by the current run; the test-only root is
injected in-process only and does not change the production Known Folders. The first verifies querying, creating
folders, copying, moving, renaming, no-overwrite behavior, and content preservation; the second additionally opens a
self-created harmless text file in the fixed Notepad and leaves the window open for `MANUAL CHECK`, without clicking,
typing, or closing the app. It never touches the user's Downloads and never opens the PDF-equivalent selection file as a PDF.
Document association and app resolution/launch are tested separately via injection; this does not claim that a real VS Code or PDF viewer has been accepted.

Goal A keeps 40 tools. The user subsequently approved Goal B adding clipboard/get_system_status for a total of 42,
followed by Goal C's nine-demo acceptance and feature freeze; Browser/Mail/Remote do not expand.

## v1 Goal B: Clipboard and system status

This stage only adds `clipboard` and `get_system_status`, for a total of exactly 42; the previous 36 tools remain compatible.

```json
{"request":{"operation":"read","max_chars":16000}}
```

The example above calls `clipboard` to explicitly read the current Unicode text; at most 64,000 characters, returning `truncated`.
Native memory objects over 256 KiB, binary formats, and invalid Unicode are rejected. Content never enters server logs,
the audit log, caches, or files, but the requesting client may retain the tool result, so do not read passwords or tokens unintentionally.

```json
{"request":{"operation":"write","text":"Harmless course note"}}
```

Writing replaces the clipboard and returns only the character count and status, without echoing the text. The limit is
64,000 characters; NUL and invalid surrogates are rejected. Memory is registered/prepared first, then the clipboard is
cleared; Windows' three
[history and cloud-sync exclusion formats](https://learn.microsoft.com/en-us/windows/win32/dataxchg/clipboard-formats)
are placed first, and then the content is published. If setting the markers fails, the content is not published; a failure after clearing may leave an empty clipboard.
It never silently reads or backs up previous content, and never auto-pastes, listens in the background, or clears the user's history.
These markers are Windows' exclusion mechanism and cannot constrain arbitrary third-party clipboard managers or the requesting client.

`get_system_status` takes no parameters and returns the foreground HWND/title (up to 256 characters), battery status,
free/total bytes of fixed local volumes, primary and virtual screen sizes, and the mouse position. Coordinates follow the process's DPI awareness.
No battery is reported as `not_present`, unknown/failed as `unknown`/`query_failed`, with an overall `partial`;
an unknown charge level is never disguised as 0%. It does not focus windows, read the clipboard, probe the network, or read credentials or command lines.
Results are observations from consecutive queries, not an atomic snapshot; the title is returned only to the caller and never logged.

Real read-only status smoke test:

```powershell
python tests/smoke_test.py --system-status
```

This smoke test only outputs component verification status and never outputs or saves window titles. All clipboard unit tests use fake/native
API mocks and do not pretend to test the real system clipboard. `python tests/smoke_test.py --clipboard-owner` only verifies the real hidden owner/locking/closing, without reading or writing content, and cannot replace a read/write smoke test. The local Windows refuses to create a separate Window Station
(Access denied), so the real write smoke test is not run until explicit confirmation to replace the current clipboard is obtained.
The nine real demos and the final freeze belong to the next stage, Goal C; items that have not been run are not marked PASS.

## Running

From the project root, run:

```powershell
python windows_gui_mcp.py
```

The VS Code MCP configuration is in `.vscode/mcp.json` and launches the same compatible entry point over stdio.

## Testing

Run all unit tests that do not touch the real desktop:

```powershell
python -m unittest discover -s tests -t . -v
```

Run the syntax compilation check:

```powershell
python -m compileall -q windows_gui_mcp.py windows_gui tests scripts
```

Run the real Windows GUI smoke test:

```powershell
python tests/smoke_test.py
```

The smoke test only uses a uniquely named, dedicated Notepad file, and writes results to `tests/smoke_artifacts/`. It never deletes files, sends messages, closes programs, or touches existing documents; Notepad is left on the desktop for manual confirmation. `MANUAL CHECK` in the log means a screenshot or desktop state needs to be observed.

## MCP tools

The server currently registers 42 tools; the names, parameters, and return structures of the 36 tools that existed before Goal A are unchanged.

| Tool | Purpose |
|---|---|
| `clipboard` | Explicit, text-only clipboard read/write; content is never logged, and writes are excluded from history/cloud sync. |
| `get_system_status` | Read-only aggregate of the foreground window, battery, fixed local disk space, screen size, and mouse. |
| `inspect_path` | Bounded stat/list/search/read_text within Downloads/Documents. |
| `open_path` | Opens an allowed regular PDF/text/image file or a directory. |
| `manage_path` | Single-level mkdir, regular-file copy/same-volume move, basename rename; never overwrites. |
| `open_app` | Launches an installed common app by fixed alias; accepts no commands or arguments. |
| `get_mouse_position` | Returns the current mouse cursor coordinates. |
| `move_mouse` | Moves the mouse to the given screen coordinates. |
| `click_mouse` | Clicks the left, right, or middle button at the current position. |
| `screenshot` | Captures the current desktop and returns it as a FastMCP Image. |
| `double_click` | Double-clicks at the current position. |
| `right_click` | Right-clicks at the current position. |
| `scroll` | Sends Windows mouse wheel events. |
| `drag_mouse` | Drags from the current position to the given coordinates. |
| `focus_and_press` | Clicks the given coordinates to take focus, then presses a key. |
| `type_text` | Types text into the currently focused input area; ASCII keeps the original input path, while Chinese, Japanese, Korean, accented characters, emoji, and other Unicode use Windows `SendInput`. |
| `press_key` | Presses a supported key using Windows keyboard events. |
| `hotkey` | Performs a hotkey described by a list of keys. |
| `list_windows` | Lists visible top-level window titles. |
| `focus_window` | Focuses a visible window matched by title. |
| `focus_window_and_press` | Focuses a matching window, then presses a key. |
| `focus_window_and_hotkey` | Focuses a matching window, then performs a hotkey. |
| `focus_window_and_type` | Focuses a matching window, then types text. |
| `focus_window_and_scroll` | Focuses a matching window, moves to its center, and scrolls. |
| `list_controls` | Lists up to 150 useful UIA controls in a matching window. |
| `click_control` | Activates a UIA control by name and optional control type. |
| `click_menu_item` | Opens the given menu and activates a menu item in it. |
| `set_save_dialog_filename` | Sets the filename in a Windows Save dialog. |
| `click_save_button` | Activates the Save button in a Windows Save dialog. |
| `open_all_mailboxes` | Opens a separate mailbox window with each of the three fixed Edge profiles and returns each mailbox's open status; does not read or modify mail. |
| `summarize_all_mailboxes_today` | The master's mailbox stays Graph-first with Edge fallback; QQ Mail and the undergraduate NetEase mailbox prefer IMAP READ-only and can fall back to Browser DOM/CDP when explicitly configured; the external return structure stays compatible. |
| `search_mailboxes` | Runs a READ-only search by mailbox, keyword, sender, ISO 8601 start/end time, and maximum count; does not open message bodies or change mail state. |
| `create_mail_draft` | Creates and saves a draft in the specified verified mailbox; saves only, never sends, no attachment support. |
| `send_mail_draft` | Sends an existing draft only when `confirm_send=true`; verifies mailbox identity, draft ownership, recipient, and subject before sending. |

## Fixed mailbox identities

Mailbox identity configuration contains only non-sensitive metadata and never stores passwords, cookies, sids, tokens, session links, or other sign-in credentials.

| Identity | Display name | Edge Profile | Service | Stable URL | Permissions |
|---|---|---|---|---|---|
| `bachelor_mail` | `本科邮箱` (undergraduate mail) | `Profile 1` | NetEase Enterprise Mail | `https://mailh.qiye.163.com/` | READ, DRAFT, SEND |
| `master_mail` | `硕士邮箱` (master's mail) | `Profile 2` | Outlook Web | `https://outlook.office.com/mail/` | READ, DRAFT, SEND |
| `qq_mail` | `QQ邮箱` (QQ Mail) | `Profile 3` | QQ Mail | `https://mail.qq.com/` | READ, DRAFT |

Every send action must first create a draft and wait for user confirmation. QQ Mail may create drafts but may not send. Deleting, moving, flagging, or archiving mail requires user confirmation first. Every mailbox operation must first verify the mailbox identity and the specified profile; if that cannot be confirmed, it stops immediately and never guesses. Automatically typing passwords and recording sign-in credentials, cookies, tokens, or session links are all prohibited.


### READ-only mail search

- `search_mailboxes(mailbox_id=None, keyword=None, sender=None, start_time=None, end_time=None, max_results=10)` is the newly added 26th tool; the original 25 MCP tools are unchanged.
- Results contain only `mailbox_id`, sender, subject, received time, `message_reference`, `reference_kind`, and the search scope; message bodies are never returned.
- For the master's Outlook mailbox, when Graph is READY, it uses a Graph `$filter` server-side search that selects only `id`, sender, subject, and received time; when Graph is unavailable, it falls back to Edge.
- For QQ Mail and the undergraduate NetEase mailbox, Edge parses the visible message list of the currently verified page in read-only mode; this fallback does not type into the search box, click messages, or scroll the page, so it only covers the currently visible list and is not a complete index of the whole mailbox.
- Edge's `message_reference` is a safe hash generated from the mailbox ID and list metadata, and contains no HWND, URL, sid, or session material; Graph returns the Graph message id.

### Unified mail draft creation

- `create_mail_draft(mailbox_id, to, subject, body)` is the 27th tool; the names, parameters, and return structures of the original 26 tools are unchanged.
- The draft creation tool only saves and never sends; the result includes the mailbox, status, draft reference, recipient, and subject, but not the body.
- For the master's Outlook mailbox, when Graph is available it first calls `/me` to verify the signed-in account, then uses `/me/messages` to create the draft and re-reads it read-only; if Graph is not configured, not authenticated, or the token is invalid, it can fall back to the verified Edge profile, while a failed Graph request fails closed to avoid ambiguous duplicate drafts.
- The undergraduate NetEase mailbox and QQ Mail reuse the existing Edge profile / service domain checks, then use UIA to find explicit New Message, recipient, subject, body, and Save Draft controls; if a required control is missing it fails, and never switches to a send or close-window action.
- QQ Mail permissions are updated to READ + DRAFT, but SEND is still not allowed. Reply, Forward, and drafts/sending with attachments are not implemented yet; summaries only show attachment names, MIME types, and sizes, without downloading or decoding attachments. Sending is only possible for existing drafts via `send_mail_draft`.
- The Graph draft path requires a delegated token with `Mail.ReadWrite`; the project provides a one-time authorization code + PKCE sign-in command. Source code and ordinary configuration never store authorization codes, passwords, cookies, sids, or tokens; the refresh token is written only to Windows Credential Manager.

### Unified sending of existing drafts

- `send_mail_draft(mailbox_id, draft_reference, confirm_send)` is the newly added 28th tool; the original 27 tools are unchanged.
- This tool does not accept `to`, `subject`, or `body`, so it cannot bypass drafts to send directly; it refuses immediately unless `confirm_send=true` is passed explicitly.
- The master's Outlook mailbox is currently the only supported send backend: Graph first verifies the `/me` identity, then reads the draft metadata and checks the single recipient, subject, draft status, and ownership, and only then calls the Graph send endpoint. A failed send never falls back to Edge.
- The Graph send response does not return a message id, so `sent_reference` is empty on success; a reference to the sent message would require a separate READ-only query capability.
- The undergraduate NetEase mailbox has no Edge send implementation yet, because the existing Edge draft hash cannot reliably locate and verify an existing draft; QQ Mail remains SEND-prohibited. Passing an Edge draft reference to the send tool returns a not-sendable status.

### AI-Work unified task entry point (v1.1)

Running `python scripts/mail_assistant_server.py --no-refresh --open` provides a unified command palette, tray menu,
and `Win+Alt+A` within the existing `127.0.0.1:8931` host.
If the hotkey is already taken by another program, the tray still works and shows a fixed conflict notice.
The same page shows the latest computer morning brief by default, and keeps the mailbox summary, today's to-dos, AI email writing,
cross-mailbox search, and system status tabs; the morning brief is still generated by a separate 08:00 scheduled task and does not depend on the assistant service running.

The entry point only accepts five kinds of tasks: open the study environment, organize files, download course materials, open a code project, and create an email draft.
Natural language is first mapped to fixed intents/slots, and a local compiler then generates a plan of at most 8 steps; users cannot supply
tool names or arbitrary steps. When a folder has multiple exact matches, an email lacks a clear mailbox/recipient, or a web target is ambiguous,
the task asks for more information instead of guessing.

File mkdir/copy/move/rename, confirmed web clicks/downloads, and saving email drafts all show the plan first and use
TaskCenter's short-lived, single-use confirmation bound to the task/plan. Email previews are shown on the current page but never enter the activity
history; the email workflow only creates drafts and never calls send. Identical requests within a short window reuse the current task,
and incomplete side effects are not resumed or replayed after a process restart.

The host holds a long-lived `windows_gui_mcp.py` stdio subprocess, and all calls are serialized through a single executor and a wait
queue of at most 8 items. Read-only steps can safely reconnect and retry once after a subprocess failure; side-effecting steps that are interrupted always stop with
an unknown result and are never replayed automatically.

The activity history is stored in LocalAppData at `AI-Work/activity-history.jsonl`, keeping at most 2000 entries, 30 days,
about 1 MiB per file, and 3 rotated files. It only stores fixed summaries and redacted resources, never clipboard content, email fields,
absolute paths, full URLs, credentials, or raw exception text; the page expands steps for the 50 most recent tasks by default.

The sign-in startup definition is not persistently installed during the feature branch stage:

```powershell
python scripts/install_agent_startup.py --dry-run
python scripts/install_agent_startup.py --check
```

After merging to the main branch, enable it if needed by explicitly running `python scripts/install_agent_startup.py --install`.
This task only starts the same 8931 host, prohibits concurrent instances, and does not trigger a mail refresh. The Daily Computer Brief
is implemented by a separate module and scheduled task and is not part of the Agent workflows; Remote/LAN expansion and new Browser/Mail
capabilities are outside the scope of this Agent Productization.

### Local AI summary and draft assistant

- `scripts/daily_mail_digest.py` generates the three-mailbox digest; `scripts/mail_assistant_server.py` binds only to `127.0.0.1:8931` and validates Host, Origin, and the JSON Content-Type.
- The assistant page supports selecting a message from the latest local digest and generating an AI reply draft; the result only goes into an editable form and is never saved or sent automatically. Digest cards additionally carry the sender address and subject metadata used for local rendering.
- The assistant page's "Today's to-dos" only parses the latest local digest, producing a concise task list by deadline, reply/action needed, school administration, and high importance; it does not access mailboxes, change read status, or delete or move mail.
- The assistant page's "Cross-mailbox search" parses common Chinese requests into keywords and a start/end time, and reuses the existing `search_mailboxes()` read-only metadata search; for example, "找最近两个月关于实习的邮件" ("find emails about internships from the last two months"). This path adds no send side effects.
- The JSON body of mutating assistant requests is limited to 256 KiB; an invalid or negative `Content-Length` is rejected before the request body is read.
- Assistant page responses include `nosniff`, `no-referrer`, same-origin frame protection, and a CSP; background refreshes are lock-protected so repeated clicks cannot start multiple read tasks.
- "Refresh digest" (`刷新摘要`) shows background running, the time of success, or an explicit failure status, and updates the mailbox summary in place on success; it only calls the existing read-only digest flow.
- AI instructions, recipient, subject, and body all have length limits; the recipient must be a single plain email address, and newlines are stripped from the subject to block SMTP/Graph header injection.
- Sending uses two-phase confirmation: the first click only saves the pending draft and generates a single-use reference; the user must click "Confirm sending the saved draft" (`确认发送已保存草稿`) again. The backend verifies the saved draft's recipient, subject, body, and location before sending; editing draft fields invalidates the pending reference.
- The assistant's SMTP path only accepts an `EmailMessage` that has been retrieved and verified; there is no bypass that "builds and sends immediately from new fields".
- IMAP saving requires the server to return APPENDUID; after saving, the draft is re-read read-only to verify `\Draft`, sender, recipient, subject, body, SHA-256, and folder/UID/UIDVALIDITY.
- Before sending, it also confirms that the Graph message is still `isDraft=true`, or that the IMAP message still carries the `\Draft` flag; references whose state has changed fail explicitly.
- Pending references expire after 15 minutes, with at most 16 kept in-process; expired, edited, or reused references fail explicitly and require saving the draft again.
- AI Chinese summarization/translation calls Zhipu GLM; the QQ assistant can only save drafts, the undergraduate SMTP path saves a draft before sending, and the page's send button still requires explicit confirmation.
- The assistant's QQ/undergraduate drafts and undergraduate SMTP use separate Credential Manager authorization code entries; if missing they fail explicitly and never fall back to the read-only digest credentials.
- Run `python scripts/configure_mail_credentials.py --missing-assistant` to configure the three assistant-specific authorization codes; each secret must be entered twice with hidden input. Only allowlisted targets can be selected via `--key`/`--all-configurable`, secret values cannot be passed as arguments, and overwriting an existing entry requires an explicit `--force`.

### Local health check

- Run `python scripts/system_health.py` for a text report, or add `--json` for automation; the exit code is 1 when a required check fails.
- `--dashboard` outputs the same four-state `PASS` / `WARN` / `FAIL` / `UNKNOWN` model as the assistant page's "System status"; the default mode keeps the strict gates on environment, scheduled task definitions, and digest freshness.
- Checks are limited to local configuration and runtime state: environment variable name presence, Credential Manager entry presence, registration of the 42 MCP tools, scheduled tasks, the latest `last-run.json` status, and the assistant service status.
- The digest health check requires every mailbox status to be `READY`/`EMPTY_TODAY` and the report to be no more than 13 hours old (covering the two daily runs at 10:00/22:00); whether the Toast was shown is a separate optional INFO and is not mixed with mail-reading health.
- The digest HTML and `last-run.json` are written via a temporary file and atomic replacement; the status includes `ok`, mailbox read results, counts, and Toast status. A failed status write makes the task fail explicitly, so there is never a false "task succeeded but report is stale" signal.
- `last-attempt.json` records the run stage, mailbox statuses/counts, and error types; it contains no senders, subjects, bodies, URLs, or credentials, and is used to locate the stage when a task fails but `last-run.json` was not updated.
- If the run lock is held by a concurrent refresh, the skipped digest run returns failure; this prevents the scheduled task from falsely reporting success while `last-run.json` is still stale.
- The assistant page's `/api/health` only performs local read-only checks and never launches a browser, reads mail, or probes remotely; recent errors come from a bounded log restricted to an allowlist of fixed codes/summaries, containing no email fields, URLs, raw exception text, or credentials.

### Scheduled task recovery

- Preview the recovery command: `python scripts/install_scheduled_tasks.py --dry-run`.
- Check the current definition read-only: `python scripts/install_scheduled_tasks.py --check`; it reports differences in path, arguments, trigger times, and execution limits, but never starts or modifies the task.
- Task definition checks also cover not starting on battery power, stopping on battery power, prohibiting concurrent instances, and a 1-hour execution limit.
- To create or repair `AI-Work Daily Mail Digest`, explicitly run `python scripts/install_scheduled_tasks.py`; the task triggers daily at 10:00 and 22:00, prohibits concurrent instances, and runs for at most 1 hour.
- Installation only writes the scheduled task definition and never reads mail immediately; real mailbox reads are still determined by the scheduled times or an explicit user start.
- This command does not open a browser, operate the desktop, access external mail services, or read message bodies, and never outputs or saves credential values; an assistant service that is not running is reported only as `INFO` and does not affect the required-check verdict.

### One-time Outlook sign-in

- After `AI_WORK_OUTLOOK_TENANT_ID` and `AI_WORK_OUTLOOK_CLIENT_ID` are configured, run `python scripts/authenticate_master_mail.py`.
- The command uses authorization code + PKCE, with the callback bound only to `127.0.0.1:8932`, and verifies the exact path, Host, and OAuth `state`; use `--no-open` to only print the URL without opening a browser automatically.
- On success, only the refresh token is written to `AI-Work/windows-gui/mailboxes` / `master_mail_graph_refresh_token`; the authorization code and access/refresh tokens are never printed, saved to the repository, or written to logs.

### Outlook Graph backend

- The summary and search paths stay READ-only and only require a delegated token with `Mail.Read`.
- The draft creation path requires a delegated token with `Mail.ReadWrite`; the one-time OAuth sign-in flow is provided by `scripts/authenticate_master_mail.py`.
- The path for sending existing drafts additionally requires the existing delegated token to have `Mail.Send`; if not configured or lacking permission, it returns a not-sendable error and never falls back to Edge sending.
- Graph requests only read `sender`, `subject`, `receivedDateTime`, and at most 10 list metadata items; they never read message bodies or change read status.
- Non-secret configuration comes from environment variables: `AI_WORK_OUTLOOK_TENANT_ID`, `AI_WORK_OUTLOOK_CLIENT_ID`, `AI_WORK_OUTLOOK_MAILBOX`.
- The digest refresh token is stored in Windows Credential Manager under `AI-Work/windows-gui/mailboxes` / `master_mail_graph_refresh_token`; when Microsoft returns a rotated token, it is immediately written back to this dedicated entry. Tokens must never appear in source code, logs, test fixtures, or Git.
- Reading, exchanging, and rotating/writing back the refresh token are serialized by a Windows named mutex, preventing the scheduled task and the assistant from invalidating each other through concurrent rotation. Access tokens are kept only in memory.
- Token exchange and refresh token write-back during the one-time OAuth sign-in use the same cross-process lock, preventing sign-in and the scheduled task from invalidating each other through concurrent rotation.
- Interactive sign-in is triggered only by an explicit command; a failed token exchange never overwrites existing credentials. When refresh fails, the refresh token is invalid, or a Graph request fails, it still explicitly returns failure or falls back to the existing Edge READ-only summary path.

`open_all_mailboxes()` and the Edge summary path share the internal `get_or_open_mailbox_window()` management layer; when Outlook Graph is available, this Edge window management layer is not called, and when Graph is unavailable it falls back according to the rules above. Each mailbox is bound to at most one Edge HWND in the Agent: a valid runtime binding returns `REUSED_EXISTING_WINDOW`; after a Server restart, it first recovers the window through the window PID and the `--profile-directory` in the process command line. When Edge reuses the same browser process and the main command line has no profile argument, the binding is recovered using the exact profile display name suffix in the Edge browser title, never guessed from page UIA content. Recovery returns `RESTORED_WINDOW_BINDING`; `CREATED_NEW_WINDOW` is returned only when no window for the corresponding profile is found. This logic never closes duplicate windows the user had already opened.

The undergraduate mailbox uses the fixed, non-session, safe entry point `https://mailh.qiye.163.com/`. The window management layer prefers a page with the host name `mailh.qiye.163.com` in an existing Profile 1 window; if a reused or recovered window is on a new tab, a blank page, or another non-mailbox page, it submits this fixed entry point through the Edge address bar within the same HWND and waits for the exact domain and at least two kinds of stable, non-sensitive mailbox UI signals. An expired session or a sign-in page returns `AUTH_REQUIRED`, and a load timeout returns `LOAD_TIMEOUT`; neither falsely reports READY. Full NetEase URLs, sids, and other session material are never saved, logged, or reused.

`summarize_all_mailboxes_today()` runs strictly in the order undergraduate, master's, QQ. The master's Outlook mailbox prefers Graph READ-only and falls back to Edge when Graph is unavailable; QQ Mail and the undergraduate NetEase mailbox prefer the shared IMAP READ-only adapter. The existing Browser DOM fallback is attempted only when IMAP is unavailable and the user has explicitly configured the corresponding CDP endpoint.

QQ IMAP always connects to `imap.qq.com:993` using SSL/TLS verified against the system CAs. The non-secret username is provided by `AI_WORK_QQ_IMAP_USERNAME`; the separate authorization code is read only from Windows Credential Manager, with service `AI-Work/windows-gui/mailboxes` and username `qq_mail_imap_authorization_code`, and must not reuse the Graph token entry. The adapter uses `EXAMINE` (`select(..., readonly=True)`), UID SEARCH, and `BODY.PEEK[HEADER.FIELDS ...]`, never calls STORE, MOVE, COPY, or EXPUNGE, and never uses this credential for drafts or sending. Not configured, authentication failure, network/TLS failure, and protocol parsing failure each return an explicit IMAP status; if candidate messages cannot be parsed, it never falsely reports `EMPTY_TODAY`.

The undergraduate NetEase IMAP always connects to `imaphz.qiye.163.com:993`, likewise using implicit SSL/TLS with system CA and hostname verification. The non-secret full school email address is provided by `AI_WORK_BACHELOR_IMAP_USERNAME`; the authorization code is read only from a separate Windows Credential Manager entry, with service `AI-Work/windows-gui/mailboxes` and username `bachelor_mail_imap_authorization_code`. The undergraduate and QQ credentials are completely separate, and both are used only for the summary READ backend, never for Search, Draft, Send, or SMTP.

On the Edge path, the profile identity comes from the in-memory binding established when this process launches the window with `--profile-directory`, and is no longer inferred from UIA page content. UIA only reads the address bar and immediately extracts the host name to exactly verify `mailh.qiye.163.com`, `outlook.office.com` (and the redirect domain `outlook.cloud.microsoft`), `mail.qq.com`, or the official QQ Mail domain `wx.mail.qq.com`; full URLs are never saved, logged, or returned.

The QQ Browser fallback and the undergraduate NetEase summary do not use Windows UIA to reconstruct message rows. UIA only confirms the runtime profile, target window, exact service domain, and sign-in/page state; the Browser adapter only extracts the sender, subject, received time, and a local opaque reference generated from a truncated SHA-256. The adapter never clicks messages, opens bodies, or changes read status, and provides no send, delete, move, archive, or flag actions.

CDP endpoints must be configured separately via `AI_WORK_BACHELOR_CDP_ENDPOINT` and `AI_WORK_QQ_CDP_ENDPOINT` as local loopback HTTP origins with an explicit port, no credentials, and no query parameters (for example `http://127.0.0.1:9222`). WebSocket debugging URLs containing browser target identifiers are neither accepted nor saved. Not configured returns `BROWSER_BACKEND_NOT_READY`, a connection or 5-second attach failure returns `BROWSER_ATTACH_FAILED`, and an expired sign-in returns `AUTH_REQUIRED`; list not found, unparseable list rows, and confirmed empty today return `MAIL_LIST_NOT_FOUND`, `MAIL_ITEMS_NOT_PARSED`, and `EMPTY_TODAY` respectively. `EMPTY_TODAY` is reported only when a trusted list is recognized and parsed successfully, or the page explicitly exposes an empty-list state.

CDP cannot be safely enabled after the fact on an ordinary running Edge. The project never automatically closes/restarts Edge, never starts another automation instance with the same everyday User Data directory, and never copies profiles; these approaches can cause profile locks, duplicate processes, or session corruption. The remote debugging port is highly privileged and has no application-level authentication, so it should be bound only to loopback, enabled only during acceptance testing, and the user decides whether to accept that risk. If the existing Edge was not started with CDP enabled, the adapter stops explicitly and never falls back to parsing QQ/NetEase message rows via UIA.

## Security notes

- GUI operations affect the current interactive desktop. Before calling them, make sure the target window title is specific enough.
- Automated tests must mock all real mouse, keyboard, and UIA side effects.
- Real GUI verification should only use the dedicated files and windows created by `tests/smoke_test.py`.
- The default smoke test only operates on a dedicated Notepad fixture. When `--mailbox-readonly` is explicitly added, it calls the unified window management layer only once, preferring to reuse or recover existing profile windows, and verifies the runtime window bindings and service domains; it does not create three extra mailbox windows each time, close the user's existing windows, or open messages.
- Screenshots, Python caches, and smoke artifacts are excluded by `.gitignore`.

## v1 acceptance and freeze

The v1 public surface is frozen at 42 tools. All nine demos are accepted in [the v1 acceptance record](docs/V1_ACCEPTANCE.md), including read-only recovery of the single Graph draft created during Demo 7 without a duplicate POST or send. Goal C adds no public tools. VS Code launches in a fixed new window. Main merge requires separate user approval.


## Daily Computer Brief (v1.1 computer morning brief)

The Daily Computer Brief is a standalone, non-MCP local scheduled workflow; the public tool count remains 42.
It does not depend on ChatGPT, MCP stdio, Edge, or the mail assistant page to run. It directly reuses the internal
Graph/IMAP read-only metadata summary, the safe Downloads scan, and system_status, and uses deterministic rules to generate at most 3 suggestions; it never calls a remote LLM.
It has no drafting, sending, mail flagging/moving/archiving/deleting, reading of downloaded content, or file management actions.
Graph OAuth refresh still uses the existing cross-process lock and Credential Manager refresh-token rotation.

Using a Python with the project dependencies installed, run from the retained runtime directory:

```powershell
python scripts/daily_computer_brief.py --dry-run
python scripts/daily_computer_brief.py --no-notify
python scripts/daily_computer_brief.py --open
python scripts/install_scheduled_tasks.py --computer-brief --dry-run
python scripts/install_scheduled_tasks.py --computer-brief
python scripts/install_scheduled_tasks.py --computer-brief --check
```

`--dry-run` only collects read-only data and prints redacted JSON, without writing brief artifacts or notifying; OAuth credential rotation may still occur.
`--no-notify` generates the files without sending a Toast; `--open` is an optional action for manual runs only and is mutually exclusive with `--dry-run`.
Without `--computer-brief`, the default install entry point still selects the original 10:00/22:00 mail digest task.
Installing the brief only touches `AI-Work Daily Computer Brief`; it never starts the task or modifies the existing mail task.
The brief task runs daily at **08:00** local time as the current user, Interactive / Limited, with no administrator rights or saved password required;
IgnoreNew prohibits concurrency, the timeout is 15 minutes, running on battery is allowed, and StartWhenAvailable allows catching up after a missed run.
It requires the user to be signed in and the computer to be able to run; it does not guarantee immediate execution when shut down or signed out, and does not depend on any foreground app.
The check covers the action, working directory, daily trigger type/interval/count, duplicate triggers, enabled state, power policy, timeout, concurrency, and current user SID.

Output is written to `%LOCALAPPDATA%\AI-Work\computer-brief\`:

- `latest.html`: today's highlights, mailbox summary, recent downloads, system status, and suggestions.
- `latest.json`: the required safe summary, with no message bodies, senders/recipients, credentials, clipboard content, or full local paths; subjects and window titles are bounded, and common URL/address/path/credential patterns are hidden.
- `last-attempt.json`: only time, stage, status, counts, and fixed error codes, with no raw exceptions or email fields.

Each file is written via a same-directory temporary file, fsync, and atomic replacement; directory redirection for Windows packaged apps is resolved through a directory handle,
never falling back to a non-atomic copy. Old artifacts are not used as input, so malformed old files cannot contaminate a new brief.
The three files are not a cross-file transaction; on a write failure `ok=false`, and an old latest file may remain, so check the generation time against last-attempt.
Only the temporary files created by the current write are cleaned up; user files and other run artifacts are preserved.

Failures are never shown as zero emails: auth/config errors, unavailable, and parser/backend failures use null counts;
EMPTY_TODAY means confirmed empty, and READY is a bounded metadata count of at most 100 items that may miss paginated, truncated, or unparseable items,
and does not claim to be the full mailbox total. Importance is a keyword rule on email subjects, not semantic classification.
Downloads scans only the top level, the last 24 hours, and shows at most 10 files; it keeps the 10,000-entry/3-second scan budget,
and partial results or the internal 200-item truncation are explicitly shown as partial; counts are limited to the observed scope and do not guarantee the truly newest files.
A failed system component shows unknown, and a missing battery shows not_present. A degraded component does not block other sections or the brief itself;
a run's `ok` means the artifact was generated successfully, not that all dependencies are healthy. The Toast only shows a fixed title and counts, and a Toast failure is marked degraded separately.

Deployment uses a separate, detached Git worktree at `<runtime-dir>` (e.g. a fixed local folder such as `D:\...\AI-Work-runtime`),
updated and checked with `--check` through the same `--computer-brief --root <runtime>` install entry point from that directory.
This runtime always checks out the verified `origin/main`, does not occupy a feature branch, and does not require modifying the user's dirty main checkout;
to upgrade, first complete verification and the main merge in a separate development worktree, then update the clean runtime to the new verified main and reinstall, check, and trigger it.
Do not delete a runtime directory that is still referenced by a scheduled task.
The local Windows scheduler is the primary implementation; an external ChatGPT 08:00 automation may send duplicate reminders, and the user must choose whether to disable it; this implementation does not modify it.
