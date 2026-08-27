# PROJECT_STATE

## 1. 当前项目目标

维护一个仅面向 Windows 的 FastMCP 桌面自动化服务器，在保持既有公共接口兼容的前提下，提供安全、可测试的鼠标、键盘、窗口、截图、UI Automation、菜单及邮箱操作能力。

当前兼容性基线包括：保留 `windows_gui_mcp.py` stdio 入口、共享服务器名 `windows-gui` 和导出的 `mcp` 对象；28 个 MCP 工具必须恰好注册一次；PyAutoGUI `FAILSAFE` 保持开启；ASCII 文本继续使用 PyAutoGUI，非 ASCII 文本继续使用 Windows `SendInput` Unicode 路径。

邮箱功能的目标是在固定 Edge Profile 和最小权限边界内提供 READ、DRAFT、SEND 流程：所有发送必须先创建草稿并获得显式确认，QQ 邮箱禁止发送，任何身份或服务域名无法确认的情况均应停止处理。

## 2. 当前已完成功能

- 28 个 FastMCP 工具已注册，覆盖鼠标、截图、键盘、窗口发现与聚焦、UIA 控件、菜单、保存对话框和邮箱操作。
- 生产代码已按职责拆分到 `windows_gui/`，同时保留 `windows_gui_mcp.py` 兼容入口和公共工具重导出。
- 键盘输入支持 ASCII 的既有 PyAutoGUI 路径，以及非 ASCII/emoji 的原生 Windows `SendInput` 路径。
- 已实现三个固定邮箱身份的 Edge Profile 启动、运行时 HWND 绑定、窗口复用、Profile/服务域名校验和并发启动保护。
- 已实现统一的只读今日邮件摘要和邮件搜索；Outlook 可优先使用 Microsoft Graph，Graph 不可用时按规则回退 Edge。
- 已实现统一草稿创建；Outlook 可使用 Graph，Graph 不可用时可回退经验证的 Edge 页面，本科网易和 QQ 使用 Edge 草稿路径。
- 已实现已有草稿的确认发送工具；当前实际发送后端为 Outlook Graph，且发送前会核验身份、草稿归属、收件人和主题。
- 最近提交为 `2a57a71`（2026-08-27，`Detect mailbox summary parse failures`）。该提交已完成邮件摘要状态判定修正：区分 `MAIL_LIST_NOT_FOUND`、`MAIL_ITEMS_NOT_PARSED` 和经解析确认的 `EMPTY_TODAY`，避免把 UIA 解析失败误报为今日零邮件。
- `2a57a71` 同时提交了 `windows_gui/mail_summary.py`、`tests/test_mail_summary.py`、`README.md` 和 `AGENTS.md` 的对应代码、回归测试、行为说明及安全规则更新。
- 更早的近期提交已完善本科网易邮箱导航与就绪检查、邮箱窗口复用与域名验证、确认后的草稿发送、统一草稿创建和统一只读搜索。
- 当前无桌面副作用验证流程包括语法编译检查、完整单元测试、独立的 28 工具注册检查和 `git diff --check`。

## 3. 正在进行的工作

- 工作区正在开发一个面向本科网易和 QQ 邮箱的只读浏览器 DOM 后端：通过显式配置的本机回环 CDP endpoint 连接现有 Edge，读取经过清理的邮件列表元数据，不点击邮件或打开正文。
- 该工作涉及新增 `windows_gui/browser_mail.py` 和 `tests/test_browser_mail.py`，修改 `windows_gui/mail_backends.py`、`windows_gui/mail_summary.py` 以增加浏览器后端状态、消息引用和摘要分发，调整 `tests/test_mail_backends.py`、`tests/test_mail_summary.py` 的分发与状态断言，并更新 `README.md`、`AGENTS.md` 的依赖、行为和安全边界说明。
- 上述浏览器 DOM 后端仍为未提交工作，本状态文档不将其描述为已完成功能。
- `.vscode/mcp.json` 保留本机 GitHub MCP stdio 配置修改，不计划提交。

## 4. 当前阻塞或已知问题

- Outlook Graph 尚未内置交互式 OAuth 登录或 token refresh 流程；摘要/搜索需要既有 `Mail.Read` 凭据，草稿需要 `Mail.ReadWrite`，发送还需要 `Mail.Send`。
- 本科网易暂不支持通过 Edge 发送已有草稿，因为现有 Edge draft hash 无法稳定定位并校验草稿；QQ 邮箱按权限设计禁止发送。
- Edge 摘要和搜索 fallback 仅解析当前已验证页面的可见邮件列表，不代表完整邮箱索引，也不打开正文。
- 本次更新未运行真实桌面 smoke test；依据仓库安全规则，真实 GUI 验证只能在用户授权后使用 `tests/smoke_test.py` 的专用 Notepad fixture。当前仅确认无桌面副作用的编译和单元测试通过。
- 工作区存在已忽略的运行时文件/目录（例如 `screen.png`、`__pycache__/`）；它们不在 Git 未提交修改列表中，也未被本次操作清理。

## 5. 未提交修改

当前 `main` 与 `origin/main` 均指向 `2a57a71`。除本状态文档外，工作区存在以下未提交修改：

- `.vscode/mcp.json`：新增本机 GitHub MCP server 配置；按当前决定保持本地修改，不提交、不还原。
- `AGENTS.md`：增加 Browser DOM/CDP 模块归属、只读数据边界和 loopback-only 安全规则。
- `README.md`：记录可选 Playwright 依赖、Browser DOM/CDP 摘要行为、状态码及安全限制。
- `tests/test_mail_backends.py`：调整本科网易和 QQ 邮箱的后端分发测试，断言使用浏览器 DOM 后端。
- `tests/test_mail_summary.py`：调整浏览器后端未配置时的摘要状态断言。
- `windows_gui/mail_backends.py`：增加浏览器后端相关状态，以及可选的安全消息引用字段。
- `windows_gui/mail_summary.py`：为本科网易和 QQ 邮箱接入浏览器 DOM 只读摘要后端及状态映射。
- `windows_gui/browser_mail.py`：新增的浏览器 DOM 只读后端实现，尚未被 Git 跟踪。
- `tests/test_browser_mail.py`：新增的浏览器 DOM 后端测试，尚未被 Git 跟踪。

`PROJECT_STATE.md` 将由独立提交记录；上述其他文件不会包含在该提交中。

## 6. 下一步建议

1. 继续完成浏览器 DOM 只读后端及其测试，并在单独评审后决定是否提交；保持现有 28 个 MCP 工具的公开接口和注册数量不变。
2. `.vscode/mcp.json` 继续作为本机配置保留，不纳入项目提交。
3. 每次提交前运行规定的编译检查、完整单元测试、28 工具注册检查和 `git diff --check`。
4. 如需真实桌面验证，先获得明确授权，再运行 `python tests/smoke_test.py`；邮箱只读 smoke 仅在另行授权时使用 `--mailbox-readonly`。
5. 后续若要扩展发送能力，优先解决 Outlook OAuth/refresh 生命周期；本科网易 Edge 发送只有在能够稳定定位并重新校验既有草稿后再实现。

## 7. 最近一次更新日期

2026-08-27（Europe/Paris）
