# PROJECT_STATE — AI-Work（Windows GUI MCP）

- 项目名称：AI-Work — 仅面向 Windows 的 FastMCP 桌面自动化服务器
- 仓库路径：`D:\21781\Documents\Codex\AI-Work`
- 远程仓库：`https://github.com/peiwenZHANG-git/AI-Work.git`（origin）
- 状态基线：本文档内容核实于 2026-09-04；Git 当前 HEAD 与分支指向请实时查询（如 `git rev-parse main origin/main`），本文档不记录会随提交立即过时的动态 hash。
- 维护规则：开始新的重要开发任务前先阅读本文档；完成影响项目状态的重要工作后更新本文档。只记录恢复上下文所需信息，不记录微小修改；记录前须用仓库、Git 历史和验证输出核实。

## 1. 当前目标

维护一个仅面向 Windows 的 FastMCP 桌面自动化服务器，在保持既有公共接口兼容的前提下，提供安全、可测试的鼠标、键盘、窗口、截图、UI Automation、菜单及邮箱操作能力。

兼容性基线：保留 `windows_gui_mcp.py` stdio 入口、服务器名 `windows-gui` 和导出的 `mcp` 对象；经用户于 2026-09-02 明确批准扩展后，36 个 MCP 工具恰好注册一次；原有 28 个工具的签名和返回结构保持不变；PyAutoGUI `FAILSAFE` 开启；ASCII 文本走 PyAutoGUI，非 ASCII 文本走原生 `SendInput`。

当前开发重点：邮箱能力已达用户可接受的成熟度，进入 maintenance mode；Remote loopback MVP、统一任务/确认中心和 Phase 3B-5 LAN readiness 已完成实现。LAN 默认关闭，真实跨设备 smoke 尚未获批准；下一步只在用户再次明确批准后进行受控跨设备验证。

邮箱稳定边界：继续保留固定 Edge Profile、READ/DRAFT/SEND 最小权限流程；所有发送必须先创建草稿并获得显式确认，QQ 邮箱永不发送，身份或服务域名无法确认时立即停止处理。

## 2. 架构

- `windows_gui_mcp.py` 是向后兼容入口并重导出全部公共工具；FastMCP 实例与进程级设置在 `windows_gui/server.py`；当前 36 个工具恰好注册一次。
- 通用网页能力分为 `browser_download.py`（公共 URL/主机地址校验、逐跳重定向检查、Edge 启动、无登录态原子下载）和 `browser_session.py`（专用工作线程持有 Playwright 持久 Edge 上下文、网络路由守卫、DOM 检查、确认式点击、带上限的登录态原子下载）；会话使用独立 Agent Profile，不复用邮箱 Profile。
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
- 本机只读健康检查已提交：检查环境变量和凭据存在性、36 个 MCP 工具注册、计划任务、最近摘要状态和助手服务；凭据值读取后立即丢弃且不输出。
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
- IMAP staging 稳定引用校验已提交：staging 要求 APPENDUID，并只读重读消息以校验 `\Draft`、发件人、收件人、主题、正文、SHA-256、folder/UID/UIDVALIDITY；缺少稳定 UID 会显式失败。
- 两阶段草稿状态校验已提交：发送前确认 Graph `isDraft=true` 或 IMAP `\Draft` 标记；非草稿消息会显式失败，不会误发。
- SMTP 旁路清理已提交：移除助手 `send_mail_smtp()` 的“新字段即时构建并发送”路径，只保留 `send_existing_email_smtp()` 发送已取回并校验的 `EmailMessage`。
- 助手服务并发与响应头加固已提交：后台刷新启动由互斥锁保护；HTML 响应增加 `nosniff`、`no-referrer`、`SAMEORIGIN` 和 CSP。
- 通用浏览器/下载已实现并注册 8 个 MCP 工具：公共网页打开和无登录态原子下载，以及使用独立 Agent Profile 的持久 Playwright Edge 会话启动、导航、页面检查、确认式点击、登录态原子下载和停止。URL 输出去除查询串/片段，检查不返回 Cookie 或输入框值，下载默认不覆盖并返回 SHA-256；公共 URL 与 Playwright 路由都会解析目标主机并拒绝非公共地址，公共下载逐跳校验重定向，两类下载均有 256 MiB 上限；服务器现固定注册 36 个工具，原有 28 个接口保持兼容。
- 邮件摘要与助手已支持单邮件本地隐藏（不修改邮箱服务器状态）、30 天/5000 项清理、`/api/dismiss`、本地搜索与邮箱/重要程度/日期筛选，以及附件名称、MIME 类型和大小展示；IMAP 不解码附件，Graph 不请求 `contentBytes` 并过滤内嵌附件。每封卡片有独立“已读”按钮，按钮仅在持久化成功后隐藏；IMAP UID / Graph message ID 的本地哈希作为稳定隐藏标识，正文变化后不会重新出现，同时兼容旧内容哈希记录；并发写入使用进程锁避免丢失。为关闭长刷新竞态，AI enrichment 完成后、写 HTML 前会再次过滤；点击后原子更新当前 HTML，`/digest` 返回旧 artifact 前也会即时过滤。
- 摘要读取与 AI enrichment 已并发化并保持固定邮箱显示顺序；AI 缓存改为整批落盘，重要程度政策升级到版本 2，高重要性检查不再进行无用翻译。助手启动器仅识别精确脚本进程，并提供 `--restart` / `--no-refresh` 安全选项。
- AI 写信的草稿生成已与摘要请求参数解耦：默认使用快速 `glm-4-flash`，可由 `AI_WORK_DRAFT_MODEL` 单独覆盖；每次只发起一次请求，10 秒硬超时，最大输出 1200 tokens，网络失败不再自动重试导致长时间卡住。助手 HTML/JSON 响应现强制 `no-store`，页面 AI 请求有 22 秒客户端截止和明确超时错误，避免旧页面缓存或前端无限等待。远程 AI 超时、网络失败或响应格式无效时，服务端会自动返回可编辑的本地基础草稿并在页面标明兜底，不再出现无草稿的终态。2026-09-02 本机经同一 `/api/ai-draft` 真实接口多次验证成功，最新一次约 3.6 秒返回 AI 草稿，未保存或发送邮件。
- AI 写信页面的无法生成根因已修复：脚本调用 `setStatus()` 时依赖 `#status`，但 HTML 曾缺少该 DOM 元素，导致点击后立即抛出 JavaScript 异常，`/api/ai-draft` 请求根本没有发出。现已增加带 `role=status`/`aria-live=polite` 的状态节点和防回归测试；用无界面 Edge 按真实流程验证“切换 AI 标签→输入→点击生成→填入主题/正文”成功，HTTP 200，约 4.59 秒，未保存或发送邮件。
- 本地健康事件已接入摘要运行与 AI 草稿关键结果：事件采用固定组件、结果、代码和摘要白名单，不接收调用方详情或异常原文，并以 512 KiB、最多 3 个轮转文件限制磁盘占用；读写由线程锁与有界 Windows 跨进程 mutex 串行化，任何日志锁、文件或解码故障都只安全降级，不会中断摘要或写信主流程。共享只读健康模型覆盖 MCP 稳定接口、凭据存在性、摘要、助手服务、浏览器/CDP 和 Remote 六类状态；无法可靠解析本地化计划任务输出时返回 `UNKNOWN` 而非误报 `FAIL`。助手页新增“系统状态”面板及脱敏的最近错误，CLI 与页面共用四态模型，不新增 MCP 工具，也不主动启动服务、浏览器或网络探测。
- 助手重启可靠性已修复：端口等待现在正确区分“等待关闭”和“等待监听”，并会在有界时间内重试精确脚本 PID 的 WMI 查询；仍只终止命令行明确运行 `mail_assistant_server.py` 的进程。真实 `--restart --no-refresh --no-open` 验证一次成功，未刷新邮箱，随后 `/api/status` 与 `/api/health` 均返回 HTTP 200。
- 通用浏览器真实验收已通过一次受控 smoke：使用专用 `browser-agent-profile` 启动非 headless Edge，访问 Selenium 公共 downloads 测试页，检查 URL/DOM 脱敏，点击普通只读链接，通过 `File 1` 完成登录态下载路径的 14 字节文件下载并核对 SHA-256，确认不覆盖已有文件；三个私网导航负例全部被拒绝，stop 后导航显式失败。未使用邮箱 Profile、未提交表单或执行生产副作用。
- 邮箱工作流下一阶段已实现且不改变 36 工具 MCP 接口：摘要卡片携带本机渲染元数据和“AI 回复”入口，助手可根据最新本地摘要卡片生成可编辑回复草稿，但不会自动保存或发送；`/api/today-todos` 只解析最近一次本地摘要，提取截止、需办理、学校行政和高重要度事项，不访问或修改邮箱；`/api/mail-search` 把常见中文时间/关键词解析为参数后复用现有只读 `search_mailboxes()` 元数据搜索。QQ 发送仍然禁止，所有发送仍复用两阶段确认。
- 统一任务/确认中心 Phase 1 已实现且不改变 36 个 MCP 工具接口：内部 TaskCenter 提供固定 domain/action 白名单、有界 TTL/容量、锁保护、单次消费、终态稳定和服务端专用 verified context；mail assistant 待发送引用已迁移为首个消费者。
- 统一任务/确认中心 Phase 2（Browser Agent 内部强化）已实现且不改变 36 个 MCP 工具接口：浏览器 worker 具备有界恢复与停后不复活；确认式点击经 TaskCenter 暂存、元素标记/指纹、session/导航 epoch 校验和单次消费执行；DNS rebinding 防护覆盖私网、回环、reserved 与过渡地址并逐请求 fail closed。
- Remote Phase 3A（架构与威胁模型）已完成并落库 `docs/REMOTE_ARCHITECTURE.md`：Remote 是传输层而非权限层，依赖方向为 Remote transport → auth/policy → TaskCenter stage → 本地确认平面 → domain adapter → 副作用原语；配对不等于确认，Remote 无 execute/consume/complete 能力且永不接触 verified_context。
- Remote Phase 3B-1（协议与认证原语）已完成且无网络监听：包含一次性 pairing code、每设备 Credential Manager secret、HMAC-SHA256 请求签名、nonce/时间窗、有界会话与幂等缓存、固定命令白名单和滑动窗口限速。
- Remote Phase 3B-2（loopback-only health server）已完成且不改变 36 个 MCP 工具接口：服务器只绑定 127.0.0.1，提供 session/health/command/status 入口，强制精确 Host、拒绝 Origin、限制请求体并使用固定错误码与安全响应头。
- Remote Phase 3B-3 收口已完成且不改变 36 个 MCP 工具接口：`/command` 现要求显式绑定 device id 的 HMAC 加 Bearer session 双重认证；`session.revoke_self` 只撤销当前 session，按 `(device, request_id)` 幂等，旧 session 的新请求固定拒绝；审计仍用固定事件码并只记录哈希设备标识。
- Remote Phase 3B-4 已完成且不改变 36 个 MCP 工具接口：Remote 设备只能通过固定白名单向独立 TaskCenter stage browser click/download 或 mail draft 请求；设备只能查看、取消自己的任务，跨设备访问统一 fail closed；browser click 复用既有确认安全模型，download 限制在规定目录并脱敏本机路径，mail 只创建草稿且 QQ 永不发送；本地确认后响应不返回本地两阶段发送引用；撤销设备会取消其 STAGED 任务；Remote 仍无 confirm、execute、consume 或 complete API。
- Remote Phase 3B-4.1 本地确认加固已完成：确认操作改用 task/action 绑定的单次 local action token，每操作签发、有界 TTL、验证即原子消费，重放、过期、伪造、跨任务/跨动作误用和并发重放固定拒绝；本地确认平面仍仅 loopback，可防远程设备和浏览器跨站请求，但不构成强用户在场证明。
- Remote Phase 3B-4.2 已修复 action-token 绑定竞态：token 在锁内先校验 expiry，再以 constant-time 比较 task/action，完全匹配后才消费；错误 target/action 固定 403 且不会使正确 token 失效，确定性与并发回归测试均覆盖。
- Remote Phase 3B-5 已实现且不改变 36 工具 MCP 接口：LAN 默认 disabled，仅接受显式物理接口、RFC 1918 IPv4、Private profile、固定 8933 和允许子网；拒绝 wildcard/IPv6/Public/Unknown/VPN/hotspot/virtual，接口/IP/profile 改变即停止且不自动 rebind。8933 LAN handler 只暴露 TLS Remote API，`/local/...` 在结构上不存在；local pairing/confirmation/device management 独立绑定 `127.0.0.1:8934`。
- LAN TLS 使用 ECDSA P-256 自签身份、TLS 1.2 minimum、证书有效期检查和客户端 SPKI SHA-256 pinning；私钥、证书与 pin 元数据存 Credential Manager。HMAC、nonce、timestamp、session、request_id 与 TaskCenter/domain 权限边界不变。配对改为 pinned-TLS pending claim → 本地 approve → credential 创建 → 单次 complete；未批准不创建 credential，已批准但未领取的 credential 到期撤销。
- LAN 审计仅使用固定白名单事件；不接收 caller detail，不记录请求、IP、证书材料或 credential。Windows Firewall 仅提供人工审阅的 add/delete guidance，程序从不执行规则或请求提权。禁用配置并重启即可回退 loopback；session/pending pairing 随进程失效，设备 credential/TLS identity 保留到本地撤销或显式轮换。
2026-09-02 本机验收：助手已用 `--restart --no-refresh --no-open` 加载当前工作树，真实本地页面确认三个新入口存在，待办 API 返回 HTTP 200，AI 回复按钮默认禁用，搜索请求边界返回 400；未触发真实邮箱搜索、AI 生成、草稿保存、发送、删除、移动或标记。

## 4. 当前工作

- `.vscode/mcp.json`：本机 GitHub MCP 配置，按既定决定保持本地修改，不提交、不还原。
- 邮箱能力已进入 maintenance mode：没有进行中的邮箱功能扩展；后续变更仅由 bug、安全问题和真实使用反馈触发，并且必须继续满足既有邮箱安全边界。
- Remote Phase 3B-5 代码、测试与文档已完成；真实 LAN 开放、防火墙变更和跨设备 smoke 均未执行，等待下一次用户明确批准。
- 最新完整验证基线（2026-09-04 隔离 3B-5 worktree 实测）：`python -m compileall -q windows_gui_mcp.py windows_gui tests` 通过；`python -m unittest discover -s tests -t . -v` 共 558 项全部通过；新增 LAN/TLS/pending pairing/network focused tests 通过；独立 MCP 检查确认 36 个工具唯一注册；`git diff --check` 通过。全部测试使用 loopback、fake store/network 或临时证书，不读取真实邮箱、不调用真实 AI、不运行真实桌面或跨设备 smoke。

## 5. 已知问题与阻塞

- Outlook 一次性登录命令已具备，但本会话未执行真实 Microsoft 登录或用真实租户验证端到端授权。已有 refresh token 时，摘要/助手会安全刷新并轮换。摘要/搜索需要 `Mail.Read` 凭据，草稿需要 `Mail.ReadWrite`，发送还需要 `Mail.Send`。
- 2026-09-02 最新只读健康检查确认：三个助手专用授权码、两个摘要授权码、GLM API key 与 Outlook refresh token 共 7 个凭据条目均已配置；计划任务最近返回 `0` 且定义匹配；最近摘要三邮箱均为健康状态，`last-run.json` 与 `last-attempt.json` 正常更新。
- 本科网易暂不支持经 Edge 发送已有草稿（draft hash 无法稳定定位并校验）；QQ 发送为设计性禁止。
- Edge 摘要/搜索回退只解析当前已验证页面的可见列表，不代表完整邮箱索引，也不打开正文。
- 助手回复选择依赖最新本地摘要 artifact；旧 artifact 没有机器可读发件人地址时，回复可基于可见正文/摘要生成，但收件人需要用户手工填写，先刷新摘要可获得稳定地址。
- “今日待办”是从本地摘要 HTML 做的有界规则提取，能识别常见中文/学校事项和常见日期，但不是语义级完整任务抽取；AI 回复和搜索的远程结果仍需用户确认后再保存或发送。
- URL/重定向防护会在每次请求前解析并校验主机地址，但尚未把连接固定到该次解析结果；理论上恶意权威 DNS 仍可在校验后的第二次解析中尝试 rebinding。真实浏览器验收只应使用受控公共测试 URL。
- `open_webpage` 只校验用户提供的初始 URL；打开后的 HTTP 重定向由普通 Edge 跟随，不经过 Playwright context 路由逐跳防护。真实验收应使用固定公共 URL，不使用重定向链。
- 登录态下载的 256 MiB 上限会限制复制到目标目录的临时文件并阻止发布，但 Playwright 浏览器自身临时文件没有下载进度取消钩子，极端响应仍可能在超限判定前占用临时磁盘。
- 本次会话未运行真实桌面 smoke test（按仓库规则需用户明确授权）。
- 2026-09-02 真实浏览器 smoke 结束时曾因进行中的邮件助手/摘要工作出现语法和局部导入失败；最新收尾验证已确认这些阻塞解除，当前无需浏览器收尾 blocker。
- 2026-09-02 助手服务已用 `--restart --no-refresh --no-open` 安全重启并加载最新系统状态面板与重启修复；重启过程未读取邮箱或打开浏览器。
- 工作区存在已忽略的运行时文件/目录（例如 `screen.png`、`__pycache__/`），未清理。

## 6. 重要技术决策

- 公共接口冻结：服务器名 `windows-gui`、导出 `mcp`、当前 36 个工具不得增删改名，不得修改签名或返回形状，除非用户明确批准；原有 28 个工具保持兼容。
- 邮箱权限矩阵：`bachelor_mail`/`master_mail` = READ+DRAFT+SEND；`qq_mail` = READ+DRAFT（永不 SEND）；每次 SEND 必须先建草稿并获显式确认。
- 身份绑定优先显式 `--profile-directory` 启动的运行时 HWND；进程重启后按 Edge PID 命令行找精确 Profile，必要时允许配置的 Profile 标题后缀回退；永不从页面 UIA 文本推断 Profile。
- IMAP 只读约束：SSL、EXAMINE、UID、BODY.PEEK；禁止 STORE/MOVE/COPY/EXPUNGE。凭据只存 Windows Credential Manager `AI-Work/windows-gui/mailboxes`；本科 IMAP 用户名读 `AI_WORK_BACHELOR_IMAP_USERNAME`。
- CDP 仅限显式 opt-in + 回环地址；浏览器 DOM 提取只返回发件人/主题/时间/哈希引用，不查询正文、不点击、不改已读状态。
- 零行不等于零邮件：无可信列表容器报 `MAIL_LIST_NOT_FOUND`，有行但解析失败报 `MAIL_ITEMS_NOT_PARSED`，至少解析一行且无今日邮件才报 `EMPTY_TODAY`。
- 摘要/助手读取正文仅用 IMAP `BODY.PEEK` 或 Graph 只读，不改变已读状态；AI 中文摘要调用 Zhipu GLM（`glm-4-flash`）。

## 7. 下一步

1. Remote Phase 3B-5 LAN readiness 已完成；下一步仅在用户明确批准后执行真实 private-LAN、防火墙人工配置和跨设备 TLS/pairing smoke。不得自动修改 Firewall、开放公网/云 relay，任何公共 MCP 接口变更仍需单独批准。
2. 邮箱只进入 maintenance mode：遇到 bug、安全问题或真实使用反馈时先做影响评估；保持 QQ 永不发送、两阶段确认、只读约束和身份/域名校验不变。
3. 每次提交前运行规定验证：compileall、完整单元测试、36 工具注册检查、`git diff --check`。
4. 真实桌面验证仅在用户明确授权后运行 `python tests/smoke_test.py`；邮箱只读 smoke 仅在另行授权时使用 `--mailbox-readonly`。
5. 在用户授权真实桌面测试后，验证专用持久 Edge Profile 的启动、手工登录、DOM 检查和登录态下载；真实测试不得提交表单或覆盖文件。

## 8. 最近一次更新

2026-09-04（Europe/Paris）
