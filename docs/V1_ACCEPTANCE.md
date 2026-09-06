# AI-Work v1 acceptance — 2026-09-05

Status: COMPLETE — all nine demos are accepted and the v1 public surface is frozen at exactly 42 tools. Feature-branch commit/push is authorized; main merge is not authorized.

All GUI actions below used tests/smoke_test.py and owned fixtures. No existing user document was used as a test target. Keep fixture windows/artifacts; do not save Notepad edits.

| Demo | Actual input | Actual result | Acceptance | Limit / MANUAL CHECK |
|---|---|---|---|---|
| 1 VS Code | open_app(vscode), fixed --new-window | New HWND 40767470 observed; user confirmed new window normal | PASS | User manual confirmation received |
| 2 latest PDF | Controlled Downloads, .pdf, depth 0, modified_desc, limit 1 | Complete scan selected AI-Work-v1-local-0a880e6dc6ca4183bd8808328237c8eb.pdf; viewer HWND 920664; user confirmed marker `AI-Work v1 controlled PDF fixture` | PASS | Controlled fixture roots injected internally |
| 3 move/no overwrite | Downloads/move-fixture.pdf → Documents/HCI/move-fixture.pdf; collision-fixture.pdf to same destination | moved; destination_exists; hash preserved, moved source absent, collision source retained | PASS | Controlled same-volume files only |
| 4 course folder | mkdir Documents/HCI | created; used by successful move | PASS | Controlled Documents root; single level |
| 5 browser/download | https://www.w3.org/WAI/ER/tests/xhtml/testfiles/resources/pdf/dummy.pdf | Existing browser start/navigate/inspect and download_web_file completed; 13264 bytes, PDF header and SHA-256 verified; user confirmed browser display | PASS | Public fixture, no login/course content |
| 6 today mail | Explicit READ/summarize authorization: bachelor_mail, master_mail, qq_mail; existing summarize_all_mailboxes_today | Second run: bachelor_mail EMPTY_TODAY (0); master_mail READY (0); qq_mail EMPTY_TODAY (0); all important-category counts 0 | PASS | Trusted primary read-only paths. QQ current-day empty result is separate from the deterministic regression for the prior parsing failure. No draft/send/delete/move/archive/mark requested or invoked |
| 7 mentor draft | master_mail; recipient 2453593047@qq.com; subject 关于 VR 交互方向学习与项目选题的请教; three supplied VR interaction points | Original create call created one exact Graph draft but failed during local post-create validation. After validation/status-mapping fixes, a strict read-only recovery returned READY, backend GRAPH_API, a stable GRAPH_DRAFT_ID, sent=false, send_attempted=false. Final read-only count remained exactly one | PASS | Recovery used identity verification plus exact isDraft/recipient/subject/body matching. It performed no POST and no send. NOT SENT |
| 8 clipboard → Notepad | AI-Work clipboard smoke test | Write/read matched, focus_window_and_type completed into AI-Work-v1-extra-87bd864e93a94f66b6a4ac8eda60247a.txt; user confirmed exact text | PASS | Unsaved fixture; prior clipboard never read/backed up |
| 9 status | get_system_status() | All five components ok; battery 100%, AC true, charging false; C free 53331935232/429497774080, D free 188569952256/592323801088 bytes; primary/virtual 2560x1600 origin 0,0; mouse 256,1224 | PASS | Sequential observation, process DPI; foreground title not persisted |

Download SHA-256: 3df79d34abbca99308e79cb94461c1893582604d68329a41fd4bec1885e6adb4.

Artifacts: tests/smoke_artifacts/v1-local-0a880e6dc6ca4183bd8808328237c8eb and tests/smoke_artifacts/v1-extra-87bd864e93a94f66b6a4ac8eda60247a (generated, untracked, retained).

Automated smoke substeps passed; pending manual checks are NOT demo PASS. Missing prerequisites, timeout or worker failure must never become PASS.

## Freeze gate

All nine demos are accepted. Complete regression, exact 42 registration, old 36 compatibility, FAILSAFE, diff hygiene and synchronized documentation form the v1 freeze gate. Browser expansion, new Mail features, Remote/LAN development, planning, memory, voice, multi-agent, delete, arbitrary shell and automatic sending remain out of scope. Main integration still needs separate user approval.

Goal C latest automated verification (2026-09-05): compileall PASS; complete 612 tests PASS; 42 unique registrations PASS; original 36 signatures and implementation modules unchanged PASS; FAILSAFE PASS; git diff --check PASS. This does not resolve pending real-mail prerequisites or manual checks.

User confirmed Demos 1, 2, 5 and 8 PASS: VS Code new window, controlled PDF marker, W3C PDF display, and exact Notepad clipboard text. Demo 7 then received concrete fields, but one authorized draft-only call returned ERROR via EDGE_GUI with no reference; it was NOT SENT and was not retried. No feature-complete declaration, commit or push while acceptance is incomplete.

## Goal C.2 master draft repair — 2026-09-06

The original draft path read the obsolete `master_mail_graph_access_token` Credential Manager slot. The repaired master Graph backend now obtains an in-memory access token through the existing locked refresh-token rotation with `Mail.ReadWrite offline_access`; it does not persist or log the access token. Identity is checked against the configured master mailbox before creation. Created drafts are verified through a read-only Graph fetch for `isDraft`, sender identity, recipient, subject, and body (HTML/plain text is normalized only for comparison). Graph request failures fail closed and no longer trigger the GUI fallback for this master draft path, avoiding an unverified UI side effect.

Regression coverage includes refresh-token use, refresh failure/no fallback, identity mismatch, Graph request failure/no fallback, persisted metadata verification, non-draft rejection, HTML body normalization, no-send dispatch, and existing fallback isolation. Focused `tests.test_mail_draft` passed 18/18. The authorized real retry returned `GRAPH_API / ERROR` with no reference; `sent=false` and `send_attempted=false`. A read-only Graph check before and after found no exact matching draft (15 draft items, zero matches each time). Demo 7 therefore remains FAIL / BLOCKED and v1 freeze is not declared.

## Goal C.3 Graph real-environment diagnosis — 2026-09-06

OAuth is sufficient for draft creation: the refresh succeeded, the OAuth response and Graph JWT both exposed `Mail.ReadWrite`, the audience was Microsoft Graph, and the account claim was present. The draft-only refresh did not request `Mail.Send`. Initial interactive authorization code requests include Mail.Read, Mail.ReadWrite, Mail.Send, and offline_access, while the C.3 draft token deliberately requests only Mail.ReadWrite plus offline_access.

The first confirmed root cause is `D. GRAPH_REQUEST_INVALID`. The implementation sent `{message: {...}}` to `POST /v1.0/me/messages`; that wrapper belongs to `sendMail`, while create-message requires the Message JSON object directly. A deterministic contract regression failed against the old implementation, then passed after the minimal payload correction.

Before the authorized retry, a read-only duplicate check returned Graph HTTP 200, 15 drafts, and zero exact matches. The retry reached GRAPH_API and created one matching draft, but exposed two further local verification bugs: newly created Graph drafts can omit `from` and `sender` even after `/me` identity was verified, and `BackendStatus.INVALID_DRAFT` was absent from the public status mapping. The latter raised `KeyError`, so the tool returned ERROR without a draft reference. A read-only post-check returned 16 drafts and exactly one match; `isDraft`, recipient, subject, and body matched, a stable Graph id existed, and both owner fields were absent. The verifier now accepts absent owner metadata only after the existing `/me` identity check and still rejects any present mismatched owner; INVALID_DRAFT now maps to ERROR without exception. Focused draft tests pass 21/21.

The actual tool call did not return the required stable reference, so Demo 7 remains FAIL / BLOCKED despite the recovered object being a verified draft. No further POST was made, no send endpoint was called, and the draft is NOT SENT. Final C.3 compileall passed; the complete suite passed 626/626 on the final run. An earlier complete run and the first isolated rerun hit only the already frozen Remote concurrent-replay race; eight isolated runs produced six PASS and two FAIL, then the final complete run passed. Interface verification confirmed 42 unique tools, old 36 signatures, server `windows-gui`, and PyAutoGUI FAILSAFE; `git diff --check` passed.

## Goal C.4 stable-reference recovery — 2026-09-06

The MCP draft reference contract is the stable Graph message id with `reference_kind=GRAPH_DRAFT_ID`. It has no TTL or process-local pending registry. The separate Mail Assistant TaskCenter registry does not participate in `create_mail_draft` or `send_mail_draft`; sending still requires a later explicit confirmation and revalidates the Graph draft.

A private, non-MCP recovery path was added only for the known v1 post-create failure. It refreshes a Mail.ReadWrite token, verifies `/me`, reads at most 100 drafts, rejects pagination, and requires exactly one candidate matching `isDraft=true`, recipient, subject, body, and any present owner metadata. Zero matches, multiple matches, identity mismatch, missing stable id, incomplete lists, or metadata mismatch fail closed. The ordinary `create_mail_draft` path does not call this recovery and does not gain general deduplication behavior. Future post-create validation failures retain the created Graph id internally for safe diagnosis without exposing an unverified public reference.

The real recovery returned READY through GRAPH_API with a stable GRAPH_DRAFT_ID. Its non-sensitive reference fingerprint was `83c7eedbe75b90f4`. A second read-only count remained 16 total drafts and exactly one target match with the same fingerprint. Recovery performed no POST and no send; `sent=false`, `send_attempted=false`, NOT SENT. This closes Demo 7 while preserving the original failure history above.

Final freeze verification: compileall PASS; complete suite 631/631 PASS; recovery/send focused suite 41/41 PASS; 42 registered tools with 42 unique names; old 36 signatures PASS; server `windows-gui`; PyAutoGUI FAILSAFE enabled; `git diff --check` PASS. README, AGENTS, PROJECT_STATE, and this acceptance record are synchronized. Remote LAN smoke remains outside v1 acceptance. The frozen Remote concurrent-replay race remains a residual risk and was not modified.

## Goal C.1 diagnosis and repair — 2026-09-06

Original Demo 6: bachelor EMPTY_TODAY; master MAIL_LIST_NOT_FOUND; QQ MAIL_ITEMS_NOT_PARSED.

Master: configured Graph summary read the obsolete access-token Credential Manager slot, which was absent. Primary status was NOT_AUTHENTICATED; Edge fallback then failed list discovery. Summary now uses the existing locked refresh-token rotation with Mail.Read scope, checks /me against configured mailbox before GET list, and rejects malformed list/date payloads. Draft/send implementations are unchanged; access tokens remain in memory.

QQ: primary IMAP selected, credential present. Read-only reproduction of the original 2026-09-05 candidate interval fetched two items; both had headers/sender/subject but neither INTERNALDATE parsed. Sanitized shape diagnostics proved a one-digit unpadded day (25-byte quoted date). Normalize only that observed variant before existing parser. No Date-header fallback or UIA reconstruction. On 2026-09-06 the current-day search was empty before repair; that is not evidence of repairing the original parser failure.

Deterministic regressions: single-digit day initially FAIL; two new Graph integration tests initially ERROR because integration was absent. After repair all six regressions PASS, including no invented missing date, identity mismatch, refresh failure and malformed Graph list. 77 related tests passed before adding three negative cases. Two old UIA tests had depended on missing local credentials; they accidentally reached real read-only Graph after refresh integration, then were changed to call the mocked Edge parser directly. No mail-state write occurred. First full 615-test run FAIL on known frozen Remote concurrent replay test; do not erase this failure or claim the race fixed.

After three more negative regressions were added, an 618-test full run failed
on the same Remote concurrency assertion and had no Mail failure.

After handoff, the focused Mail suite passed 81/81. The exact frozen Remote
concurrency test was run eight times: seven passed and one failed, confirming
the known interleaving flake without evidence of a Mail-diff dependency. The
final complete suite passed 619/619 after compileall. Server name
`windows-gui`, 42 registered tools, 42 unique names, the old 36 signatures,
PyAutoGUI FAILSAFE, and `git diff --check` all passed.

The authorized second Demo 6 run used only `summarize_all_mailboxes_today`:
bachelor_mail returned `EMPTY_TODAY` with zero items; master_mail returned
`READY` through the identity-checked Graph path with zero items; qq_mail
returned `EMPTY_TODAY` with zero items. Every important category contained
zero items. No draft, send, SMTP, delete, move, archive, mark, or message-row
click occurred. The QQ run happened on the next calendar day and therefore
does not reproduce the previous messages; the deterministic single-digit
INTERNALDATE regression is the evidence for that repair.
