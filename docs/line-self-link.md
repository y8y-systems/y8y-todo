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
| **Todo 用户** | 反向代理认证后注入的身份头 / 机器 Token（`serve.py::_current_user`） | 读写自己的任务、通知偏好、自己的 LINE 绑定 |
| **LINE 官方服务器** | `X-Line-Signature` = base64(HMAC-SHA256(Channel Secret, **原始请求体**)) | 只能投递事件；只能凭「已认证用户创建的一次性绑定码」写入绑定 |
| **通知侧进程** | 独立系统用户 `y8y-notify` + `root:root 600` 的 env 文件 | 持有 Channel Secret / Access Token；读写 `<data>/notify/` |

Web 进程属于第一类，**永远不持有任何 LINE 长期凭据**。LINE 官方服务器属于第二类，
它**不是** Todo 用户，不能读任务、不能改别人的绑定、不能调用任何 Todo 内部 API。
这两件事由两个不同的进程承担，中间没有 HTTP 互调，只有共享状态目录里的文件。

---

## 2. 进程与数据流

```
 LINE 官方服务器
        │ POST https://todo.example.com/line/webhook
        │ X-Line-Signature: <HMAC over raw body>
        ▼
 [Cloudflare Access]  ← 只对这个精确路径 Bypass；其它路径仍要求认证
        │
 [Caddy / nginx]      ← 原样转发 body，剥掉客户端自带的身份头 / X-Internal-Token
        │ 127.0.0.1:8087
        ▼
 y8y-todo-line-webhook.service   (User=y8y-notify, 常驻)
   · 验证签名 / 请求体 ≤64 KiB / 事件数 ≤20 / 1:1 聊天 / 事件 ID 去重
   · 兑换一次性绑定码 → 写 binding-<user>.json
   · 消费测试推送队列 → 复用 notifications.LineProvider 发固定文本
        │
        │  <data>/notify/line-link/   (setgid 2770, 共享组 y8y-notify)
        │  每个文件只有一个写者；跨进程原子操作走同目录 flock 文件
        ▼
 y8y-todo.service                (User=y8y, Web)
   · 设置 → 通知 → LINE：生成绑定码 / 查状态 / 解绑 / 排测试推送
   · 只返回脱敏后的 LINE ID，不返回原始绑定码以外的任何服务端数据

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
  Link + 「复制绑定码」已经够用，且绑定码本身可直接粘贴。V1 因此只提供
  Deep Link 与复制按钮。
* **不建后台管理系统**：绑定关系就是每个用户一个 JSON 小文件，备份数据目录即可。
* **不做 LINE 侧回复消息**：成功/失败反馈全部在网页设置面板里（轮询），这样
  Webhook 处理路径里没有出网调用，响应更快、更不容易因 LINE 接口抖动而重试。
* **一个 Todo 用户 ↔ 一个 LINE 账号**：V1 的明确口径，冲突一律拒绝而不是静默覆盖。

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
   删码 → 作废该用户其它待用码 → 写结果。
6. 网页每 1.5 秒轮询 `GET /todo/api/line-link`，状态从 `pending` 变 `bound`
   后显示「LINE：已连接」+ 脱敏 ID。

同一个 `webhookEventId` 只处理一次；LINE 因超时重发时返回 `duplicate` 但不重复
绑定。已消费的码第二次出现返回 `invalid_code`。同一个 LINE 账号 10 分钟内
连续 8 次无效码后暂时不再处理它的消息（内存计数，重启清零 —— 绑定码有
192 bit 熵，暴力猜测本就不可行，这只是纵深防御）。

---

## 4. 数据文件与权限

都在 `<data>/notify/line-link/`（数据目录里，**不在源码静态目录**）：

| 文件 | 写者 | 内容 |
| --- | --- | --- |
| `code-<sha256>.json` | Web | 待用绑定码（用户 id、过期时间、`replace` 标记） |
| `result-<sha256>.json` | Webhook | 绑定结果（固定错误码，不含 LINE ID） |
| `binding-<sha256(user)>.json` | Webhook | Todo 用户 ↔ LINE User ID、绑定时间 |
| `test-<id>.req.json` / `.res.json` | Web / Webhook | 测试推送请求与结果 |
| `event-<sha256>.json` | Webhook | 已处理事件 ID（24 小时后 GC） |
| `test-rate.json` | Web | 每用户测试推送频率 |
| `lock` | 两者 | 空的 `flock` 文件 |

`<data>/notify/line-link-capabilities.json` 是 Webhook 服务每 60 秒重写一次的
**在线标记**（不含凭据）；超过 5 分钟没更新，网页显示「尚未启用」。

目录必须是 `setgid 2770`、属组 `y8y-notify`，`y8y` 是该组的成员：

```bash
sudo install -d -o y8y-notify -g y8y-notify -m 2770 /var/lib/y8y-todo/notify/line-link
sudo chmod 2770 /var/lib/y8y-todo/notify/line-link
```

两个单元的 `UMask=0022` 让新文件是 `0644`（组可读），而删除文件只需要**目录**
写权限 —— 所以两边都能建/删单写者文件，不需要 `0666`。

**备份 / 恢复**：绑定数据就在这个目录里，跟着 `<data_dir>` 一起备份即可
（见 README「Backups, restore, upgrades」）。恢复时先停 Web 与 Webhook 服务，
恢复目录，再启动；不需要迁移脚本，也不碰任务文件。

---

## 5. 凭据隔离（核心不变量）

| 凭据 | 存放位置 | 谁能读 |
| --- | --- | --- |
| Channel Access Token (`Y8Y_NOTIFY_LINE_TOKEN`) | `/etc/y8y-todo/y8y-todo-notify.env` (`root:root 600`) | 调度器 + Webhook 服务 |
| Channel Secret (`Y8Y_NOTIFY_LINE_CHANNEL_SECRET`) | 同上 | 调度器 + Webhook 服务 |
| Bot Basic ID (`Y8Y_LINE_BOT_ID`) | `/etc/y8y-todo/y8y-todo.env` | Web 服务（**公开信息，不是凭据**） |

* `y8y-todo.service` 的 `EnvironmentFile` 不引用 `y8y-todo-notify.env`。
* `serve.py` 与 `line_link.py` 里**不出现**任何 `Y8Y_NOTIFY_*` 变量名
  （`tests/test_line_link.py` 静态断言钉死）。
* Webhook 服务与调度器用 `y8y-notify`，与 Web 的 `y8y` 不同 UID —— 内核
  `ptrace_may_access()` 会拒绝跨 UID 读 `/proc/<pid>/environ`；进程启动还会
  `prctl(PR_SET_DUMPABLE, 0)` 做纵深防御（见 `src/procguard.py`）。
* 新单元的 `ReadWritePaths` 只有 `/var/lib/y8y-todo/notify`，`NoNewPrivileges`、
  `ProtectSystem=strict`、`ProtectHome`、`PrivateTmp` 全部沿用既有模板。
* **没有把 Hermes / Developer / Web 加进 root 组**，也没有新增任何 root 进程。
* Webhook 服务不调用 Todo 内部 API；`line_webhook.py` 里没有 `/todo/api/` 也没有
  `X-Internal-Token`。

---

## 6. Cloudflare Access 与反向代理

**不要为了 Webhook 关闭整个应用的身份认证。** 只给下面这一个精确路径开一个
独立且严格受限的入口：

1. Cloudflare Zero Trust → Access → Applications，为主应用
   `todo.example.com/*` 保留原来的认证策略（不变）。
2. 新建一个更具体的 Application，路径 `todo.example.com/line/webhook`，
   Policy = **Bypass / Everyone**（Access 按最长路径匹配，只有这个路径绕过）。
   建议再配一条 WAF Rate Limiting 规则限制该路径的 POST 频率。
3. 反向代理把该路径转发到 `127.0.0.1:8087`，**原样透传 body**：签名是对
   原始字节算的，任何重新序列化、压缩、改写都会让验签失败。

Caddy（放在需要认证的站点块**之前**，作为独立 site block）：

```caddyfile
todo.example.com {
    @linewebhook path /line/webhook
    handle @linewebhook {
        header_up -X-Internal-Token
        header_up -Cf-Access-Authenticated-User-Email
        reverse_proxy 127.0.0.1:8087
    }
    handle {
        # 原来的 forward_auth / 认证配置，一个字都不用改
        forward_auth ... 
        reverse_proxy 127.0.0.1:8086
    }
}
```

nginx（注意 `proxy_set_header` 的继承陷阱：一个 `location` 里只要写了任意一条
自己的 `proxy_set_header`，server 级的所有条目都不会被继承 —— 必须写在**同一块**）：

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
```

Webhook 服务本身只监听回环，只接受 `POST /line/webhook`，其它路径/方法一律
404；请求体 >64 KiB → 413，缺签名/签名不符 → 401。

### 验证 Webhook

```bash
# 1) 服务在跑且能力表新鲜（无凭据内容）
sudo systemctl status y8y-todo-line-webhook.service
python3 -m json.tool /var/lib/y8y-todo/notify/line-link-capabilities.json

# 2) 未带签名必须 401（从公网域名测，确认 Access 只放行了这一条路径）
curl -i -X POST https://todo.example.com/line/webhook -d '{}'     # 401

# 3) LINE Developers 控制台 → Messaging API → Webhook → 「Verify」应为 Success
# 4) 网页里绑定一次，发送测试通知，LINE 收到固定文本
# 5) 其余路径仍要求认证
curl -i https://todo.example.com/todo/api/tasks                   # Cloudflare Access 拦截
```

---

## 7. systemd 安装

只需要**新增一个**常驻单元；已有单元不用改：

```bash
sudo cp /opt/y8y-todo/packaging/y8y-todo-line-webhook.service \
        /etc/systemd/system/y8y-todo-line-webhook.service
sudo -e /etc/y8y-todo/y8y-todo-notify.env   # 填 Channel Secret + Token
sudo -e /etc/y8y-todo/y8y-todo.env         # 填 Y8Y_LINE_BOT_ID（可选）
sudo systemctl daemon-reload
sudo systemctl enable --now y8y-todo-line-webhook.service
```

`Y8Y_NOTIFY_ENABLED` 与 `y8y-todo-notify.timer` 保持不变 —— 正常提醒与摘要
仍由原来的调度器负责，自助绑定只是替它解析收件人。

---

## 8. 旧 `Y8Y_NOTIFY_LINE_TARGETS` 的迁移

**优先级（明确、固定）**：

1. 用户有自助绑定 → 用它。
2. 用户没有自助绑定、旧环境变量里有 → 用它（**回退**），保证升级不丢通知能力。
3. 两者都没有 → 该用户没有 LINE 渠道。

**冲突**（两者都在且指向不同 LINE 账号）由
`Y8Y_NOTIFY_LINE_CONFLICT_POLICY` 决定，绝不会静默二选一：

| 值 | 行为 | 适用 |
| --- | --- | --- |
| `binding`（默认） | 以用户自助绑定为准；调度器把冲突用户写进能力表的 `line_conflicts`，设置页显示「旧配置已被忽略，建议管理员移除」 | 推荐。用户自己认证后的选择就是权威收件人 |
| `strict` | 该用户**停发**（fail closed），直到运维移除旧条目 | 高安全环境，要求人工确认每一次收件人变更 |

两种策略下能力表都只写**用户 id**，不写 LINE User ID、不写凭据。

**建议的迁移顺序**（对每个已有用户，可随时做、无需停机）：

1. 让用户自己在网页里绑定 LINE，并点「发送测试通知」确认收到。
2. 管理员从 `y8y-todo-notify.env` 里删掉该用户的 `user=Uxxxx` 条目，
   `sudo systemctl restart y8y-todo-line-webhook.service`（env 是启动时读取的）。
3. 下一次 timer 运行会重发能力表，设置页的冲突提示自动消失。

在全部用户迁移完之前，旧条目可以原样留着 —— 未绑定的用户仍靠它收通知。

---

## 9. 测试通知

* 固定文本，`LineProvider` 复用既有 LINE push 客户端；正文**不能**由客户端指定。
* 只能发给**当前用户服务端已绑定的** LINE 账号；客户端传任何 `line_user_id`
  都会被忽略。
* 频率限制：同一用户 60 秒最多 1 条、1 小时最多 5 条，超限 429。
* Web 只把请求排进队列，由持有 Token 的 Webhook 服务执行；网页轮询结果，
  失败给出可理解的中文提示（超时 / 未绑定 / 发送失败）。
* **不写 `notification_state.json`**，因此完全不影响正常提醒的去重状态。

---

## 10. 常见错误与排查

| 现象 | 原因 / 处理 |
| --- | --- |
| 设置页显示「尚未启用 LINE 自助绑定」 | Webhook 服务没跑，或 `line-link-capabilities.json` 超过 5 分钟没更新。看 `systemctl status` / journal；确认 `<data>/notify/` 权限是 2770 且 `y8y-notify` 能写 |
| 「绑定 LINE」按钮灰着 | 同上，`available=false` |
| 发了绑定码但状态一直 `pending` | ① 确认是在**一对一**聊天里发的（群聊忽略）；② Cloudflare Access 是否只放行了 `/line/webhook`；③ 反向代理是否原样透传 body；④ journal 里看服务是否启动 |
| LINE 控制台 Verify 失败 | Channel Secret 填错 / 代理改写了 body / `X-Line-Signature` 被剥离。用 `curl -i` 未带签名应得 401 证明路径通 |
| 提示「绑定码已过期」 | 10 分钟 TTL，重新生成即可 |
| 提示「这个 LINE 账号已经绑定了其他 Todo 用户」 | 一个 LINE 账号只能绑一个 Todo 用户；让对方先解绑 |
| 测试通知超时 | Webhook 服务的 `Y8Y_NOTIFY_LINE_TOKEN` 无效 / 出网被挡；看 `journalctl -u y8y-todo-line-webhook.service` |
| 设置页显示「旧配置已被忽略」 | 该用户自助绑定与 `Y8Y_NOTIFY_LINE_TARGETS` 指向不同账号；按 §8 移除旧条目 |
| 用户解绑后仍收到旧 LINE 的通知 | 该用户可能还有旧环境变量条目（回退生效）；按 §8 移除 |

日志**不记录**消息正文、LINE User ID、身份头或签名值；排查时看状态码与
固定错误码即可。
