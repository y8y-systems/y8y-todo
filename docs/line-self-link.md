# LINE 自助绑定 (V1)

> 目标：**部署管理员只配置一次 LINE Messaging API，之后每位合法的 Todo 用户
> 都能自己在网页里绑定 / 测试 / 解绑 / 更换 LINE 账号**，不需要管理员再去改
> `Y8Y_NOTIFY_LINE_TARGETS`。

面向第一次部署的开发者请先读 [README 的 LINE 自助绑定章节](../README.md#line-自助绑定-v1)，
按其中的顺序操作；本文解释「为什么这样设计」和运维细节。

---

## 1. 三种认证不能混为一谈

| 主体 | 认证方式 | 能做什么 |
| --- | --- | --- |
| **Todo 用户** | 反向代理认证后注入的身份头 / 机器 Token（`serve.py::_current_user`） | 读写自己的任务、通知偏好、**提出**自己的 LINE 绑定/解绑请求 |
| **LINE 官方服务器** | `X-Line-Signature` = base64(HMAC-SHA256(Channel Secret, **原始请求体**)) | 只能投递事件；只能凭「已认证用户创建的一次性绑定码」写入绑定 |
| **通知侧进程** | 独立系统用户 `y8y-notify` + `root:root 600` 的 env 文件 | 持有 Channel Secret / Access Token；**独占**写最终生效的绑定与解绑墓碑 |

Web 进程属于第一类，**永远不持有任何 LINE 长期凭据**。LINE 官方服务器属于第二类，
它**不是** Todo 用户，不能读任务、不能改别人的绑定、不能调用任何 Todo 内部 API。
这两件事由两个不同的进程承担，中间没有 HTTP 互调，只有共享状态目录里的文件。

**文件权限不能绕过第二类认证**：Web 只能往请求队列写「意图」，写不了权威绑定
记录；绑定里的 LINE User ID 只取自**验签后**事件的 `source.userId`。详见 §4。

---

## 2. 进程与数据流

```
 LINE 官方服务器
        │ POST https://todo.example.com/line/webhook
        │ X-Line-Signature: <HMAC over raw body>
        ▼
 [Cloudflare Access]  ← 为 /line/webhook 单独建 Bypass 应用；其它路径仍要认证
        │
 [Caddy / nginx]      ← 精确路径 + POST 才转发；原样透传 body；剥掉不可信头
        │ 127.0.0.1:8087
        ▼
 y8y-todo-line-webhook.service   (User=y8y-notify, 常驻)
   · 验证签名 / 请求体 ≤64 KiB / 事件数 ≤20 / 1:1 聊天 / 事件 ID 去重
   · 兑换一次性绑定码 → 写 line-link/binding-*.json
   · 消费解绑请求     → 删绑定 + 写 unlinked-* 墓碑 + 取消待发测试
   · 消费测试推送队列 → 复用 notifications.LineProvider 发固定文本
        │
        ├─ <notify>/line-link/          2750  ← 只有通知侧可写 (Web: r-x)
        └─ <notify>/line-link-inbox/    2770  ← Web 写请求 / 通知侧消费
        ▼
 y8y-todo.service                (User=y8y, Web)
   · 设置 → 通知 → LINE：生成绑定码 / 查状态 / 提出解绑 / 排测试推送
   · 只返回脱敏后的 LINE ID，不返回任何凭据

 y8y-todo-notify.timer → notify_runner.py (User=y8y-notify, 一次性)
   · 正常提醒/摘要：LineProvider.target_for(user) 解析收件人
```

**为什么需要第三个进程？** LINE 的 Webhook 必须由 LINE 官方服务器主动 POST
进来，所以要有一个常驻监听者；但 Channel Secret 与 Access Token 都不能进 Web
进程（既有安全设计）。让常驻监听者与调度器同属「通知侧」信任域（`y8y-notify`），
是最小改动、且不需要给 Web 任何新权限的方案。

**为什么不把 Webhook 挂在 Web 进程里？** 那样 Web 就必须读 Channel Secret；
一旦 Web 应用出现 RCE，攻击者就能伪造任意 LINE 事件去改所有用户的绑定。

### 取舍说明

* **不引入二维码**：项目是 stdlib-only、无构建步骤、无前端框架。生成二维码需要
  新增依赖或自带一个 QR 编码器，收益却很小 —— 手机浏览器上「打开 LINE」Deep
  Link + 「复制绑定码」已经够用。V1 因此只提供 Deep Link 与复制按钮。
* **不建后台管理系统**：绑定关系就是每个用户一个 JSON 小文件，备份数据目录即可。
* **不做 LINE 侧回复消息**：成功/失败反馈全部在网页设置面板里（轮询），这样
  Webhook 处理路径里没有出网调用，响应更快、更不容易因 LINE 接口抖动而重试。
* **一个 Todo 用户 ↔ 一个 LINE 账号**：V1 的明确口径，冲突一律拒绝而不是静默覆盖。
* **解绑是「立即停发 + 异步落盘」两段式**：Web 只写请求，通知侧下一轮
  （≈0.4 s）写墓碑；但从请求排队的那一刻起，调度器解析收件人就已经
  fail closed，**Webhook 离线也不会恢复旧地址**（§3/§8）。网页轮询
  `unlinking → unlinked`。这样权威绑定记录始终只有一个写者（§4）。

---

## 3. 绑定流程

1. 已认证用户在「设置 → 通知 → LINE」点「绑定 LINE」。
   `POST /todo/api/line-link` 生成 `secrets.token_urlsafe(24)`（192 bit）的
   一次性绑定码，**10 分钟有效**。
2. 服务端只在磁盘上保存 `sha256(绑定码)` 作为文件名索引；原始绑定码只返回给
   浏览器，并存在浏览器的 `sessionStorage`，不落服务端磁盘、不写日志。
   每个用户最多 3 个待用码，全实例最多 200 个，超限返回 429。
3. 用户点「打开 LINE」跳到官方 Bot 的一对一聊天，粘贴发送绑定码（也可加
   `y8y-link:` 前缀）。群聊 / 房间事件一律忽略。
4. LINE 把事件 POST 到 `/line/webhook`。Webhook 服务对**原始字节**验签；无效
   签名直接 401，不解析、不记录正文。
5. 在跨进程 `flock` 内一次性完成：查码 → 校验有效期 → 校验 LINE User ID
   形状（`U` + 32 位小写十六进制）→ 校验该 LINE 账号没绑给别人 → 写绑定 →
   清除该用户的解绑墓碑 → 删码 → 作废该用户其它待用码 → 写结果。
6. 网页每 1.5 秒轮询 `GET /todo/api/line-link`，状态从 `pending` 变 `bound`
   后显示「LINE：已连接」+ 脱敏 ID。

### 解绑流程（P0-1，两段式）

1. `DELETE /todo/api/line-link`（已认证用户）只在 `line-link-inbox/` 写一条
   `unlink-<hash>.json` **请求**，Web 不碰权威目录；返回 `state=unlinking`。
2. **立即生效**：`LineProvider.target_for()` 每轮解析收件人时，第一个检查的
   就是这条有效请求 —— 命中即返回空串，`available_for(user)` 变 False，
   调度器把该用户整体跳过。此时**权威绑定还在、墓碑还没写、旧
   `Y8Y_NOTIFY_LINE_TARGETS` 还配着**，但一条新的 LINE 通知都不会发。
   请求没有 TTL，也不参与 GC 清理：Webhook 离线多久就挡多久。
   测试推送在排队阶段也被拒（409）。
3. **Webhook 恢复**：后台线程在同一把 `flock` 内删 `binding-*.json`、
   写持久化的 `unlinked-<hash>.json` **墓碑**、作废待用码、**取消该用户尚未
   发送的测试通知**（写 `cancelled` 结果）、删请求文件。墓碑保证此后
   **绝不自动恢复向旧地址推送**。网页状态变为 `unlinked`（与「从未绑定」
   的 `unbound` 明确区分）。
4. 用户重新绑定成功时墓碑被清除，通知恢复。

> 「已提出解绑」「明确解绑」「从未绑定」是三种状态：前两种都停发，只有
> 第三种能享受旧环境变量的升级兼容。请求文件的 `version` / `user` /
> `requested_at` 与**文件名哈希**都校验；文件名与内容不一致的伪造请求
> 会被通知侧丢弃，不能借它解绑别的用户。

同一个 `webhookEventId` 只处理一次；LINE 因超时重发时返回 `duplicate` 但不重复
绑定。已消费的码第二次出现返回 `invalid_code`。同一个 LINE 账号 10 分钟内
连续 8 次无效码后暂时不再处理它的消息（内存计数，重启清零 —— 绑定码有
192 bit 熵，暴力猜测本就不可行，这只是纵深防御）。

---

## 4. 数据文件与权限（P0-2 核心）

全部在 `<data>/notify/`（数据目录里，**不在源码静态目录**）。修复前这两类文件
混在一个 `2770` 共享目录里，Web 因为拥有**目录写权限**，可以直接删掉、替换或
凭空新建 `binding-*.json` —— 等于绕过 LINE 验签伪造收件人。现在拆成两层：

| 路径 | 权限 | 属主 | Web (`y8y`) | 通知侧 (`y8y-notify`) |
| --- | --- | --- | --- | --- |
| `<notify>/` | **3770** (setgid + **sticky**) | `y8y-notify:y8y-notify` | rwx（只能删自己拥有的条目） | rwx |
| `<notify>/line-link/` | **2750** (setgid) | `y8y-notify:y8y-notify` | **r-x（只读，无写）** | rwx |
| `<notify>/line-link-inbox/` | **2770** (setgid) | `y8y-notify:y8y-notify` | rwx | rwx |

> 创建 / 改名 / 删除一个文件只需要**父目录**的写权限（不需要文件本身可写）。
> 所以 `2750` 的 `line-link/` 从根上挡住了「Web 伪造 / 替换 / 删除绑定」；
> `3770` 的 sticky 位则阻止 Web 把通知侧拥有的 `line-link/` 整个改名后再伪冒
> 一个同名目录。Web 仍能读 `0644` 的绑定文件来显示自己的状态 —— 应用层按
> 用户隔离由 `status(user)` 保证。

⚠️ **sticky 不限制目录所有者**。Linux 的 sticky 只约束「非目录属主」删别人的
目录项；如果 `<notify>/` 本身归 `y8y` 所有，Web 依然可以 `rmdir` / 改名
`line-link/` 再原地伪冒一个 —— 权限位 `3770/2750/2770` 全部合规也挡不住。
CI 的 `tests/linux_notify_acl.sh` 用两个真实系统账号做过 A/B 实测。
因此 `<notify>/` 的**属主必须是 `y8y-notify`**，这是硬前置条件，不是优化项。

| 文件 | 所在目录 | 写者 | 内容 |
| --- | --- | --- | --- |
| `binding-<sha256(user)>.json` | `line-link/` | **仅通知侧** | 最终生效的 Todo 用户 ↔ LINE User ID |
| `unlinked-<sha256(user)>.json` | `line-link/` | **仅通知侧** | 解绑墓碑（禁止回退旧地址），持久保存 |
| `result-<sha256>.json` | `line-link/` | 通知侧 | 绑定结果（固定错误码，不含 LINE ID） |
| `event-<sha256>.json` | `line-link/` | 通知侧 | 已处理事件 ID（24 小时后 GC） |
| `capabilities.json` | `line-link/` | 通知侧 | 在线标记，每 60 秒重写；>5 分钟没更新视为离线 |
| `code-<sha256>.json` | `line-link-inbox/` | Web | 待用绑定码（用户 id、过期时间、`replace`） |
| `unlink-<sha256(user)>.json` | `line-link-inbox/` | Web | 解绑**请求**（不是权威记录） |
| `test-<id>.req.json` / `.claim.json` / `.res.json` | `line-link-inbox/` | Web / 通知侧 | 测试推送请求 / 已认领 / 结果 |
| `test-rate.json` | `line-link-inbox/` | Web | 每用户测试推送频率 |
| `lock` | `line-link-inbox/` | 两者 | 空的 `flock` 协调文件（**不是**权限边界） |

通知侧启动时会执行 `verify_trusted_layout()` **fail closed** 自检，逐项检查
三级目录：**`<notify>/` 归通知侧账号所有**、sticky 位、other 无访问权、共享组
可穿越、属主可写；`line-link/` 与 `line-link-inbox/` 归通知侧账号所有、不是
符号链接、group/other 不可写（inbox 则需共享组可写）。任一不满足就拒绝启动
并打印脱敏原因，避免运维漏配或目录被抢先占用。

### Operator 一次性建立目录 + 前置检查

```bash
sudo install -d -o y8y-notify -g y8y-notify -m 3770 /var/lib/y8y-todo/notify
sudo chmod 3770 /var/lib/y8y-todo/notify            # 已有目录: 补 sticky 位
# 已有部署务必确认属主 —— chmod 不会改属主:
sudo chown y8y-notify:y8y-notify /var/lib/y8y-todo/notify
sudo install -d -o y8y-notify -g y8y-notify -m 2750 \
        /var/lib/y8y-todo/notify/line-link
sudo install -d -o y8y-notify -g y8y-notify -m 2770 \
        /var/lib/y8y-todo/notify/line-link-inbox
sudo usermod --append --groups y8y-notify y8y
# 改组后必须按 docs/deployment.md §5.2.6 在维护窗口重启 Web 服务

# ⚠️ 装好 Webhook 单元后、启动服务之前, 以 root 跑一次前置审计 (C8):
sudo python3 /opt/y8y-todo/scripts/check-notify-isolation.py \
        --unit-dir /etc/systemd/system \
        --env-file /etc/y8y-todo/y8y-todo-notify.env \
        --data-dir /var/lib/y8y-todo --line-link-layout
# 退出码 0 才算合格; 1 = 有检查项失败, 2 = 无法验证 (都不等于通过)
```

C8 会独立复核三级目录的**属主 / 权限位 / 符号链接**，特别会拒绝
「`<notify>/` 归 Web 用户所有」的部署 —— 那种情况下 sticky 无效。
不装 Webhook 单元时该检查自动跳过，也可用 `--line-link-layout` 强制启用
（例如升级前的预检）。CI 在 Ubuntu 22.04 上用 root + 两个真实系统账号
复现了「父目录归 Web → `rmdir` 成功 → 可伪造绑定文件」的完整攻击链，
并验证启动自检与 C8 都能拦住它。

两个单元的 `UMask=0022` 让新文件是 `0644`（组可读）。⚠️ 给 `notify/` 加 sticky
后，Web 只能替换**自己拥有**的文件；`notification_settings.json` 由 Web 创建，
其余三个通知 JSON 由通知侧创建，互不冲突。若运维曾手工以 root 建过其中某个
文件，请先 `chown` 给对应服务账号。

**备份 / 恢复**：绑定与墓碑跟着 `<data_dir>` 一起备份即可（见 README
「Backups, restore, upgrades」）。恢复时先停 Web 与 Webhook 服务、恢复目录、
再按上面的权限重建目录，最后启动；不需要迁移脚本，也不碰任务文件。

---

## 5. 凭据隔离（核心不变量）

| 凭据 | 存放位置 | 谁能读 |
| --- | --- | --- |
| Channel Access Token (`Y8Y_NOTIFY_LINE_TOKEN`) | `/etc/y8y-todo/y8y-todo-notify.env` (`root:root 600`) | 调度器 + Webhook 服务 |
| Channel Secret (`Y8Y_NOTIFY_LINE_CHANNEL_SECRET`) | 同上 | 调度器 + Webhook 服务 |
| Bot Basic ID (`Y8Y_LINE_BOT_ID`) | `/etc/y8y-todo/y8y-todo.env` | Web 服务（**公开信息，不是凭据**） |

* `y8y-todo.service` 的 `EnvironmentFile` 不引用 `y8y-todo-notify.env`。
* `serve.py` 与 `line_link.py` 里**不出现**任何 `Y8Y_NOTIFY_*` 变量名
  （`tests/test_line_link.py` 静态断言钉死）；`serve.py` 也不调用任何权威写入口
  （`tests/test_line_link_acl.py` 静态断言钉死）。
* Webhook 服务与调度器用 `y8y-notify`，与 Web 的 `y8y` 不同 UID —— 内核
  `ptrace_may_access()` 会拒绝跨 UID 读 `/proc/<pid>/environ`；进程启动还会
  `prctl(PR_SET_DUMPABLE, 0)` 做纵深防御（见 `src/procguard.py`）。
* 新单元的 `ReadWritePaths` 只有 `/var/lib/y8y-todo/notify`，`NoNewPrivileges`、
  `ProtectSystem=strict`、`ProtectHome`、`PrivateTmp` 全部沿用既有模板。
* **没有把 Hermes / Developer / Web 加进 root 组**，也没有新增任何 root 进程。
* Webhook 服务不调用 Todo 内部 API；`line_webhook.py` 里没有 `/todo/api/` 也没有
  `X-Internal-Token`。
* 跨 UID 的真实演练在 CI 的 `sudo bash tests/linux_notify_acl.sh`（Ubuntu 22.04,
  root + 两个真实系统账号）：Web 能读绑定/墓碑，但**伪造、替换、删除、目录改名
  四种手法全部被拒绝**；Web 仍能在 inbox 创建请求。

---

## 6. Cloudflare Access 与反向代理

**不要为了 Webhook 关闭整个应用的身份认证。** 只给下面这一个精确路径开一个
独立且严格受限的入口。

### Cloudflare Access 的实际匹配行为（重要）

Cloudflare 官方《Application paths》说明：

* 「When multiple rules are set for a common root path, **the more specific rule
  takes precedence**... and no rule is inherited from the broader one.」→ 为主域
  建 `Allow` 应用，再为 `/line/webhook` 建一个 `Bypass` 应用，特定路径的
  Bypass 优先生效，其余路径仍走主域认证。
* 路径应用是**路径根**，不是单个 URL：`dashboard.com/eng` 的策略会被
  `dashboard.com/eng/exec` 继承。因此 `/line/webhook` 的 Bypass 会连带覆盖
  `/line/webhook/...` 子路径 —— 这些请求虽然不经过 Access 认证，但**必须**在
  Caddy 层直接 404（见下），绝不能落进 Todo 应用的 handle。
* `Bypass` 官方示例就是 `Action=Bypass / Include=Everyone / Value=Everyone`。
  但官方同时警告：**Bypass 不施加任何 Access 安全控制、请求也不记入 Access
  日志**；所以安全性完全依赖应用层的 HMAC 验签。请务必配 WAF Rate Limiting
  限制该路径 POST 频率，并保持 `Always use HTTPS` 开启。
* Access 应用路径**不支持**查询串与 `#` 锚点；端口会被剥离重定向。本功能的
  Webhook 不使用查询串，把 URL 填成 `https://todo.example.com/line/webhook` 即可。

配置顺序：

1. 主应用 `todo.example.com`（Path 留空）保留原来的 `Allow` 策略，**一个字不改**。
2. 新建 Application：Domain = `todo.example.com`，**Path = `/line/webhook`**，
   Policy = `Bypass / Include Everyone`。不要在它下面再放任何业务路由。
3. 加一条 WAF Rate Limiting（该路径的 POST 频率上限）。
4. 反向代理把**精确路径 + POST** 转发到 `127.0.0.1:8087`，**原样透传 body**：
   签名是对原始字节算的，任何重新序列化、压缩、改写都会让验签失败。
5. ⚠️ **不要把认证只交给 Cloudflare**：Bypass 前缀下的请求仍会到达反向代理，
   而 Access 此时不再注入身份头。反向代理必须自己再做 `forward_auth`（或等价
   认证），并在公开入口剥掉客户端自带的 `Cf-Access-Authenticated-User-Email`
   与 `X-Internal-Token`，否则绕过前缀的请求可能带着伪造身份头抵达应用。
   Caddy 示例的「前缀一律 404 + 兜底 handle 保留 forward_auth」就是为此。
6. 验证其余路径仍被 Access 拦截：`curl -i https://todo.example.com/todo/api/tasks`。

### Caddy

`header_up` 是 `reverse_proxy` 的**子指令**，必须写在 `reverse_proxy { ... }`
块内；写在 `handle` 里 Caddy 直接报 `unrecognized directive: header_up`。
[`packaging/Caddyfile.line-webhook.example`](../packaging/Caddyfile.line-webhook.example)
是**参考片段**（占位域名 + 单个站点块），已通过 `caddy validate`：

⚠️ **不要整文件覆盖 `/etc/caddy/Caddyfile`**：先备份，再把下面 3 个
matcher / handle 合并进你**现有的**站点块，保留原有全局选项、TLS/证书配置、
其它站点与其它路由。示例里**故意不含任何全局选项**，尤其没有
`auto_https off` —— 该选项只用于离线语法测试，若被复制到生产会关掉
Caddy 的自动 HTTPS 与 HTTP→HTTPS 重定向，站点可能降级成明文 HTTP。
`tests/test_caddy_webhook_config.py` 会钉死这一点（含 `caddy adapt` 实证）。

```caddyfile
todo.example.com {
	@line_webhook_post {
		path /line/webhook
		method POST
	}
	handle @line_webhook_post {
		reverse_proxy 127.0.0.1:8087 {
			header_up -X-Internal-Token
			header_up -Cf-Access-Authenticated-User-Email
		}
	}

	# Bypass 前缀下的其它请求直接拒绝, 不给 Todo API 任何机会
	@line_webhook_prefix path /line/webhook /line/webhook/*
	handle @line_webhook_prefix {
		respond "Not Found" 404
	}

	handle {
		forward_auth 127.0.0.1:4180 {
			uri /oauth2/auth
			copy_headers Cf-Access-Authenticated-User-Email {http.reverse_proxy.X-Auth-Request-Email}
		}
		reverse_proxy 127.0.0.1:8086 {
			header_up -X-Internal-Token
		}
	}
}
```

要点：

* `path /line/webhook` 是**精确匹配**（Caddy 的 `path` 加尾部 `*` 才是前缀匹配），
  再叠加 `method POST`；其它方法走下面的认证 handle 或直接 404。
* 两条 `header_up -...` 剥掉客户端伪造的机器 Token 与身份头；
  `X-Line-Signature` 默认原样透传，body 不做任何改写。
* 兜底 `handle` 里的 `forward_auth` 与 `header_up -X-Internal-Token` 与
  [deployment.md §3 Option B](deployment.md#option-b--caddy--an-authenticating-proxy-sso)
  完全一致 —— **普通 Todo API 的认证链没有被放宽**。
* 合并后先 `caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile`，
  再 `systemctl reload caddy`；确认 `curl -I http://todo.example.com/` 仍会
  301/308 跳到 HTTPS（证明 `auto_https` 没被误关）。
* 仓库里的 `tests/test_caddy_webhook_config.py` 会做结构与（有 caddy 时）语法、
  适配 JSON 的回归，含「示例不得产生 `automatic_https.disable=true`」的负对照。

### nginx

（注意 `proxy_set_header` 的继承陷阱：一个 `location` 里只要写了任意一条
自己的 `proxy_set_header`，server 级的所有条目都不会被继承 —— 必须写在**同一块**；
同时把 Bypass 前缀下的子路径一并拒绝。）

```nginx
location = /line/webhook {
    client_max_body_size 64k;
    limit_except POST { deny all; }
    proxy_pass http://127.0.0.1:8087;          # 不要改写 body
    proxy_set_header Host $host;
    proxy_set_header X-Line-Signature $http_x_line_signature;
    proxy_set_header X-Internal-Token "";
    proxy_set_header Cf-Access-Authenticated-User-Email "";
}
location ^~ /line/webhook/ {
    deny all;                                   # Bypass 前缀下的其它路径
}
```

Webhook 服务本身只监听回环，只接受 `POST /line/webhook`，其它路径/方法一律
404；请求体 >64 KiB → 413，缺签名/签名不符 → 401。

### 验证 Webhook

```bash
# 1) 服务在跑且能力表新鲜（无凭据内容）
sudo systemctl status y8y-todo-line-webhook.service
python3 -m json.tool /var/lib/y8y-todo/notify/line-link/capabilities.json

# 2) 未带签名必须 401（从公网域名测，确认 Access 只放行了这一条路径）
curl -i -X POST https://todo.example.com/line/webhook -d '{}'     # 401
#    Bypass 前缀下的其它路径必须 404, 而不是回到 Todo 应用
curl -i https://todo.example.com/line/webhook/anything             # 404

# 3) LINE Developers 控制台 → Messaging API → Webhook → 「Verify」应为 Success
# 4) 网页里绑定一次，发送测试通知，LINE 收到固定文本
# 5) 其余路径仍要求认证
curl -i https://todo.example.com/todo/api/tasks                   # Access 拦截 / 401
```

---

## 7. systemd 安装

只需要**新增一个**常驻单元；已有单元不用改：

```bash
# 0. 先按 §4 建立三级目录 (3770 / 2750 / 2770) 并让 y8y 加入 y8y-notify 组
sudo cp /opt/y8y-todo/packaging/y8y-todo-line-webhook.service \
        /etc/systemd/system/y8y-todo-line-webhook.service
sudo -e /etc/y8y-todo/y8y-todo-notify.env   # 填 Channel Secret + Token
sudo -e /etc/y8y-todo/y8y-todo.env         # 填 Y8Y_LINE_BOT_ID（可选）
sudo systemctl daemon-reload
sudo systemctl enable --now y8y-todo-line-webhook.service
sudo journalctl -u y8y-todo-line-webhook.service -n 30 --no-pager
```

若看到「绑定状态目录权限自检失败，拒绝启动」，说明目录属主/权限不对 —— 按
§4 重建后再启动；服务**不会**降级到不安全布局。`Y8Y_NOTIFY_ENABLED` 与
`y8y-todo-notify.timer` 保持不变 —— 正常提醒与摘要仍由原来的调度器负责，
自助绑定只是替它解析收件人。

---

## 8. 旧 `Y8Y_NOTIFY_LINE_TARGETS` 的迁移

**优先级（明确、固定）**：

1. 用户已**提出解绑**（inbox 里有有效 `unlink-*` 请求）→ **立即停发**。
2. 用户**明确解绑过**（有 `unlinked-*` 墓碑）→ **停发**，不回退旧配置。
3. 用户有自助绑定 → 用它。
4. 用户**从未绑定、也从未解绑**、旧环境变量里有 → 用它（升级兼容，不丢通知能力）。
5. 都没有 → 该用户没有 LINE 渠道。

第 1/2 条是 P0-1 修复点。第 1 条尤其关键：Webhook 离线时绑定与旧环境变量
都还在、墓碑还没写，只有把「待处理请求」放进收件人解析的第一优先级，
才能保证解绑后不会重新发往旧地址。墓碑与请求都让
`LineProvider.target_for()` 直接返回空串；旧环境变量条目可以原样留着，
不会被自动恢复；重新绑定成功后墓碑清除，通知恢复。

**冲突**（自助绑定与旧变量同时存在且指向不同 LINE 账号）由
`Y8Y_NOTIFY_LINE_CONFLICT_POLICY` 决定，绝不会静默二选一：

| 值 | 行为 | 适用 |
| --- | --- | --- |
| `binding`（默认） | 以用户自助绑定为准；调度器把冲突用户写进能力表的 `line_conflicts`，设置页显示「旧配置已被忽略，建议管理员移除」 | 推荐。用户自己认证后的选择就是权威收件人 |
| `strict` | 该用户**停发**（fail closed），直到运维移除旧条目 | 高安全环境，要求人工确认每一次收件人变更 |

两种策略下能力表都只写**用户 id**，不写 LINE User ID、不写凭据；解绑墓碑在
两种策略下都生效（不会因策略切换而复活旧地址）。

**建议的迁移顺序**（对每个已有用户，可随时做、无需停机）：

1. 让用户自己在网页里绑定 LINE，并点「发送测试通知」确认收到。
2. 管理员从 `y8y-todo-notify.env` 里删掉该用户的 `user=Uxxxx` 条目，
   `sudo systemctl restart y8y-todo-line-webhook.service`（env 是启动时读取的）。
3. 下一次 timer 运行会重发能力表，设置页的冲突提示自动消失。

在全部用户迁移完之前，旧条目可以原样留着 —— 未绑定的用户仍靠它收通知；
已解绑的用户不受影响。

---

## 9. 测试通知

* 固定文本，`LineProvider` 复用既有 LINE push 客户端；正文**不能**由客户端指定。
* 只能发给**当前用户服务端已绑定的** LINE 账号；客户端传任何 `line_user_id`
  都会被忽略；明确解绑后返回 409。
* 频率限制：同一用户 60 秒最多 1 条、1 小时最多 5 条，超限 429。
* Web 只把请求排进队列，由持有 Token 的 Webhook 服务执行；网页轮询结果，
  失败给出可理解的中文提示（超时 / 未绑定 / 发送失败 / 已取消）。
* **解绑与待发测试的竞态**：「复核绑定 + 认领请求」与解绑的 `apply_unlink`
  共用一把跨进程 `flock`；解绑会把该用户尚未认领/已认领未回写的请求全部标成
  `cancelled`。即使认领后紧接着解绑，`LineProvider.send()` 也会**重新解析**
  收件人 —— 待处理请求或墓碑让它 fail closed，不会发往旧地址。请求文件本身
  也是一道「预写日志」：即使通知侧在删绑定后、写墓碑前崩溃，它仍然挡着旧
  地址。唯一残留窗口是「HTTP 请求已经发出之后才解绑」，那属于同一用户自己
  发起的在途消息，无法撤销。
* **Webhook 离线时**：解绑请求会一直留在 inbox，调度器每轮都停发；网页停在
  `unlinking` 过渡态（不提供重新绑定入口），直到服务恢复写完墓碑才变成
  `unlinked`。这是刻意的 fail closed 行为，不是卡死。
* **不写 `notification_state.json`**，因此完全不影响正常提醒的去重状态。

---

## 10. 常见错误与排查

| 现象 | 原因 / 处理 |
| --- | --- |
| 设置页显示「尚未启用 LINE 自助绑定」 | Webhook 服务没跑，或 `line-link/capabilities.json` 超过 5 分钟没更新。看 `systemctl status` / journal；确认 §4 的三级目录权限 |
| 「绑定 LINE」按钮灰着 | 同上，`available=false` |
| journal 出现「绑定状态目录权限自检失败」 | 三级目录权限不对，或 **`<notify>/` 父目录属主不是 `y8y-notify`**（sticky 挡不住目录所有者）/ 有符号链接。按 §4 重建并跑 `--line-link-layout` 前置审计（服务会 fail closed, 不会带病启动） |
| 发了绑定码但状态一直 `pending` | ① 确认是在**一对一**聊天里发的（群聊忽略）；② Cloudflare Access 的 Bypass 应用是否配在 `/line/webhook`；③ Caddy 的 `header_up` 是否在 `reverse_proxy` 块内（否则配置根本起不来）；④ 代理是否原样透传 body |
| LINE 控制台 Verify 失败 | Channel Secret 填错 / 代理改写了 body / `X-Line-Signature` 被剥离。用 `curl -i` 未带签名应得 401 证明路径通 |
| 提示「绑定码已过期」 | 10 分钟 TTL，重新生成即可 |
| 提示「这个 LINE 账号已经绑定了其他 Todo 用户」 | 一个 LINE 账号只能绑一个 Todo 用户；让对方先解绑 |
| 测试通知超时 / 显示「已取消」 | 超时看 `Y8Y_NOTIFY_LINE_TOKEN` 与出网；「已取消」表示期间解绑，符合预期 |
| 设置页显示「旧配置已被忽略」 | 该用户自助绑定与 `Y8Y_NOTIFY_LINE_TARGETS` 指向不同账号；按 §8 移除旧条目 |
| 解绑后仍收到旧 LINE 的通知 | 本版本不会：**待处理请求 + 墓碑**都会阻止回退（Webhook 离线也生效）。若仍出现，先确认 Webhook/调度器版本已更新，再检查是否有旧版进程在读旧配置 |
| 解绑后停在「已停止 LINE 通知，正在解除绑定…」 | Webhook 服务没在跑或没能写 `line-link/`；通知**已经停发了**，看 journal 与目录属主，恢复服务后会自动补写墓碑 |
| 上线后 `http://` 不再跳转 HTTPS | 检查是否把示例里测试用的 `auto_https off` 或整个文件复制进了生产 Caddyfile。示例**不含**该选项；见 §6 |

日志**不记录**消息正文、LINE User ID、身份头或签名值；排查时看状态码与
固定错误码即可。
