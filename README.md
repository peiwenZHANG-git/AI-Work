# Windows GUI MCP Server

这是一个仅面向 Windows 的 FastMCP Server，为 AI 客户端提供鼠标、键盘、窗口聚焦、截图、Windows UI Automation 和菜单操作能力。

项目保留 `windows_gui_mcp.py` 作为兼容启动入口，具体实现按职责放在 `windows_gui/` 包中：

- `server.py`：共享 FastMCP 实例和 PyAutoGUI 安全设置。
- `mouse.py`：鼠标、滚轮、拖动和截图。
- `keyboard.py`：文本输入、按键和快捷键。
- `windows.py`：窗口枚举、聚焦及聚焦后的输入操作。
- `uia.py`：UI Automation 控件、菜单和保存对话框操作。
- `mailboxes.py`：固定邮箱身份、权限边界和 Edge Profile 启动逻辑。
- `mail_summary.py`：邮箱身份和页面验证、只读列表解析、今日摘要及重要事项分类。

## 环境要求

- Windows 10 或 Windows 11
- Python 3.10 或更高版本
- 可交互的桌面会话

安装依赖：

```powershell
python -m pip install fastmcp pyautogui pywin32 pywinauto pillow
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

当前服务器注册 25 个工具。

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
| `summarize_all_mailboxes_today` | 依次验证三个邮箱的 Edge Profile 与页面，并只读汇总今天最近最多 10 封可识别邮件及重要事项。 |

## 固定邮箱身份

邮箱身份配置只包含非敏感元数据，不保存密码、Cookie、sid、token、会话链接或其他登录凭证。

| 身份 | 显示名称 | Edge Profile | 服务 | 稳定 URL | 权限 |
|---|---|---|---|---|---|
| `bachelor_mail` | 本科邮箱 | `Profile 1` | 网易企业邮箱 | 未配置；工具只打开指定 Profile，不猜测地址 | READ、DRAFT、SEND |
| `master_mail` | 硕士邮箱 | `Profile 2` | Outlook Web | `https://outlook.office.com/mail/` | READ、DRAFT、SEND |
| `qq_mail` | QQ邮箱 | `Profile 3` | QQ Mail | `https://mail.qq.com/` | READ |

所有发送动作都必须先生成草稿并等待用户确认。QQ 邮箱默认只读。删除、移动、标记或归档邮件前必须获得用户确认。任何邮箱操作都必须先核对邮箱身份与指定 Profile；无法确认时立即停止，禁止猜测。自动输入密码以及记录登录凭证、Cookie、token 或会话链接均被禁止。

`open_all_mailboxes()` 和摘要工具共享内部 `get_or_open_mailbox_window()` 管理层。每个邮箱在 Agent 中最多绑定一个 Edge HWND：有效运行时绑定返回 `REUSED_EXISTING_WINDOW`；Server 重启后优先通过窗口 PID 和进程命令行中的 `--profile-directory` 找回窗口。Edge 复用同一浏览器进程、主命令行不含 Profile 参数时，使用 Edge 浏览器标题中精确的 Profile 显示名称后缀恢复绑定，不从页面 UIA 内容猜测。恢复返回 `RESTORED_WINDOW_BINDING`；只有未找到对应 Profile 窗口时才返回 `CREATED_NEW_WINDOW`。该逻辑不会关闭用户原本打开的重复窗口。

本科邮箱没有稳定 URL。窗口管理层会优先在 Profile 1 的现有窗口中选择主机名为 `mailh.qiye.163.com` 的已登录页面；否则只复用或恢复 Profile 1 状态并返回 `PAGE_NOT_READY`，提示用户“请在本科邮箱 Profile 中人工打开一次本科邮箱页面”。完整网易 URL、sid 和其他会话材料不会被保存、记录或复用。

`summarize_all_mailboxes_today()` 严格按本科、硕士、QQ 邮箱顺序执行。Profile 身份来自本进程使用 `--profile-directory` 启动窗口时建立的内存绑定，不再由 UIA 页面内容反推。UIA 只读取地址栏并立即提取主机名，用于精确验证 `mailh.qiye.163.com`、`outlook.office.com`（以及该入口当前重定向到的 `outlook.cloud.microsoft`）或 `mail.qq.com`；完整 URL 不会被保存、记录或返回。运行时 Profile 与服务域名不一致时返回 `IDENTITY_MISMATCH` 并在读取邮件前停止；窗口无法绑定时返回 `NOT_READY`。运行时绑定不会写入磁盘，Agent 重启后必须重新启动对应 Profile。

摘要读取使用有 5 秒边界的只读 UI Automation 查询，不聚焦或点击邮件。当前实现只从可见邮件列表中识别发件人、主题和时间，最多 10 封，并据此生成简短摘要；不会为取得正文而打开邮件，因此返回的 `read_state_change` 为 `NONE`。页面已就绪但当前 UIA 列表没有可识别的今日邮件时，数量为 0。重要事项只做分类和摘要，不执行写操作。

## 安全说明

- GUI 操作会影响当前交互式桌面。调用前应确认目标窗口标题足够具体。
- 自动化测试必须 mock 所有真实鼠标、键盘和 UIA 副作用。
- 真实 GUI 验证应只使用 `tests/smoke_test.py` 创建的专用文件和窗口。
- 默认 Smoke test 只操作专用 Notepad fixture。显式追加 `--mailbox-readonly` 时只调用一次统一窗口管理层，优先复用或恢复现有 Profile 窗口，并验证运行时窗口绑定及服务域名；它不会每次额外创建三个邮箱窗口，也不会关闭用户原有窗口或打开邮件。
- 截图、Python 缓存和 smoke artifacts 已由 `.gitignore` 排除。
