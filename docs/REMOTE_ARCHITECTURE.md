# AI-Work Remote 架构与威胁模型（Phase 3A 设计文档）

状态：设计边界与实现说明。Phase 3B-1（协议与认证原语）、3B-2（loopback
health server）、3B-3（pairing / device / revocation）、3B-4（TaskCenter
staging 与本地确认页）和 3B-4.1（单次 action token 加固）已实现；
3B-5（LAN opt-in）未获批准，不得实现。

## 1. Goals

- 为 AI-Work 定义一个最小、安全、可回滚的 Remote 能力：从另一台设备查看健康状态、
  提交待确认任务、由本机用户确认后执行。
- 复用 Phase 1（TaskCenter）与 Phase 2（浏览器确认/恢复/DNS 防护）的安全机制，
  而不是重建一套确认体系。
- 保持 36 个 MCP 工具公共接口完全不变；Remote 是独立内部服务，不是 MCP 工具。
- 安全默认 fail closed：无法判断时不执行任何副作用。

## 2. Non-goals

- 不实现远程确认（Remote 永远不能确认自己的请求）。
- 不提供任意命令、shell、任意 MCP 调用、任意 selector/JavaScript、任意文件读写。
- 不做 cloud relay、公网暴露、第三方远程服务。
- MVP 不读取邮件元数据/正文，不提供远程 GUI 操作。
- 不改变现有邮件安全边界（QQ 永不发送、两阶段发送、只读约束）。

## 3. Existing reusable components（现有能力审计）

### 3.1 可直接复用

| 组件 | 位置 | 对 Remote 的价值 |
| --- | --- | --- |
| TaskCenter | `windows_gui/task_center.py` | staged/confirmed 生命周期、TTL、容量、单次消费、固定白名单、确定性错误，是 Remote 唯一的副作用入口 |
| 浏览器确认暂存 | `windows_gui/browser_session.py`（Phase 2） | `confirm_click` 暂存 + 本地确认 + 重验证，可直接作为 remote 浏览器请求的执行末端 |
| health_events | `windows_gui/health_events.py` | 固定 allowlist JSONL 审计日志、轮转、跨进程 mutex、fail-safe 写入，Remote 审计沿用该模式 |
| system_health | `windows_gui/system_health.py` | 只读健康模型（PASS/WARN/FAIL/UNKNOWN），`health.read` 命令直接消费 |
| Credential Manager 封装 | `windows_gui/mail_backends.py`（`WindowsCredentialManagerSecretStore` / `WindowsCredentialManagerTokenStore`） | 设备凭据与服务端 secrets 的存储方式 |
| 回环回调校验 | `windows_gui/master_oauth.py` | 精确 path/Host 校验、`secrets.compare_digest`、不记录敏感值的模式，用于 pairing 与确认平面 |
| 本机 HTTP 服务加固 | `scripts/mail_assistant_server.py` | Host/Origin/Content-Length/JSON 校验、通用错误、`nosniff`/`no-referrer`/`SAMEORIGIN`/CSP，Remote transport 照此实现 |
| `_is_loopback_endpoint` | `windows_gui/browser_mail.py` | 回环地址判定 |
| 公共地址校验 | `windows_gui/browser_download.py` | URL/解析地址防护，供浏览器域适配器复用 |
| 跨进程 mutex 模式 | `health_events` / mail refresh lock | Remote 服务与 MCP 进程共享状态时的串行化参考 |

### 3.2 需要泛化

- `health_events`：新增固定组件 `remote` 与固定事件码（见 §15），属小步扩展。
- 本地确认 UX：助手页确认模式推广为“本地确认面板”，服务 remote 暂存任务（见 §12）。
- TaskCenter 暂存上下文：保持服务端私有，新增可选的非敏感来源标记
  （如 device opaque id），供撤销与审计使用；不改变公开视图。

### 3.3 不适合复用

- `master_oauth.py` 的完整 OAuth 流程（仅借鉴其校验模式）。
- Graph/IMAP/CDP 传输实现（域适配器内部细节，Remote 不直接接触）。
- 计划任务安装器、邮件摘要渲染等邮件域功能。

### 3.4 Remote 必须新增

1. transport 服务（默认 loopback HTTP）。
2. pairing / 设备凭据 / 会话管理。
3. HMAC 请求签名 + nonce/时间戳防重放。
4. 固定命令白名单 + 权限级别 + 速率限制。
5. 本地确认面板（与 Remote 请求平面隔离）。
6. 设备注册/撤销（Credential Manager 条目管理）。
7. 幂等去重缓存（重复请求不产生重复副作用）。

## 4. Trust boundaries

```
+--------------------------------------------------------------+
| 本机信任域                                                     |
|                                                              |
|  [Remote client 设备]                                         |
+--------||----------------------------------------------------+
         ||  仅经认证通道（MVP: loopback / opt-in: LAN+TLS+HMAC）
         vv
+--------------------------------------------------------------+
| Remote transport（独立本机服务，默认 127.0.0.1）               |
|   authn / authz / rate limit / audit                         |
+--------||----------------------------------------------------+
         ||  固定命令白名单，仅 stage / status / cancel
         vv
+--------------------------------------------------------------+
| TaskCenter（服务端 staged 生命周期，verified_context 私有）     |
+--------||----------------------------------------------------+
         ||  本地确认平面（仅 loopback，拒绝远程凭据）
         vv
+--------------------------------------------------------------+
| domain adapter（browser / mail / [gui 未来]）                  |
|   最终权限校验：QQ 永不发送等由域自身保证                       |
+--------||----------------------------------------------------+
         vv
  Browser / Mail / GUI 副作用原语
```

信任边界结论：

- 网络侧（含已配对设备）只信任到 transport 的输入验证为止。
- TaskCenter 是唯一副作用入口；Remote 没有 execute 能力。
- 本地确认平面在 loopback 上，与 Remote 认证平面完全隔离。

## 5. Threat model

### 5.1 网络攻击者（同 Wi-Fi 恶意设备）

| 威胁 | 防线 |
| --- | --- |
| ARP/DNS 欺骗、抓包 | MVP 仅 loopback（网络不可达）；LAN opt-in 必须 TLS + HMAC（§13），token 不进 URL |
| token 窃取 | 设备凭据存 Credential Manager；会话 token 仅内存；轮换/撤销（§17） |
| replay | nonce + 时间窗 + HMAC 签名 + 幂等去重（§9） |
| request tampering | HMAC 覆盖 method/path/body/nonce/timestamp，篡改即拒绝 |
| session hijack | 会话 token 短时效 + 绑定设备凭据派生；被窃会话最多存活至 idle/absolute 上限 |
| brute force pairing | pairing code 高熵 + 短 TTL + 每源速率限制 + 失败锁定冷却（§16） |
| rate-limit bypass | per-device 与全局双层限制；无有效凭据按来源 IP 限速 |
| malformed/资源耗尽 | 请求大小上限（沿用 256 KiB 模式）、解析失败即断开、固定错误 |

### 5.2 已配对但不可信设备

| 威胁 | 防线 |
| --- | --- |
| 手机被盗/凭据泄漏 | 单设备撤销；撤销即杀会话并取消其未确认任务（§17） |
| 恶意客户端 | 只能调用白名单命令；只能 stage；不能确认；不能读 verified_context |
| 旧设备未撤销 | 设备列表在本地面板可见，一键撤销全部 |
| 冒充其他设备 | 每设备独立 secret；签名绑定 device id；跨设备签名无效 |
| 已撤销 token 重放 | 撤销即时生效（查注册表 + 会话表），重放返回固定 401 并审计 |

### 5.3 本机攻击面

| 威胁 | 防线 |
| --- | --- |
| 本机其他进程直接调用 Remote endpoint | 仍需 pairing 后凭据；未配对进程无法调用任何命令（全部命令要求认证，见 §7） |
| localhost CSRF / 恶意网页探测 | 校验 Host 为 `127.0.0.1:<port>`、拒绝跨源、`no-referrer`/`SAMEORIGIN`/CSP（沿用助手服务模式）；浏览器无法伪造 HMAC |
| DNS rebinding | Host 校验拒绝非 `127.0.0.1` 主机名；绑定固定回环地址 |
| Host header abuse | 精确匹配 `127.0.0.1:<port>`（`master_oauth.parse_callback` 模式） |

### 5.4 权限升级链分析

审查目标链：`remote read → remote stage → remote confirm → unrestricted local execution`。

该链在设计上被两处硬切断：

1. **Remote 不能 confirm。** 确认平面只存在于 loopback，端点只接受本地会话
   （Host 必须是 `127.0.0.1`，且确认端点不参与 Remote 认证体系——携带 Remote
   凭据的请求在确认端点上没有意义，也不会被接受为确认）。
2. **Remote 没有 execute。** consume/complete 仅由本地域适配器在确认后调用；
   Remote 命令枚举中不存在等价能力（§7）。

因此：**远程配对本身不等价于用户确认。** 配对只能让设备请求“把一个已验证的
意图放入 TaskCenter 暂存”，执行必须由本机用户在本地确认面板完成。剩余的
最坏情形是“已配对设备频繁制造待确认垃圾任务”，由速率限制、容量上限与撤销处理。

### 5.5 敏感信息泄露

Remote 响应永不包含：passwords、cookies、access/refresh token、session URL、
带 query 的完整 URL、Credential Manager 值、邮件正文（MVP 连元数据也不返回，
见 §26-D8）、浏览器输入框内容、DOM secret、Windows 命令行中的敏感值。
健康响应仅含固定四态与计数。任务状态仅含非敏感状态机字段。日志与审计按
固定 allowlist（§15）。现有 36 工具的返回形状不受影响。

## 6. Authorization matrix（权限模型）

| Level | 名称 | 内容 | 授权要求 |
| --- | --- | --- | --- |
| L0 | Health | 服务状态、浏览器 worker 健康、邮件子系统就绪、任务计数（非敏感统计） | 已配对设备即可 |
| L1 | Read-only info | 未来：邮件元数据、页面标题等 | **MVP 不开放**；如开放需单独批准 + 隐私评审 + 更高认证强度 |
| L2 | Stage | 创建待确认 Task（browser 点击/下载请求、mail 草稿请求） | 已配对设备 + HMAC 签名 + 速率限制 |
| L3 | Execution | 一律由本地确认触发，**无远程触发路径** | 本地确认面板（loopback） |

默认模型：**Remote can request; local user confirms.** 远程自确认不纳入任何阶段。

## 7. Command allowlist（命令白名单草案）

最终命名在 3B-1 落定，形态固定为枚举：

| 命令 | 级别 | mutating | TaskCenter | 本地确认 | 速率（/设备） | 审计 | 返回数据分类 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `health.read` | L0 | 否 | 否 | 否 | 30/min | 否 | 聚合健康 |
| `task.status` | L0 | 否 | 否 | 否 | 30/min | 否 | 本设备任务的非敏感状态 |
| `task.cancel` | L0 | 是（删除意图） | 是（cancel） | 否（取消自己的暂存是安全的） | 30/min | 是 | 状态 |
| `browser.request_click` | L2 | 是 | 是（stage） | **是** | 10/h，burst 5 | 是 | 暂存确认 |
| `browser.request_download` | L2 | 是 | 是（stage） | **是** | 5/h | 是 | 暂存确认 |
| `mail.request_draft` | L2 | 是 | 是（stage） | **是** | 5/h | 是 | 暂存确认 |
| `session.revoke_self` | L0 | 是 | 否 | 否（自撤销是安全的） | 5/min | 是 | 状态 |

禁止项（永久）：arbitrary Python/shell/PowerShell、任意 MCP 调用、任意
method/function 分发、任意 selector、任意 JavaScript、任意文件读写、进程启动、
带私网绕过的任意 URL。未知命令一律固定拒绝并审计。

所有命令：需要认证；无匿名路径（pairing 端点除外，见 §8）。

## 8. Pairing / Authentication

候选比较：

| 方案 | 复杂度 | 抗重放 | 轮换/撤销 | 多设备 | 可用性 | 结论 |
| --- | --- | --- | --- | --- | --- | --- |
| A. 长随机 bearer token | 低 | 依赖 TLS | 粗粒度 | 弱（共享 token） | 高 | 不推荐单独使用 |
| B. 一次性 pairing code → 每设备长期凭据 | 中 | 配对层高 | 按设备精确撤销 | 强 | 中（需本机操作一次） | **推荐（骨架）** |
| C. 每设备 secret + HMAC 签名请求 | 中 | 应用层抗重放 | 按设备 | 强 | 高 | **推荐（与 B 组合）** |
| D. 证书/mTLS、Windows Hello | 高 | 高 | 强 | 强 | 低（部署复杂） | 暂不采用 |

**推荐：B + C 混合。**

- 配对：用户在本地确认面板点击“配对新设备”，面板显示 8 位高熵一次性 code
  （`secrets.token_urlsafe` 派生，TTL 5 分钟，单次使用）；设备提交 code 换取
  每设备随机 secret（32 字节）。服务端 secret 存 Credential Manager
  （服务 `AI-Work/windows-gui/remote`，用户名 `device_<opaque_id>`）；
  客户端自行安全存储。
- 认证：`POST /session` 携带 device opaque id、nonce、timestamp 和
  `method\npath\nbody-hash\nnonce\ntimestamp` 的 HMAC-SHA256。
  `POST /command` 额外要求 Bearer session，并且 HMAC 串以
  `device:<opaque_id>` 收尾，显式绑定请求设备：
  `method\npath\nbody-hash\nnonce\ntimestamp\ndevice:<opaque_id>`。
  服务端用该设备 secret 验签，再校验 session 属于同一 device opaque id。
  secret 永不出现在 URL、命令行、日志或明文配置。
- 旋转：本地面板可对单设备重新配对（生成新 secret，旧 secret 立即失效）。

## 9. Replay prevention

- `/command` 每个请求：128-bit 随机 nonce + 客户端 timestamp（±90 秒窗口）
  + 显式绑定 device id 的 HMAC 签名。
- 服务端有界 nonce 缓存（TTL = 时间窗，LRU 上限如 4096）；重复 nonce 固定拒绝。
- 应用层签名在 TLS 之外仍然必需：它保护 LAN HTTP 场景与任何 TLS 终止代理，
  并把重放防护与传输解耦。即使用了 TLS，mutating 命令也保留签名 + nonce；
  命令信封中的客户端 `request_id` 以 `(device, request_id)` 为键进入
  有界幂等缓存。只有成功 mutating 结果会缓存；重复同一
  `(device, request_id)` 返回首次结果，不产生第二次副作用。
  当前唯一的 enabled mutating 命令是 `session.revoke_self`，它撤销当前
  Bearer session，不撤销设备凭据；幂等缓存命中先于已撤销 session 校验，
  因此同一请求的授权重试得到确定性首次结果，而新的 `request_id`
  在旧 session 上固定拒绝。
- confirm/cancel：confirm 只存在于本地平面（无重放面）；`task.cancel` 幂等
  （重复取消返回相同终态）；已消费/过期引用的重复使用返回确定性错误
  （TaskCenter 已保证）。
- 重复请求不得导致重复副作用：由幂等去重 + TaskCenter 单次消费共同保证。

## 10. Session model

- 长期 device credential（Credential Manager）与短期 session token 分离。
- session token 仅存内存：TTL = 15 分钟 idle、12 小时绝对上限；并发每设备
  最多 2 个，超出淘汰最旧。
- **服务重启后所有 session 全部失效**（内存态）；设备凭据保留，设备需重新
  认证（不重新配对）。
- 撤销设备：立即失效其全部 session，并取消其所有未确认 staged task（推荐并
  采纳，见 §17）。
- 丢失设备：本地面板“撤销全部设备”始终离线可用。
- MVP 最小集：上述全部；不做持久化 session、不做推送唤醒。

## 11. Network binding strategy

| 维度 | loopback only（MVP 默认） | private LAN binding（opt-in） |
| --- | --- | --- |
| 可用性 | 本机/隧道可用 | 局域网直连 |
| 防火墙 | 无需改动 | 需用户确认 Windows 防火墙提示（文档指导，不自动改） |
| 攻击面 | 本机进程（仍需认证） | 整个二层网络 |
| IPv4/IPv6 | 仅 `127.0.0.1`（IPv6 `::1` 需显式开关） | 显式指定接口地址，禁止 `0.0.0.0`/`::` |
| 接口变化/VPN/热点 | 不受影响 | VPN/热点可能暴露到不受信网络——文档必须警告 |
| 意外公网暴露 | 不可能 | 依赖配置正确 + TLS + 认证 + 速率限制 |

**MVP 默认：loopback only，fail closed。** LAN 属 3B-5，需要用户明确批准、
TLS（§13）与启动时的显著本地提示。

## 12. Local confirmation UX（确认平面隔离）

- 推荐：由 Remote 服务自身提供一个仅绑定 loopback 的本地确认页（沿用助手服务
  的 Host/Origin/头部模式），列出未确认 remote 任务的非敏感摘要
  （domain、动作、时间、来源设备 opaque id）与批准/取消按钮。
- 备选：扩展 mail assistant 页面（未来合并）。
- Windows Toast 提醒有新待确认任务（复用摘要通知模式）。
- **User-presence 边界评估（3B-4.1 结论）**：loopback 确认平面是本地信任边界，防远程设备与浏览器跨站请求；它不是强用户在场证明。更强的边界需要交互桌面会话内的原生 UI（Win32 对话框/tray/独立确认进程），属 LAN 批准前的后续安全选项。
- **隔离不变量**：确认端点只接受 loopback 本地会话；它不参与 Remote 认证
  体系——Remote 凭据/HMAC 在确认端点上不被识别，携带与否都不构成确认。
  本地页面与 Remote API 共用同一 TCP 端口但路径与认证域不同；如实现中发现
  难以保证，则拆分为两个端口（确认面板独立 loopback 端口）。

## 13. TLS 设计

- **loopback MVP：HTTP 可接受。** 回环流量对网络攻击者不可见；残余风险是
  本机其他进程（已有认证防线）——明确记录。
- **LAN：必须 TLS。** 自签名设备证书在 pairing 时生成并交付客户端 pinning
  （指纹校验，不做主机名校验，规避 IP/主机名不匹配）；私钥存 Credential
  Manager（DPAPI 保护）；证书轮换 = 重新配对；信任引导 = 首次配对在本机
  确认面板完成（不存在引导劫持窗口，因为证书分发与 pairing code 一样
  走本地信任面）。
- 优先选择能防局域网抓包/token 盗取的组合：LAN = TLS + HMAC + 速率限制。

## 14. Protocol（草案）

### 14.1 Transport 比较

| 维度 | HTTPS REST（本设计） | WebSocket | HTTP + 轮询 | Named Pipe | Cloud relay |
| --- | --- | --- | --- | --- | --- |
| 复杂度 | 低（复用助手服务模式） | 中（长连接状态机、心跳） | 低 | 中（Windows 专属 API） | 高（外部服务、账户体系） |
| 认证 | 每请求 HMAC + session，天然请求边界 | 连接级认证，中间指令需另防重放 | 同 REST | 管道 ACL，模型不同 | 依赖云身份 |
| TLS | loopback 可 HTTP；LAN 用 TLS | 同左 | 同左 | 不适用（本机） | 强制但由第三方终结 |
| 推送需求 | MVP 无需推送，`task.status` 轮询足够 | 天然推送 | 轮询即本质 | 本机通知即可 | 可推送 |
| NAT 穿透 | 不涉及（LAN/loopback） | 不涉及 | 不涉及 | 不涉及 | 天然可穿 NAT（未来方案） |
| LAN MVP 契合 | 高 | 中（对 MVP 是过度设计） | 高（与 REST 合并使用） | 低（无法服务 LAN） | 低（MVP 排除） |
| 重放防护 | 应用层签名 + nonce（§9） | 需在消息层重建同等机制 | 同 REST | 需自定义 | 由云方案定义 |
| Python 生态 | stdlib `http.server` + 现有模式 | 需第三方库（如 websockets） | 同 REST | `pywin32` | SDK 依赖 |
| Windows 兼容 | 已验证（助手服务） | 一般 | 已验证 | 良好但冷门 | 取决于厂商 |

**结论：Phase 3B MVP 选择 HTTP REST + 轮询**（`task.status` 轮询覆盖无推送需求），
与现有 mail assistant 服务器同构、认证面最清晰、零新依赖。WebSocket 的推送
能力对 MVP 无价值；Named Pipe 无法支撑未来 LAN；cloud relay 仅作为未来需要
公网访问时的独立评审方案（不在本架构范围内）。

### 14.2 REST 协议草案

REST over HTTP(S)，JSON：

- `POST /pairing/claim`（无认证；body: pairing code + 设备名 + 客户端证书指纹
  [仅 LAN/TLS]）→ 设备凭据（仅此一次返回）。
- `POST /session`（HMAC）→ session token。
- `POST /command`（HMAC 绑定 device id + Bearer session；body: 命令枚举 +
  参数 + request_id）→ 统一响应 `{status, task_id?|data?}`；错误为固定
  `{error: <fixed_code>}`。
- `GET /health`、`GET /tasks`（HMAC + session）。
- 确认平面（仅 loopback）：`GET /local/confirmations`、
  `POST /local/confirmations/<task_id>/approve|cancel`。

所有响应：固定错误码、无调用方细节、无敏感字段；头部沿用助手服务安全头。

## 15. Audit logging

- 沿用 `health_events` 模式：组件 `remote`（新增固定白名单），固定事件码：
  `pairing_started/pairing_completed/pairing_failed`、`auth_failed`、
  `rate_limited`、`command_denied`、`task_staged`、`task_cancelled`、
  `task_confirmed_local`、`task_expired`、`device_revoked`。
- 允许字段：时间、设备 opaque id（对 device id 做 HMAC 哈希后记录）、固定码、
  成功/失败、任务 opaque id、固定 reason code。
- 禁止字段：正文/主题/收件人/密码/Cookie/token/完整 URL/原始请求体/异常原文/DOM 内容。
- 保留策略：沿用 512 KiB × 3 轮转；预期为“防抵赖的尽力而为日志”，不承诺
  防篡改（同机攻击者可改文件），这一边界写入文档。
- 日志写失败不阻断主流程（现有 fail-safe 语义）。

## 16. Rate limits

| 操作 | per device/source | global | burst/锁定 |
| --- | --- | --- | --- |
| pairing claim | 5 次/10 分钟（按源 IP） | 20/小时 | 失败 5 次冷却 15 分钟（源级） |
| auth 失败 | 10 次/5 分钟 | 100/小时 | 超限临时冷却，不永久锁定 |
| health.read | 30/分钟 | 300/分钟 | 无 |
| task.status | 30/分钟 | 300/分钟 | 无 |
| mutating stage | 10/小时（burst 5） | 50/小时 | 无 |
| 确认尝试 | 本地面板不限制 | 无 | 无 |

设计原则：可冷却、可恢复，避免攻击者用锁定把合法用户永久挡在门外；
限制器为进程内有界结构，重启即清零（安全方向：重启后限制重新累计）。

## 17. Revocation

- 单设备撤销：删 Credential Manager 条目 + 失效 session + 取消该设备全部
  未确认 staged task（推荐并采纳：撤销设备的待确认请求一律作废）。
- 撤销全部设备 / 紧急丢失手机：本地面板一键操作，离线可用。
- 轮换 = 撤销 + 重新配对。
- 重启效应：session 全失效；设备凭据与撤销状态（Credential Manager/注册表）
  持久。

## 18. Failure modes（默认：无法判断 → 不执行）

| 场景 | 行为 |
| --- | --- |
| 网络断开/客户端重试 | request_id 幂等去重，重试返回首次结果 |
| 服务重启 | session 全失效；staged 任务随进程消失；客户端重新认证 |
| Credential Manager 不可用 | 认证失败关闭（fixed error），健康面板 WARN |
| 恶意/畸形凭据 | 固定 401 + `auth_failed` 审计 |
| TaskCenter 满 | 固定拒绝，无部分副作用 |
| 任务过期 | 确定性过期错误；确认面板消失 |
| 本地确认超时 | TTL 到期任务 EXPIRED，永不执行 |
| 浏览器 worker 不可用 | stage 前置检查 not-ready 即拒绝，避免堆积 |
| 已撤销设备 | 固定 401 + 审计 |
| 时钟偏差 | 超出 ±90 秒固定拒绝（响应携带服务器时间供客户端校正） |
| 重复请求 | 幂等去重 |
| 审计写失败 | 主流程继续（现有 fail-safe） |

## 19. TaskCenter integration

| 问题 | 决策 |
| --- | --- |
| Remote 能否直接 `consume` | **否** |
| Remote 能否直接 `complete` | **否** |
| 谁负责 execute | 本地域适配器（本地确认之后） |
| Remote 能否读 verified_context | **永远不能**（服务端私有，不序列化） |
| restart 后 staged task | 随进程消失（与现有 TaskCenter 进程内语义一致），文档明示 |
| device id 记录 | 以 opaque 哈希写入暂存上下文（服务端私有），用于撤销与审计 |

Remote 不获得通用 TaskCenter execution API；命令枚举中没有等价能力。

## 20. Domain adapters

每个域保持同一条链：request → validation → stage → local confirm →
revalidate → execute。**域适配器保有最终权限否决权**，Remote policy 只是前置过滤：

- browser：复用 Phase 2 `confirm_click`/下载暂存；`browser.request_download`
  额外限制目标目录白名单（3B 细化）。
- mail：复用 mail TaskCenter 暂存与两阶段发送；**QQ 永不发送由 mail 域自身
  保证**，即使 Remote policy 出错也不可能绕过。
- GUI：MVP 不提供远程 GUI 操作（高风险；如未来开放需单独批准）。

## 21. Public API impact

**Phase 3B 不需要新增任何 MCP tool。** Remote 是独立内部服务（独立进程 +
loopback 端口），与 MCP 服务器平行。36 个工具、签名、返回形状、服务器名
`windows-gui` 与入口 `windows_gui_mcp.py` 全部不变，并继续由现有单测守卫。
任何未来把 Remote 暴露为 MCP 工具的想法都需要用户单独批准。

## 22. 建议模块边界

```
windows_gui/remote/
  __init__.py
  auth.py        # pairing、设备凭据（Credential Manager）、session、HMAC、nonce 缓存
  protocol.py    # 请求解析/验证、命令枚举、幂等 request_id
  policy.py      # 白名单、权限级别、速率限制
  audit.py       # health_events 的 remote 组件薄封装
  adapters.py    # browser/mail 暂存桥接（薄层，最终权限在域内）
scripts/remote_server.py   # loopback HTTP transport + 本地确认页（mail_assistant_server 模式）
```

MVP 刻意保持模块少；不为未来功能预留空壳。

## 23. Phase 3B implementation plan

### 3B-1 协议与认证原语（无网络）

- 范围：`auth.py` + `protocol.py` + 单测（凭据生成/存储抽象、HMAC 规范化签名、
  nonce 缓存、命令枚举、幂等缓存、速率限制器）。
- 风险：低（纯内部库）。测试：签名篡改、重放、时钟偏差、限速、枚举外命令。
- DoD：全部单测通过；36 工具不变；无任何监听。
- 回滚：删除新模块即可，无存量依赖。

### 3B-2 loopback 服务 + health.read + task.status

- 范围：`scripts/remote_server.py`（Host/Origin/头部/大小校验）、session、
  审计、L0 命令。依赖 3B-1。
- 风险：本机端口占用（中低）。
- 测试：认证矩阵、限速、畸形请求、重启失效、Host 伪造拒绝。
- DoD：loopback 手动验收脚本（本机 curl）+ 全部单测；36 工具不变。
- 回滚：不启动服务即完全失效；删除脚本与模块。

### 3B-3 pairing / 撤销

- 范围：pairing code 生命周期、设备注册表、本地面板设备管理、撤销联动。
- 风险：中（凭据管理）。测试：配对成功率/暴力限制、撤销即时性、重启持久性。
- DoD：配对/撤销全流程单测 + 本机验收；撤销后重放固定 401。
- 回滚：未配对状态即无攻击面；删除模块。

### 3B-4 TaskCenter remote staging + 本地确认

- 范围：`adapters.py`（browser.request_click/request_download、
  mail.request_draft）+ 本地确认页 + Toast 提醒。依赖 3B-1~3。
- 风险：中高（触碰副作用路径）。测试：§24 全矩阵 + 既有两阶段/确认回归。
- DoD：远程 stage → 本地确认 → 既有执行链路端到端（mock + 本机验收）；
  QQ 发送尝试在域层被拒；36 工具不变。
- 回滚：撤销 staging 接入（域适配器开关），恢复纯本地流程。

### 3B-5 LAN opt-in（需用户批准后才能开工）

- 范围：TLS + 证书 pinning + 接口绑定参数 + 文档化防火墙指导。
- 风险：高（网络暴露）。测试：抓包防护、pinning 失败拒绝、跨网段拒绝。
- DoD：默认仍 loopback；LAN 仅显式参数启用且启动横幅提示。
- 回滚：默认参数即回到 loopback。

## 24. Test plan（Phase 3B 测试矩阵）

| 类别 | 用例 |
| --- | --- |
| 认证 | wrong token / expired session / revoked device / 伪造 Host / 非 loopback 来源（LAN 模式） |
| 重放 | 同 nonce 重放、同 request_id 重复 stage、跨设备签名、过期时间戳、时钟偏差边界 |
| 配对 | 错误 code、过期 code、重复 claim、暴力限制、配对后凭据格式 |
| 授权 | 未列命令、L1 越权、remote 调确认端点无效、remote 直接 consume 不可达 |
| 任务 | task id 猜测（随机性）、过期、取消、重复取消、TaskCenter 满容量 |
| 泄漏 | 响应/审计/日志不含 token、URL query、正文、凭据字段（repr 断言） |
| 并发 | 并发重复请求只产生一个任务/一次副作用 |
| 生命周期 | 服务重启、撤销后 outstanding task 全取消、确认超时 |
| 域策略 | browser worker 不可用、mail policy 拒绝、QQ send 尝试被拒 |
| 审计 | 事件码白名单、内容安全、写失败不影响主流程 |

## 25. Security invariants（安全不变量）

1. 远程配对不等于用户确认；确认只存在于 loopback 本地平面。
2. Remote 无 execute/consume/complete 能力；副作用入口只有 TaskCenter stage。
3. verified_context 永不离开服务端进程，永不出现在任何响应/日志。
4. 域适配器最终权限：QQ 永不发送等规则不依赖 Remote policy。
5. secret 只存 Credential Manager；URL/命令行/日志/明文配置零秘密。
6. 命令固定枚举，未知即拒绝；每命令定义认证/级别/速率/审计。
7. 默认绑定 loopback；LAN 仅 opt-in + TLS + 显式批准。
8. mutating 远程请求必须使用显式绑定 device id 的 HMAC 签名 + nonce +
   幂等 request_id；当前 `session.revoke_self` 只撤销当前 session。
9. 审计与健康事件使用固定 allowlist，不记录调用方提供的细节。
10. 36 个 MCP 工具与现有确认语义不变；Remote 不新增 MCP 表面。
11. 本地确认 mail draft 后，Remote 响应只返回 staged 状态、邮箱标识和安全
    详情，永不返回本地两阶段发送引用（如 `pending_id`）。

## 26. Open decisions requiring user approval before Phase 3B

| 编号 | 决策 | 推荐默认 |
| --- | --- | --- |
| D1 | MVP 绑定策略 | 仅 loopback（127.0.0.1） |
| D2 | 是否提供 LAN opt-in（3B-5） | 保留为远期可选项，默认关闭 |
| D3 | Transport | HTTP REST + 轮询（loopback） |
| D4 | LAN 必须 TLS（自签 + pinning） | 是（若 D2 批准） |
| D5 | Pairing 模型 | 一次性 code → 每设备凭据 + HMAC（B+C） |
| D6 | 本地确认面板宿主 | Remote 服务自带 loopback 确认页（后续可并入助手页） |
| D7 | 是否新增 MCP 工具 | 否（永久倾向） |
| D8 | 远程读取邮件元数据（L1） | MVP 不开放；如开放需单独批准 + 隐私评审 |
| D9 | 远程请求 GUI 操作 | MVP 不开放 |
| D10 | 远程自确认 | 设计上永久禁止，无批准路径 |

## 27. Implementation status

- 3B-1 到 3B-4.1 的当前实现覆盖协议原语、loopback-only transport、pairing、
  设备注册/撤销、`health.read`、`task.status`、`session.revoke_self`、
  TaskCenter staging、本地确认页和 task/action 绑定的单次 action token。
- Remote mail draft 确认结果只暴露 staged 状态、邮箱标识和安全详情，不暴露
  本地两阶段发送引用。
- Remote 不新增 MCP 表面；现有 MCP 公共接口保持不变。
