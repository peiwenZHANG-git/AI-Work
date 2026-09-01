# Windows GUI MCP Server

这是一个仅面向 Windows 的 FastMCP Server，为 AI 客户端提供鼠标、键盘、窗口聚焦、截图、Windows UI Automation 和菜单操作能力。

项目保留 `windows_gui_mcp.py` 作为兼容启动入口，具体实现按职责放在 `windows_gui/` 包中：

- `server.py`：共享 FastMCP 实例和 PyAutoGUI 安全设置。
- `mouse.py`：鼠标、滚轮、拖动和截图。
- `keyboard.py`：文本输入、按键和快捷键。
- `windows.py`：窗口枚举、聚焦及聚焦后的输入操作。
- `uia.py`：UI Automation 控件、菜单和保存对话框操作。
- `mail_backends.py`：统一邮箱后端抽象、Graph 与 Edge adapter；摘要和搜索保持 READ-only，草稿仅保存不发送。
- `browser_mail.py`：QQ 与本科网易邮箱的 Browser DOM/CDP READ-only 摘要 adapter；只提取列表元数据。
- `imap_mail.py`：QQ 与本科网易邮箱共享的标准库 IMAP READ-only 摘要 adapter；使用 SSL、EXAMINE、UID 和 BODY.PEEK。
- `mailboxes.py`：固定邮箱身份、权限边界和 Edge Profile 启动逻辑。
- `mail_search.py`：统一 READ-only 邮件搜索、后端分发和安全结果引用。
- `mail_draft.py`：统一草稿创建、Graph/Edge 后端分发和不发送安全检查。
- `mail_send.py`：统一发送已有草稿、显式确认和发送前元数据校验。
- `mail_summary.py`：邮箱身份和页面验证、只读列表解析、今日摘要及重要事项分类。
- `mail_digest.py`：计划任务摘要、Outlook refresh 轮换、GLM 摘要/翻译和本地 HTML 渲染。
- `master_oauth.py` / `scripts/authenticate_master_mail.py`：一次性 Outlook OAuth 登录和 refresh token 安全写入。
- `mail_assistant.py` / `scripts/mail_assistant_server.py`：本机 AI 草稿助手和 `127.0.0.1:8931` 页面。
- `scripts/configure_mail_credentials.py`：交互式写入白名单凭据；输入不回显，密钥不从命令行或日志传递。
- `scripts/system_health.py`：本机只读健康检查，验证配置/凭据存在性、MCP 注册、计划任务、助手服务和最近摘要运行状态。
- `scripts/install_scheduled_tasks.py`：幂等恢复每日摘要计划任务；只注册任务，不自动触发邮件读取。

## 环境要求

- Windows 10 或 Windows 11
- Python 3.10 或更高版本
- 可交互的桌面会话

安装依赖：

```powershell
python -m pip install fastmcp pyautogui pywin32 pywinauto pillow requests keyring
```

QQ / 网易 Browser DOM 摘要还需要可选依赖 `playwright`。该 adapter 只使用
`connect_over_cdp` 连接已经由用户明确开启远程调试的 Edge，不会下载或启动新的浏览器：

```powershell
python -m pip install playwright
```

`pyautogui` 的故障保护已开启。把鼠标快速移动到屏幕左上角可中止 PyAutoGUI 操作。

## 启动

在项目根目录运行：

```powershell
python windows_gui_mcp.py
```

VS Code MCP 配置位于 `.vscode/mcp.json`，通过 stdio 启动同一个兼容入口。

## 测试

运行所有不操作真实桌面的单元测试：

```powershell
python -m unittest discover -s tests -t . -v
```

运行语法编译检查：

```powershell
python -m compileall -q windows_gui_mcp.py windows_gui tests
```

运行真实 Windows GUI smoke test：

```powershell
python tests/smoke_test.py
```

Smoke test 只使用唯一命名的专用记事本文件，测试结果写入 `tests/smoke_artifacts/`。它不会删除文件、发送消息、关闭程序或操作已有文档；记事本会保留在桌面供人工确认。日志中的 `MANUAL CHECK` 表示需要观察截图或桌面状态。

## MCP 工具

当前服务器注册 28 个工具；原有 27 个工具的名称、参数和返回结构保持不变。

| 工具 | 用途 |
|---|---|
| `get_mouse_position` | 返回当前鼠标光标坐标。 |
| `move_mouse` | 把鼠标移动到指定屏幕坐标。 |
| `click_mouse` | 在当前位置单击左键、右键或中键。 |
| `screenshot` | 截取当前桌面并作为 FastMCP Image 返回。 |
| `double_click` | 在当前位置执行双击。 |
| `right_click` | 在当前位置执行右键单击。 |
| `scroll` | 发送 Windows 鼠标滚轮事件。 |
| `drag_mouse` | 从当前位置拖动到指定坐标。 |
| `focus_and_press` | 点击指定坐标取得焦点，然后按一个键。 |
| `type_text` | 向当前聚焦输入区域输入文本；ASCII 保持原输入路径，中文、日文、韩文、重音字符、emoji 和其他 Unicode 使用 Windows `SendInput`。 |
| `press_key` | 使用 Windows 键盘事件按下一个受支持的按键。 |
| `hotkey` | 执行由按键列表描述的快捷键。 |
| `list_windows` | 列出可见顶层窗口标题。 |
| `focus_window` | 按标题匹配并聚焦可见窗口。 |
| `focus_window_and_press` | 聚焦匹配窗口后按一个键。 |
| `focus_window_and_hotkey` | 聚焦匹配窗口后执行快捷键。 |
| `focus_window_and_type` | 聚焦匹配窗口后输入文本。 |
| `focus_window_and_scroll` | 聚焦匹配窗口、移动到窗口中心并滚动。 |
| `list_controls` | 列出匹配窗口中最多 150 个有用 UIA 控件。 |
| `click_control` | 按名称及可选控件类型激活 UIA 控件。 |
| `click_menu_item` | 打开指定菜单并激活其中的菜单项。 |
| `set_save_dialog_filename` | 在 Windows 保存对话框中设置文件名。 |
| `click_save_button` | 激活 Windows 保存对话框中的保存按钮。 |
| `open_all_mailboxes` | 使用三个固定 Edge Profile 分别打开独立邮箱窗口，并返回每个邮箱的打开状态；不读取或修改邮件。 |
| `summarize_all_mailboxes_today` | 硕士邮箱保持 Graph 优先 / Edge fallback；QQ 与本科网易邮箱优先使用 IMAP READ-only、显式配置时可回退 Browser DOM/CDP；外部返回结构保持兼容。 |
| `search_mailboxes` | 按邮箱、关键词、发件人、ISO 8601 起止时间和最大数量执行 READ-only 搜索；不打开正文，不改变邮件状态。 |
| `create_mail_draft` | 在指定已验证邮箱中创建并保存草稿；只保存不发送，不支持附件。 |
| `send_mail_draft` | 仅在 `confirm_send=true` 时发送已有草稿；发送前校验邮箱身份、draft 归属、收件人和主题。 |

## 固定邮箱身份

邮箱身份配置只包含非敏感元数据，不保存密码、Cookie、sid、token、会话链接或其他登录凭证。

| 身份 | 显示名称 | Edge Profile | 服务 | 稳定 URL | 权限 |
|---|---|---|---|---|---|
| `bachelor_mail` | 本科邮箱 | `Profile 1` | 网易企业邮箱 | `https://mailh.qiye.163.com/` | READ、DRAFT、SEND |
| `master_mail` | 硕士邮箱 | `Profile 2` | Outlook Web | `https://outlook.office.com/mail/` | READ、DRAFT、SEND |
| `qq_mail` | QQ邮箱 | `Profile 3` | QQ Mail | `https://mail.qq.com/` | READ、DRAFT |

所有发送动作都必须先生成草稿并等待用户确认。QQ 邮箱允许创建草稿但不允许发送。删除、移动、标记或归档邮件前必须获得用户确认。任何邮箱操作都必须先核对邮箱身份与指定 Profile；无法确认时立即停止，禁止猜测。自动输入密码以及记录登录凭证、Cookie、token 或会话链接均被禁止。


### READ-only 邮件搜索

- `search_mailboxes(mailbox_id=None, keyword=None, sender=None, start_time=None, end_time=None, max_results=10)` 是新增的第 26 个工具；原 25 个 MCP 工具保持不变。
- 结果只包含 `mailbox_id`、发件人、主题、接收时间、`message_reference`、`reference_kind` 和搜索范围；不返回正文。
- 硕士 Outlook 在 Graph READY 时使用 Graph `$filter` 服务端搜索，只选择 `id`、发件人、主题和接收时间；Graph 不可用时回退 Edge。
- QQ 与本科网易通过 Edge 只读解析当前已验证页面的可见邮件列表；该 fallback 不输入搜索框、不点击邮件、不滚动页面，因此只能覆盖当前可见列表，不代表全邮箱完整索引。
- Edge 的 `message_reference` 是由邮箱 ID 和列表元数据生成的安全 hash，不包含 HWND、URL、sid 或会话材料；Graph 返回 Graph message id。

### 统一邮件草稿创建

- `create_mail_draft(mailbox_id, to, subject, body)` 是第 27 个工具；原 26 个工具的名称、参数和返回结构保持不变。
- 草稿创建工具只保存，不发送；返回包含邮箱、状态、draft reference、收件人和主题，不返回正文。
- 硕士 Outlook 在 Graph 可用时先调用 `/me` 校验登录账号，再用 `/me/messages` 创建草稿；Graph 未配置、未认证、token 失效或请求失败时回退已验证 Edge Profile。
- 本科网易和 QQ 邮箱复用现有 Edge Profile / 服务域名校验，再通过 UIA 查找显式的新建邮件、收件人、主题、正文和存草稿控件；找不到必需控件时失败，不会改用发送或关闭窗口动作。
- QQ 邮箱权限更新为 READ + DRAFT，但仍不允许 SEND。所有邮箱都未实现 Reply、Forward 或 Attachment；Send 只能通过 `send_mail_draft` 发送已有草稿。
- Graph 草稿路径需要委托 token 具备 `Mail.ReadWrite`；当前未实现 OAuth 登录流程，也不会在源码或配置中保存授权码、密码、cookie、sid 或 token。

### 统一发送已有草稿

- `send_mail_draft(mailbox_id, draft_reference, confirm_send)` 是新增的第 28 个工具；原 27 个工具保持不变。
- 该工具不接受 `to`、`subject` 或 `body`，不能绕过草稿直接发送；未显式传入 `confirm_send=true` 时立即拒绝。
- 硕士 Outlook 是当前唯一实际支持的发送后端：Graph 先校验 `/me` 身份，再读取草稿元数据并核对单一收件人、主题、草稿状态和归属，最后才调用 Graph send endpoint。发送失败不会回退到 Edge。
- Graph send 响应不返回 message id，因此成功结果的 `sent_reference` 为空；后续如需已发送邮件引用，必须另建 READ-only 查询能力。
- 本科网易暂不提供 Edge 发送实现，因为现有 Edge draft hash 不能稳定定位和校验已有草稿；QQ 邮箱保持禁止 SEND。Edge draft reference 传入发送工具时返回不可发送状态。

### 本地 AI 摘要与草稿助手

- `scripts/daily_mail_digest.py` 生成三邮箱摘要；`scripts/mail_assistant_server.py` 只绑定 `127.0.0.1:8931`，并校验 Host、Origin 和 JSON Content-Type。
- 助手变异请求的 JSON 主体上限为 256 KiB；无效或负数 `Content-Length` 会在读取请求体前拒绝。
- AI 指令、收件人、主题和正文都有长度上限；收件人必须是单个普通邮箱地址，主题会去除换行以阻止 SMTP/Graph 头注入。
- 发送采用两阶段确认：第一次点击只保存待发送草稿并生成一次性引用；必须再次点击“确认发送已保存草稿”。后端会校验已保存草稿的收件人、主题、正文和位置后才发送；修改草稿字段会使待发送引用失效。
- IMAP 保存要求服务器返回 APPENDUID；保存后会只读重读草稿，校验 `\Draft`、发件人、收件人、主题、正文、SHA-256、folder/UID/UIDVALIDITY。
- 发送前还会确认 Graph 消息仍为 `isDraft=true`，或 IMAP 消息仍带 `\Draft` 标记；状态已变化的引用会显式失败。
- 待发送引用 15 分钟过期，进程内最多保留 16 个；过期、修改字段或重复使用都会显式失败，需要重新保存草稿。
- AI 中文摘要/翻译调用 Zhipu GLM；QQ 助手只能保存草稿，本科 SMTP 发送前先保存草稿，页面发送按钮仍需显式确认。
- 助手 QQ/本科草稿和本科 SMTP 使用独立的 Credential Manager 授权码条目；缺失时明确失败，绝不回退只读摘要凭据。
- 运行 `python scripts/configure_mail_credentials.py --missing-assistant` 配置三个助手专用授权码；每个密钥需隐藏输入两次。只能用 `--key`/`--all-configurable` 选择白名单目标，不能用参数传递密钥值；覆盖已有条目需显式 `--force`。

### 本机健康检查

- 运行 `python scripts/system_health.py` 查看文本报告，或加 `--json` 供自动化消费；必需检查失败时退出码为 1。
- 检查范围限于本机配置和运行状态：环境变量名存在性、Credential Manager 条目存在性、28 个 MCP 工具注册、计划任务、最近 `last-run.json` 状态和助手服务状态。
- 摘要健康检查要求邮箱状态全部为 `READY`/`EMPTY_TODAY`，且报告不超过 13 小时（覆盖每日 10:00/22:00 两次调度）；Toast 是否显示单独作为可选 INFO，不与邮件读取健康混在一起。
- 摘要 HTML 和 `last-run.json` 使用临时文件加原子替换写入；状态包含 `ok`、邮箱读取结果、计数和 Toast 状态。状态写入失败会让任务显式失败，不会留下“任务成功但报告过期”的假信号。
- `last-attempt.json` 记录运行阶段、邮箱状态/计数和错误类型；它不包含发件人、主题、正文、URL 或凭据，用于在任务失败但 `last-run.json` 未更新时定位阶段。
- 如果运行锁被并发刷新占用，本次摘要跳过时返回失败；这避免计划任务在 `last-run.json` 仍过期时误报成功。

### 计划任务恢复

- 预览恢复命令：`python scripts/install_scheduled_tasks.py --dry-run`。
- 只读校验当前定义：`python scripts/install_scheduled_tasks.py --check`；它会报告路径、参数、触发时间和执行限制差异，但不会启动或修改任务。
- 任务定义校验还覆盖禁止电池供电启动、电池供电时停止、禁止并发实例和 1 小时执行上限。
- 需要创建或修复 `AI-Work Daily Mail Digest` 时显式运行 `python scripts/install_scheduled_tasks.py`；任务在每日 10:00 和 22:00 触发，禁止并发实例，最长运行 1 小时。
- 安装只写入计划任务定义，不会立即读取邮件；真实邮箱读取仍由计划时间或用户显式启动决定。
- 该命令不打开浏览器、不操作桌面、不访问外部邮件服务、不读取邮件正文，也绝不输出或保存凭据值；助手服务未运行只报告 `INFO`，不影响必需检查结论。

### Outlook 一次性登录

- 在 `AI_WORK_OUTLOOK_TENANT_ID` 和 `AI_WORK_OUTLOOK_CLIENT_ID` 已配置后运行 `python scripts/authenticate_master_mail.py`。
- 该命令使用 authorization code + PKCE，回调只绑定 `127.0.0.1:8932`，校验精确路径、Host 和 OAuth `state`；使用 `--no-open` 可只打印 URL、不自动打开浏览器。
- 成功后只把 refresh token 写入 `AI-Work/windows-gui/mailboxes` / `master_mail_graph_refresh_token`；授权码和 access/refresh token 不会打印、保存到仓库或写入日志。

### Outlook Graph 后端

- 摘要和搜索路径保持 READ-only，仅需委托 token 具备 `Mail.Read`。
- 草稿创建路径需要委托 token 具备 `Mail.ReadWrite`；当前未实现 OAuth 登录流程。
- 发送已有草稿路径需要既有委托 token 额外具备 `Mail.Send`；未配置或不具备权限时返回不可发送错误，不会回退到 Edge 发送。
- Graph 请求只读取 `sender`、`subject`、`receivedDateTime` 和最多 10 条列表元数据，不读取正文，不改变已读状态。
- 非秘密配置来自环境变量：`AI_WORK_OUTLOOK_TENANT_ID`、`AI_WORK_OUTLOOK_CLIENT_ID`、`AI_WORK_OUTLOOK_MAILBOX`。
- 摘要 refresh token 存放在 Windows Credential Manager 的 `AI-Work/windows-gui/mailboxes` / `master_mail_graph_refresh_token`；Microsoft 返回旋转 token 时立即写回该专用条目。源码、日志、测试 fixture 和 Git 中不得出现 token。
- refresh token 的读取、交换和旋转写回由 Windows named mutex 串行化，避免计划任务与助手并发轮换导致对方失效。访问 token 只保留在内存。
- 一次性 OAuth 登录的 token 交换和 refresh token 写回也使用同一把跨进程锁，避免登录与计划任务并发轮换时互相失效。
- 交互式登录只通过显式命令触发；token 交换失败时不会覆盖既有凭据。刷新失败、refresh token 失效或 Graph 请求失败时，仍明确返回失败或回退到现有 Edge READ-only 摘要路径。

`open_all_mailboxes()` 和 Edge 摘要路径共享内部 `get_or_open_mailbox_window()` 管理层；Outlook Graph 可用时不会调用该 Edge 窗口管理层，Graph 不可用时按上述规则回退。每个邮箱在 Agent 中最多绑定一个 Edge HWND：有效运行时绑定返回 `REUSED_EXISTING_WINDOW`；Server 重启后优先通过窗口 PID 和进程命令行中的 `--profile-directory` 找回窗口。Edge 复用同一浏览器进程、主命令行不含 Profile 参数时，使用 Edge 浏览器标题中精确的 Profile 显示名称后缀恢复绑定，不从页面 UIA 内容猜测。恢复返回 `RESTORED_WINDOW_BINDING`；只有未找到对应 Profile 窗口时才返回 `CREATED_NEW_WINDOW`。该逻辑不会关闭用户原本打开的重复窗口。

本科邮箱使用固定的非会话安全入口 `https://mailh.qiye.163.com/`。窗口管理层会优先在 Profile 1 的现有窗口中选择主机名为 `mailh.qiye.163.com` 的页面；复用或恢复的窗口停留在新标签页、空白页或其他非邮箱页面时，会在同一 HWND 内通过 Edge 地址栏提交该固定入口，并等待精确域名和至少两类稳定、非敏感邮箱 UI 信号。会话过期或登录页返回 `AUTH_REQUIRED`，加载超时返回 `LOAD_TIMEOUT`，都不会假报 READY。完整网易 URL、sid 和其他会话材料不会被保存、记录或复用。

`summarize_all_mailboxes_today()` 严格按本科、硕士、QQ 邮箱顺序执行。硕士 Outlook 优先走 Graph READ-only，Graph 不可用时回退 Edge；QQ 与本科网易优先使用共享的 IMAP READ-only adapter。只有 IMAP 不可用且用户显式配置了对应 CDP endpoint 时，才尝试现有 Browser DOM fallback。

QQ IMAP 固定连接 `imap.qq.com:993` 并使用系统 CA 验证的 SSL/TLS。非秘密用户名由 `AI_WORK_QQ_IMAP_USERNAME` 提供；独立授权码只从 Windows Credential Manager 读取，service 为 `AI-Work/windows-gui/mailboxes`，username 为 `qq_mail_imap_authorization_code`，不得复用 Graph token 条目。adapter 使用 `EXAMINE`（`select(..., readonly=True)`）、UID SEARCH 和 `BODY.PEEK[HEADER.FIELDS ...]`，不会调用 STORE、MOVE、COPY 或 EXPUNGE，也不会把该 credential 用于草稿或发送。未配置、认证失败、网络/TLS 失败和协议解析失败分别返回明确 IMAP 状态，候选邮件无法解析时不会假报 `EMPTY_TODAY`。

本科网易 IMAP 固定连接 `imaphz.qiye.163.com:993`，同样使用系统 CA 与 hostname 校验的隐式 SSL/TLS。非秘密完整学校邮箱地址由 `AI_WORK_BACHELOR_IMAP_USERNAME` 提供；授权码只从独立的 Windows Credential Manager 条目读取，service 为 `AI-Work/windows-gui/mailboxes`，username 为 `bachelor_mail_imap_authorization_code`。本科和 QQ credential 完全分离，均仅用于摘要 READ backend，不用于 Search、Draft、Send 或 SMTP。

Edge 路径的 Profile 身份来自本进程使用 `--profile-directory` 启动窗口时建立的内存绑定，不再由 UIA 页面内容反推。UIA 只读取地址栏并立即提取主机名，用于精确验证 `mailh.qiye.163.com`、`outlook.office.com`（以及重定向域名 `outlook.cloud.microsoft`）、`mail.qq.com` 或官方 QQ Mail 域名 `wx.mail.qq.com`；完整 URL 不会被保存、记录或返回。

QQ Browser fallback 与本科网易摘要不使用 Windows UIA 重建邮件行。UIA 仅确认运行时 Profile、目标窗口、精确服务域名和登录/页面状态；Browser adapter 只提取发件人、主题、接收时间和经过 SHA-256 截断生成的本地 opaque reference。adapter 不点击邮件、不打开正文、不改变已读状态，也不提供发送、删除、移动、归档或标记动作。

CDP endpoint 必须分别通过 `AI_WORK_BACHELOR_CDP_ENDPOINT` 和 `AI_WORK_QQ_CDP_ENDPOINT` 配置为带显式端口、无凭证、无查询参数的本机回环 HTTP origin（例如 `http://127.0.0.1:9222`）。不接受或保存包含浏览器 target 标识的 WebSocket 调试 URL。未配置返回 `BROWSER_BACKEND_NOT_READY`，连接或 5 秒 attach 失败返回 `BROWSER_ATTACH_FAILED`，登录失效返回 `AUTH_REQUIRED`；找不到列表、列表行不可解析和确认今日为空分别返回 `MAIL_LIST_NOT_FOUND`、`MAIL_ITEMS_NOT_PARSED`、`EMPTY_TODAY`。只有识别到可信列表且解析成功，或页面明确暴露空列表状态，才会判定 `EMPTY_TODAY`。

普通运行中的 Edge 无法事后安全开启 CDP。项目不会自动关闭/重启 Edge，不会用同一个日常 User Data 目录再起自动化实例，也不会复制 Profile；这些做法可能造成 Profile 锁、重复进程或会话损坏。远程调试端口具备高权限且无应用级认证，应只绑定 loopback、仅在验收期间开启，并由用户自行决定是否接受该风险。若现有 Edge 没有预先开启 CDP，adapter 会明确停止而不会回退到 QQ/网易 UIA 邮件行解析。

## 安全说明

- GUI 操作会影响当前交互式桌面。调用前应确认目标窗口标题足够具体。
- 自动化测试必须 mock 所有真实鼠标、键盘和 UIA 副作用。
- 真实 GUI 验证应只使用 `tests/smoke_test.py` 创建的专用文件和窗口。
- 默认 Smoke test 只操作专用 Notepad fixture。显式追加 `--mailbox-readonly` 时只调用一次统一窗口管理层，优先复用或恢复现有 Profile 窗口，并验证运行时窗口绑定及服务域名；它不会每次额外创建三个邮箱窗口，也不会关闭用户原有窗口或打开邮件。
- 截图、Python 缓存和 smoke artifacts 已由 `.gitignore` 排除。
