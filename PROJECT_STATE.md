# PROJECT_STATE — AI-Work（Windows GUI MCP）

- 项目名称：AI-Work — 仅面向 Windows 的 FastMCP 桌面自动化服务器
- 仓库路径：`D:\21781\Documents\Codex\AI-Work`
- 远程仓库：`https://github.com/peiwenZHANG-git/AI-Work.git`（origin）
- 状态基线：本文档内容核实于 2026-08-31；Git 当前 HEAD 与分支指向请实时查询（如 `git rev-parse main origin/main`），本文档不记录会随提交立即过时的动态 hash。
- 维护规则：开始新的重要开发任务前先阅读本文档；完成影响项目状态的重要工作后更新本文档。只记录恢复上下文所需信息，不记录微小修改；记录前须用仓库、Git 历史和验证输出核实。

## 1. 当前目标

维护一个仅面向 Windows 的 FastMCP 桌面自动化服务器，在保持既有公共接口兼容的前提下，提供安全、可测试的鼠标、键盘、窗口、截图、UI Automation、菜单及邮箱操作能力。

兼容性基线：保留 `windows_gui_mcp.py` stdio 入口、服务器名 `windows-gui` 和导出的 `mcp` 对象；28 个 MCP 工具恰好注册一次；PyAutoGUI `FAILSAFE` 开启；ASCII 文本走 PyAutoGUI，非 ASCII 文本走原生 `SendInput`。

邮箱目标：在固定 Edge Profile 与最小权限边界内提供 READ、DRAFT、SEND 流程；所有发送必须先创建草稿并获得显式确认；QQ 邮箱永不发送；身份或服务域名无法确认时立即停止处理。

## 2. 架构

- `windows_gui_mcp.py` 是向后兼容入口并重导出全部公共工具；FastMCP 实例与进程级设置在 `windows_gui/server.py`；28 个工具恰好注册一次（2026-08-31 重新确认）。
- 模块职责遵循 `AGENTS.md`：鼠标/截图 `mouse.py`、键盘 `keyboard.py`、窗口 `windows.py`、UIA/菜单/保存对话框 `uia.py`；邮箱链路为 `mailboxes.py`（固定身份与 Edge 启动）、`mail_summary.py`（只读摘要与就绪校验）、`imap_mail.py`（QQ/网易 IMAP 只读传输）、`browser_mail.py`（可选 CDP/DOM 传输）、`mail_backends.py`（后端分发）、`mail_search.py`、`mail_draft.py`、`mail_send.py`。
- 邮件后端优先级：Outlook（master_mail）优先 Microsoft Graph，失败回退经验证的 Edge 页面；QQ 与本科网易摘要优先 IMAP 只读后端，可选启用回环 CDP 浏览器 DOM 后端。
- 摘要与助手：`mail_digest.py` + `scripts/daily_mail_digest.py` 由计划任务触发；`mail_assistant.py` + `mail_assistant_page.py` + `scripts/mail_assistant_server.py` 提供本机 8931 网页。助手 Web 层校验 `Host`、`Origin` 和 JSON Content-Type；本科 SMTP 发送前先保存 IMAP 草稿。
- Outlook 一次性登录：`master_oauth.py` + `scripts/authenticate_master_mail.py` 提供 authorization code + PKCE、回环回调、state 校验和 refresh token 专用写入。

## 3. 已完成功能（已提交）

- 28 个 MCP 工具覆盖鼠标、截图、键盘、窗口发现/聚焦、UIA 控件、菜单、保存对话框和邮箱操作；键盘支持 ASCII 的 PyAutoGUI 路径与非 ASCII/emoji 的原生 `SendInput` 路径。
- 三个固定邮箱身份（`bachelor_mail`、`master_mail`、`qq_mail`）的 Edge Profile 启动、运行时 HWND 绑定、窗口复用、Profile/服务域名校验和并发启动保护。
- 统一只读今日邮件摘要与搜索、统一草稿创建、已确认草稿发送（实际发送后端为 Outlook Graph）；`2a57a71` 修正摘要状态语义，区分 `MAIL_LIST_NOT_FOUND`、`MAIL_ITEMS_NOT_PARSED` 和经解析确认的 `EMPTY_TODAY`。
- `ea522f3`：可选浏览器 DOM/CDP 只读后端（仅回环、仅列表元数据、hashed 不透明引用），并同步了 AGENTS/README 安全规则。
- `a0d6a92` / `0504b7e`：QQ 与本科网易 IMAP 只读后端（SSL、EXAMINE、UID、BODY.PEEK 头部；凭据走专用 Credential Manager 条目，本科用户名走环境变量）。
- `c4a648e`：`PROJECT_STATE.md` 纳入仓库。
- 验证基线（2026-08-30 实测）：`python -m compileall` 通过；`python -m unittest discover -s tests -t .` 189 项全部通过；工具注册数确认 28。
- 每日邮件摘要 + AI 助手已提交：QQ/网易 IMAP 只读正文、Graph 只读摘要、GLM 摘要/翻译、计划任务、本机 8931 助手页、草稿保存和已确认本科 SMTP 发送。
- 助手 QQ/本科草稿和本科 SMTP 已改为独立 Credential Manager 授权码；只读摘要授权码没有草稿或发送路径。
- Outlook refresh 生命周期已提交：读取、Graph 交换、旋转写回由跨进程 Windows mutex 串行化；助手和摘要共用同一函数，access token 只留在内存。

## 4. 当前工作（待提交）

- Outlook 一次性登录增强（待提交）：`master_oauth.py` 使用 PKCE、精确 `/callback`、精确回环 Host、OAuth `state` 和 S256 code challenge；无效回调不会取消等待。成功时只写回 `master_mail_graph_refresh_token`，失败不覆盖既有凭据。
- `scripts/authenticate_master_mail.py` 是显式交互命令，支持 `--no-open`、`--port` 和 `--timeout`；浏览器启动只发生在该命令中，单元测试全部 mock。
- `AGENTS.md` 与 README 已同步 OAuth 安全边界；保持 28 个 MCP 工具接口不变。
- `.vscode/mcp.json`：本机 GitHub MCP 配置，按既定决定保持本地修改，不提交、不还原。
- 当前工作树验证基线（2026-08-31 实测）：`python -m compileall -q windows_gui_mcp.py windows_gui tests scripts` 通过；`python -m unittest discover -s tests -t . -v` 共 220 项全部通过；`mcp.list_tools()` 确认 28 个工具；`git diff --check` 通过。

## 5. 已知问题与阻塞

- Outlook 一次性登录命令已具备，但本会话未执行真实 Microsoft 登录或用真实租户验证端到端授权。已有 refresh token 时，摘要/助手会安全刷新并轮换。摘要/搜索需要 `Mail.Read` 凭据，草稿需要 `Mail.ReadWrite`，发送还需要 `Mail.Send`。
- 本科网易暂不支持经 Edge 发送已有草稿（draft hash 无法稳定定位并校验）；QQ 发送为设计性禁止。
- Edge 摘要/搜索回退只解析当前已验证页面的可见列表，不代表完整邮箱索引，也不打开正文。
- 本次会话未运行真实桌面 smoke test（按仓库规则需用户明确授权）。
- 工作区存在已忽略的运行时文件/目录（例如 `screen.png`、`__pycache__/`），未清理。

## 6. 重要技术决策

- 公共接口冻结：服务器名 `windows-gui`、导出 `mcp`、28 个工具不得增删改名，不得修改签名或返回形状，除非用户明确批准。
- 邮箱权限矩阵：`bachelor_mail`/`master_mail` = READ+DRAFT+SEND；`qq_mail` = READ+DRAFT（永不 SEND）；每次 SEND 必须先建草稿并获显式确认。
- 身份绑定优先显式 `--profile-directory` 启动的运行时 HWND；进程重启后按 Edge PID 命令行找精确 Profile，必要时允许配置的 Profile 标题后缀回退；永不从页面 UIA 文本推断 Profile。
- IMAP 只读约束：SSL、EXAMINE、UID、BODY.PEEK；禁止 STORE/MOVE/COPY/EXPUNGE。凭据只存 Windows Credential Manager `AI-Work/windows-gui/mailboxes`；本科 IMAP 用户名读 `AI_WORK_BACHELOR_IMAP_USERNAME`。
- CDP 仅限显式 opt-in + 回环地址；浏览器 DOM 提取只返回发件人/主题/时间/哈希引用，不查询正文、不点击、不改已读状态。
- 零行不等于零邮件：无可信列表容器报 `MAIL_LIST_NOT_FOUND`，有行但解析失败报 `MAIL_ITEMS_NOT_PARSED`，至少解析一行且无今日邮件才报 `EMPTY_TODAY`。
- 摘要/助手读取正文仅用 IMAP `BODY.PEEK` 或 Graph 只读，不改变已读状态；AI 中文摘要调用 Zhipu GLM（`glm-4-flash`）。

## 7. 下一步

1. 评审并提交一次性 Outlook OAuth 登录及其测试；保持 28 个工具的公开接口不变。
2. 提交同步后的 `AGENTS.md`、README 和 `PROJECT_STATE.md`，不与代码改动混在同一提交。
3. 每次提交前运行规定验证：compileall、完整单元测试、28 工具注册检查、`git diff --check`。
4. 真实桌面验证仅在用户明确授权后运行 `python tests/smoke_test.py`；邮箱只读 smoke 仅在另行授权时使用 `--mailbox-readonly`。
5. 后续增强：在用户授权的交互会话中执行一次真实 Outlook 登录；本科网易 Edge 发送仅在能稳定定位并校验既有草稿后再实现。

## 8. 最近一次更新

2026-08-31（Europe/Paris）
