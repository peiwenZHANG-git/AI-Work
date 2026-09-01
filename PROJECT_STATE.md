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
- Outlook 一次性 OAuth 登录已提交：authorization code + PKCE、精确回环回调、state 校验和专用 refresh token 写入。
- OAuth 一次性登录与自动刷新共用跨进程锁；token 交换失败不会覆盖既有凭据。
- 摘要 HTML 与状态 JSON 使用临时文件加原子替换；`last-run.json` 记录 `ok`、邮箱健康、计数和 Toast 状态。
- 本机只读健康检查已提交：检查环境变量和凭据存在性、28 个 MCP 工具注册、计划任务、最近摘要状态和助手服务；凭据值读取后立即丢弃且不输出。
- 安全凭据配置 CLI 已提交：隐藏提示、白名单目标、双重输入确认，且不在 argv、日志或输出中保留密钥。
- 计划任务恢复安装器已提交：幂等注册每日摘要任务，支持 10:00/22:00 双触发、禁止并发、1 小时超时和 `--dry-run`；安装本身不触发邮件读取。
- 计划任务定义只读校验已提交：`--check` 比较动作、参数、工作目录、触发时间和执行限制；健康检查将其作为必需项。
- 计划任务定义漂移已修复：真实任务现在使用带引号脚本参数和 `PT1H` 执行限制；`--check` 通过。
- 锁冲突可见性已提交：计划任务摘要因运行锁被占用而跳过时返回 `ok=false`、`reason=lock_busy`，不再把未更新的 `last-run.json` 伪装成成功。
- 助手服务输入边界已提交：变异 POST 的 JSON 主体限制为 256 KiB；无效、负数或超限 `Content-Length` 在读取请求体前拒绝。
- 助手字段校验已提交：收件人必须是单个普通邮箱地址并限制长度；主题去除换行、正文拒绝 NUL，AI 指令与生成字段也有上限。无效收件人会在访问凭据、IMAP、SMTP 或 Graph 前失败。
- 两阶段草稿发送已提交：先保存/校验草稿并返回一次性引用；用户再次确认后只发送该已存在草稿。IMAP 校验 folder/UID/UIDVALIDITY/哈希与收件人、主题、正文、发件人；Graph 校验身份和草稿元数据。
- 待发送引用 TTL/容量已提交：15 分钟过期、进程内最多 16 项；过期、重复使用或字段修改会显式失败。
- 摘要诊断已提交：每次运行在开始、读取邮箱、artifact 写入、完成或异常阶段记录 `last-attempt.json`；内容仅含阶段、状态、计数和错误类型。Windows `os.replace` 增加重试和完整临时文件回退。
- 健康检查已提交：`last_mail_digest` 可附加 `last-attempt.json` 的阶段/错误类型，便于区分状态未更新与实际失败阶段。
- 计划任务电源策略校验已提交：`--check` 覆盖 `DisallowStartIfOnBatteries` 和 `StopIfGoingOnBatteries`。
- 助手错误边界已提交：未预期 500 响应只返回 `internal_server_error`，不向页面泄漏原始异常细节。

## 4. 当前工作（待提交）

- 两阶段草稿状态校验（待提交）：发送前确认 Graph `isDraft=true` 或 IMAP `\Draft` 标记；非草稿消息会显式失败，不会误发。
- `.vscode/mcp.json`：本机 GitHub MCP 配置，按既定决定保持本地修改，不提交、不还原。
- 当前工作树验证基线（2026-09-01 实测）：`python -m compileall -q windows_gui_mcp.py windows_gui tests scripts` 通过；`python -m unittest discover -s tests -t . -v` 共 275 项全部通过；`mcp.list_tools()` 确认 28 个工具；`git diff --check` 通过。

## 5. 已知问题与阻塞

- Outlook 一次性登录命令已具备，但本会话未执行真实 Microsoft 登录或用真实租户验证端到端授权。已有 refresh token 时，摘要/助手会安全刷新并轮换。摘要/搜索需要 `Mail.Read` 凭据，草稿需要 `Mail.ReadWrite`，发送还需要 `Mail.Send`。
- 2026-09-01 检查确认：3 个助手专用授权码条目缺失；计划任务 10:00 运行返回 `1`，`last-run.json` 仍未更新。诊断修复将在 22:00 运行时生效，用于记录失败阶段。缺失凭据需要用户提供授权码，不能自动输入或猜测。
- 本科网易暂不支持经 Edge 发送已有草稿（draft hash 无法稳定定位并校验）；QQ 发送为设计性禁止。
- Edge 摘要/搜索回退只解析当前已验证页面的可见列表，不代表完整邮箱索引，也不打开正文。
- 本次会话未运行真实桌面 smoke test（按仓库规则需用户明确授权）。
- 当前运行的助手服务进程早于 JSON 主体上限修复；需要用户显式重启后才会加载该修复。本会话不自动关闭或重启服务。
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

1. 22:00 计划任务运行后读取 `last-attempt.json` 和 `last-run.json` 判断失败阶段；用户配置 3 个助手专用授权码后重新运行健康检查，再用显式授权执行一次真实 Outlook 登录。
2. 每次提交前运行规定验证：compileall、完整单元测试、28 工具注册检查、`git diff --check`。
3. 真实桌面验证仅在用户明确授权后运行 `python tests/smoke_test.py`；邮箱只读 smoke 仅在另行授权时使用 `--mailbox-readonly`。
4. 后续增强：在用户授权的交互会话中执行一次真实 Outlook 登录；本科网易 Edge 发送仅在能稳定定位并校验既有草稿后再实现。

## 8. 最近一次更新

2026-09-01（Europe/Paris）
