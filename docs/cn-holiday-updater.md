# 中国法定节假日年度更新器

一个每年只跑几次、纯标准库、无需常驻服务的小工具。它让 Todo 自己维护中国法定节假日
与调休数据：自动查官方通知 → 严格解析与校验 → 生成数据更新 → 建 PR → **人工合并**。

## 1. 它是怎么工作的

```
GitHub Actions（定时 / 手动）
        │
        ▼
算目标年份（1 月查当年，其它月份查下一年）
        │
        ├─ 该年份已有完整安排 ─────────────► 结束，一个字节都不改
        │
        ▼
① 读取中国政府网「最新政策」结构化索引（ZUIXINZHENGCE.json）—— 首选
        │
        ├─ 命中目标年份通知 ─────────────► 直接用它（不再查历史来源）
        │
        ├─ 索引损坏 / 抓取失败 ──────────► check-unavailable：失败并报告
        │
        └─ 未命中 → ② 必须再查**国务院政策文件库历史归档检索**
                     （sousuo.www.gov.cn/search-gov/data, t=zhengcelibrary_gw）
                     按标题一次取回 2007 年至今全部年度通知，与滚动列表互相独立
                       │
                       ├─ 归档查不到任何往年通知 / 返回异常 / 不可达
                       │      └─► check-unavailable：无法确认，绝不误报「未公布」
                       │
                       ├─ 多个官方地址冲突 / 地址不可信 ─► 停止，报告
                       │
                       ├─ 归档命中目标年份 ─► 用历史归档里的官方 URL（不漏检）
                       │
                       └─ 归档健康但缺目标年份 → ③ 精确年份检索二次确认
                              ├─ 官方明确返回「没有找到相关结果」且归档也无 → not-published
                              ├─ 精确检索命中（归档尚未收录）→ 用该 URL
                              └─ 精确检索异常 → check-unavailable
        │
        ▼
抓取通知全文（只从可信官方域名），严格解析七条安排
        │  · 标题必须精确匹配目标年份
        │  · 逐条解析放假区间、共 N 天、调休上班日
        │  · 交叉校验日期、星期、天数、区间不重叠、七个节日齐全
        │  · 从「节日本身」日期推导连休中的覆盖标签（overrides）
        │  · 任何一条不通过 ─► 报告并转人工，绝不猜日期
        ▼
原子写入 holidays/cn-holidays.json + src/index.html 的受保护区块
        │  · 只替换 BEGIN/END marker 之间的文本，其余逐字节不变
        │  · 写入前后校验 @media 头数量（历史事故：误删后手机端不可用）
        │  · 任一步失败 → 回滚到写入前字节，不留半写入文件
        ▼
自动验证 → 建 PR（Bot 身份）→ 人工审批合并
```

不依赖 Hermes、不依赖 LLM、不依赖任何个人 Agent Session，也不需要每年启动一次开发任务。
Todo 应用运行时也不发任何网络请求：节假日数据仍是 `src/index.html` 里的内联常量，
只是这段常量现在由结构化数据生成。

## 2. 数据放在哪

| 文件 | 角色 |
|---|---|
| `holidays/cn-holidays.json` | **唯一数据真源**（含 `sources` 来源 URL / 文号、`festivals` 节日本身、`schedules` 正式安排） |
| `src/index.html` | `BEGIN/END Y8Y_CN_HOLIDAY_DATA` 之间的**生成区块**，`CN_HOLIDAY_SCHEDULE` / `CN_FESTIVALS` / `CN_FIXED_FESTIVALS` |
| `scripts/cn_holiday_updater.py` | 检查、解析、校验、生成、原子写入（纯标准库） |

> 仓库的 `data/` 目录被 `.gitignore` 忽略（那是运行时任务数据目录），所以真源放在
> `holidays/`。前端渲染逻辑、用户可见性开关、日本祝日逻辑、任务数据格式都没有改动。

有 `festivals[year]` **不代表**该年已有正式放假安排。只有 `schedules[year]` 存在才算完成，
两者分别判断。

## 3. 官方来源规则

两个互相独立的官方发现来源，缺一不可：

| 优先级 | 来源 | 用途 | 局限 |
|---|---|---|---|
| 首选 | `www.gov.cn/zhengce/zuixin/ZUIXINZHENGCE.json` | 最新政策结构化索引 | 滚动列表，通知可能被挤出 |
| 兜底 | `sousuo.www.gov.cn/search-gov/data`（`t=zhengcelibrary_gw`，国务院政策文件库） | 按标题一次取回 2007 年至今全部年度通知 + 精确年份检索 | 与首选独立，不会因为「最新」被清理而漏检 |

- **最新索引里没有 ≠ 官方没发布**。首选未命中时必须走兜底；只有「最新索引 + 历史归档 + 精确年份检索」三方都确认缺席，才返回 `not-published`。
- 历史归档必须自检：如果连往年通知都查不到，说明索引本身不可信 → `check-unavailable`，绝不误报。
- 可信通知全文域名白名单：`www.gov.cn` / `gov.cn`、新华社、人民网；检索接口域名 `sousuo.www.gov.cn` **只用于查询**，不能作为通知全文来源。
- 只认《国务院办公厅关于 XXXX 年部分节假日安排的通知》**原文**，标题必须精确匹配目标年份（检索结果里的 `<em>` 高亮标签会先归一化）。
- 不采信：聚合页、预测文章、社交媒体、根据农历自行推算的调休日期、无文号网传安排。
- 网页存在 ≠ 数据可信：必须逐条严格解析并通过全部校验才写数据；检索记录文号与通知全文文号不一致也会停下。
- 每条安排都保留来源 URL 与文号（存在时）写入 `sources`，便于日后追溯。
- 冲突（多个不同官方地址）或来源不可信 → 停止自动提交。

## 4. 定时检查

`.github/workflows/cn-holiday-updater.yml`：

```yaml
on:
  schedule:
    - cron: '0 1 16 11,12,1 *'   # 01:00 UTC = 10:00 JST
  workflow_dispatch: ...
```

- 11/16、12/16、1/16 各检查一次；1 月查当年（补漏录），其余查下一年。
- GitHub Actions 定时执行可能被平台延迟，语义是「这三天各检查一次」，不是精确准点。
- `workflow_dispatch` 手动入口支持 `dry_run` / `offline_rehearsal` / `notice_url`。
- 只有这一套调度；没有新增常驻服务、Timer 或第二套调度系统。
- 防重复：目标年份已录入直接跳过；同名分支已有 open PR 时不重复创建。

## 5. 本地 / CI 可复现运行

```bash
# 检查（真实联网：最新政策索引 → 历史归档 → 精确年份检索）
python3 scripts/cn_holiday_updater.py check --today 2026-11-16

# 完全离线演练：通知已发布、但已被最新索引移除（本次修复的核心回归场景）
python3 scripts/cn_holiday_updater.py check --no-network --today 2026-12-16 \
  --index-feed tests/fixtures/cn_holiday/feed-not-published.json \
  --history-feed tests/fixtures/cn_holiday/history-archive-with-2027.json \
  --notice-html tests/fixtures/cn_holiday/notice-2027.html

# 完全离线演练：三方确认尚未公布
python3 scripts/cn_holiday_updater.py check --no-network --today 2026-11-16 \
  --index-feed tests/fixtures/cn_holiday/feed-not-published.json \
  --history-feed tests/fixtures/cn_holiday/history-archive.json \
  --exact-feed tests/fixtures/cn_holiday/history-exact-none.json

# 在隔离副本上演练写入（不要对真实数据文件直接跑）
cp -r holidays src /tmp/cn-holiday-rehearsal/
python3 scripts/cn_holiday_updater.py apply --root /tmp/cn-holiday-rehearsal \
  --no-network --index-feed tests/fixtures/cn_holiday/feed-2027-published.json \
  --notice-html tests/fixtures/cn_holiday/notice-2027.html

# 一致性与 @media 头自检
python3 scripts/cn_holiday_updater.py verify

# 按结构化数据重新生成前端区块（修 drift）
python3 scripts/cn_holiday_updater.py regenerate
```

`tests/fixtures/cn_holiday/` 里的 2027 通知是**虚构测试样本**，只用于测试与离线演练，
永远不会写进真实节假日数据；调度也不会拿它建 PR。

测试：

```bash
python3 -m pytest --basetemp=.pytest-tmp
bash scripts/check-public-tree.sh
bash scripts/check-ci-gates.sh
```

浏览器回归脚本 `tests/browser_cn_holidays.py` 连的是本地 dev-mode 隔离实例，
已挂到仓库既有的两个浏览器 E2E job，不新增测试 job、不改 Required Check 名称。

## 6. PR 自动创建的前置条件

默认 `GITHUB_TOKEN` 权限是只读；仓库设置 `Allow GitHub Actions to create and approve
pull requests` 也未启用。工作流因此**不使用个人账号或 PAT 绕过**，而是使用仓库既有的
GitHub App Bot 身份：

- repository variable：`CN_HOLIDAY_APP_ID`
- repository secret：`CN_HOLIDAY_APP_PRIVATE_KEY`

配置后，`propose` job 用 `actions/create-github-app-token` 铸一个仅含
`contents: write` + `pull-requests: write`、且限定本仓库的安装令牌，再建 PR。
Bot 身份建 PR 才能让 4 个 `gate / *` Required Checks 正常触发；PR 仍需人工审批合并。

**当前这两个凭据尚未配置**：定时检查、解析、校验、生成补丁都能自动完成，
但自动建 PR 会被明确报为阻塞（job 失败 + `$GITHUB_STEP_SUMMARY` 说明 + 补丁 artifact），
不会静默跳过，也不会降级权限。配置方法见仓库管理员的常规 Secrets/Variables 页面。

## 7. 仍需要人工确认的步骤

1. 官方通知的识别与复核：解析器只在**逐条严格通过**时才自动写数据；任何异常都报告并停下。
   需要时可用 `workflow_dispatch` 的 `notice_url` 做来源交叉核对。
2. 农历节日对应的「节日本身」日期：未来年份若尚未录入 `festivals`，解析器会要求人工补录，
   不会自行推算。
3. PR 的人工审批与合并；合并后如需上生产，走部署流程并单独授权。
4. 首次有效检查后，确认新旧两套更新机制不会同时写同一数据源，再关闭旧任务。

## 8. 失败与恢复

| 情形 | 行为 |
|---|---|
| 目标年份已录入 | 幂等跳过，不改文件 |
| 官方通知未公布 | 需「最新索引 + 历史归档 + 精确年份检索」三方确认；不写占位数据，报告下次检查时间 |
| 通知已发布但退出最新索引 | 由历史归档（或精确年份检索）发现，照常严格解析、生成更新 |
| 首选索引抓取失败 / JSON 损坏 | `check-unavailable`，任务失败并报告，不误报「未公布」 |
| 历史归档不可达 / 返回异常 / 查不到往年通知 | `check-unavailable`（自检失败），**绝不**误报「未公布」 |
| 精确年份检索异常 | `check-unavailable`，不误报「未公布」 |
| 来源不可信 / 来源冲突 / 检索文号与全文文号不一致 | 停止自动提交，报告 |
| 解析或校验不通过 | `needs-human-review`，不写任何数据 |
| 写入中途失败 | 回滚到写入前字节；不留下半写入文件 |
| 写入后 `@media` 头数量变化 / 旧年份被改动 | 回滚并报错 |
| 生成 PR 凭据缺失 | 保留已验证补丁 artifact，明确报阻塞 |

手工恢复：把工作区还原到上一个已合并 commit，修好原因后重跑
`workflow_dispatch`；`verify` 与 `regenerate` 可用来检查/修复前端区块与 JSON 的 drift。

## 9. 边界

- 本工作流**不部署生产**、不自动合并、不改 Required Check、不改任务数据。
- 代码合并到 `main` 不等于生产已更新；生产部署是另一套授权流程。
- 定时工作流只有合并到默认分支后才会真正启用。
