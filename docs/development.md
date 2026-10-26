# Development Guide

> 给在这个仓库里写代码的人（和 agent）的操作手册。
> 硬性规则在 [`AGENTS.md`](../AGENTS.md)；本文只讲「怎么做」。

---

## 1. 环境

核心**零第三方依赖**——标准库 Python 3.10–3.13 就能跑。

```bash
git clone https://github.com/y8y-systems/y8y-todo.git
cd y8y-todo
Y8Y_DEV_MODE=1 python3 src/serve.py
```

打开 <http://127.0.0.1:8086/todo/>。

`Y8Y_DEV_MODE=1` 给一个本地用户（`dev`）、强制绑 `127.0.0.1`、数据落在仓库内
`dev-data/`——**不可能撞到任何真实部署的数据**。

### 测试依赖

```bash
python3 -m pytest --basetemp=.pytest-tmp          # 主套件
pip install playwright && playwright install chromium   # 仅 UI 脚本需要
```

浏览器脚本（`tests/browser_verify.py`）默认连 **18086**（历史端口；应用自身默认
8086），用 `Y8Y_VERIFY_PORT` / `Y8Y_VERIFY_BASE` 覆盖。

---

## 2. 日常循环

```bash
# 1. 同步 main
git fetch origin && git checkout main && git pull --ff-only origin main

# 2. 开分支
git checkout -b feat/<short-description>

# 3. 改代码 → 跑测试 → 再改
python3 -m pytest --basetemp=.pytest-tmp

# 4. 提交（只 add 你改的文件，别无脑 git add -A）
git add src/serve.py tests/test_xxx.py
git diff --cached --stat          # 扫一眼：有没有混进奇怪文件
git commit -m "feat: <一句话说清做了什么>"

# 5. 推 + 开 PR
git push -u origin HEAD
gh pr create --title "feat: ..." --body-file /tmp/pr-body.md
gh pr checks --watch              # 等 CI
```

**建 PR 的身份**：PR 作者决定谁能 Approve —— 作者不能 Approve 自己建的 PR。
Agent 建的 PR 应当用**独立于人工审批者**的身份，这样评审才成立。
你的 Agent 环境如果提供了专用身份工具，就用它；没有的话用自己的账号建 PR
也能工作，只是**你自己不能批这个 PR**，需要另一位有权限的人来批。
详见 [AGENTS.md §2.1](../AGENTS.md)。

**不要 `git add -A`。** 这个仓库有 `vendor/`、`docs/font-migration/*.png`（几十 KB
的对比图）、`rehearsal/legacy-baseline/`——`git add -A` 容易把临时产物扫进来。
提交前永远先 `git diff --cached --stat`。

---

## 3. 代码地图

| 路径 | 是什么 | 改它要注意 |
|---|---|---|
| `src/serve.py` | 后端全部逻辑（单文件 stdlib HTTP 服务） | 路径拼接一律走 `safe_join`；别硬编码绝对路径 |
| `src/index.html` | 前端 SPA 全部（单文件 vanilla JS） | 输出到 HTML 的用户内容要 `escapeHtml`；`@media` 头别误删 |
| `src/config.py` | 环境变量解析 | 新增变量要同步 `README.md` §Configuration + `packaging/y8y-todo.env.example` |
| `src/taskstore.py` | 任务文件解析/写盘的纯函数（serve 与通知引擎共用） | 行为要跟旧实现逐字一致；改这里等于改数据格式 |
| `src/notifications.py` | 通知引擎（渠道 provider、设置、投递状态、调度） | 纯 stdlib；凭据只从环境变量读，绝不写进 Web 端 |
| `src/notify_runner.py` | 通知调度器 CLI（一次性，systemd timer 驱动） | 退出码 0/1 的语义别改；provider 失败不许崩 |
| `vendor/y8y-shared-chrome/` | 共享 UI 壳（tokens/icons/字体/主题引擎） | 是 vendored 的上游副本，改动要考虑同步 |
| `tests/` | pytest 套件 + 浏览器脚本 | 新功能先在 `tests/conftest.py` 看 fixture 怎么造数据 |
| `rehearsal/legacy-baseline/` | 旧版行为快照（升级演练用） | **不要手改**——`MANIFEST.sha256` 是防篡改 tripwire，改了 CI 会红 |
| `packaging/` | systemd unit + env 样例 | 样例里只能是占位符，绝不能有真实值 |

### 数据格式（不可轻动）

任务 = markdown + frontmatter；完成的任务进 `done/<date>/`；列表在 `lists/<id>/`；
跨设备状态在 `persistent_projects.json`。

**发布后的格式不能被静默改写。** 改了就是数据迁移，必须：
1. 写迁移方案（进 `docs/upgrade-runbook.md`）
2. 加兼容测试（`tests/test_legacy_compat.py` 那套）
3. 保证旧数据仍然读得动

---

## 4. 测试怎么写

用 `tests/conftest.py` 里的 fixture 造**虚构数据**。绝不写真实任务内容。

| 我改了什么 | 至少要跑 |
|---|---|
| 任何后端逻辑 | `python3 -m pytest --basetemp=.pytest-tmp`（全套） |
| 认证 / 权限 | `tests/test_auth.py` `tests/test_security.py` |
| 任务 / 存储 | `tests/test_tasks.py` `tests/test_legacy_compat.py` |
| 列表 / 购物 / 项目 | `tests/test_lists.py` |
| API 契约 | `tests/test_api_compat.py` |
| 配置 / 数据目录 | `tests/test_config.py` `tests/test_data_dir.py` |
| 通知系统 | `tests/test_notifications.py`（`tests/browser_notify.py` 跑 UI） |

**改了认证、存储、API → 必须加对应回归测试**，不能只靠原有测试「顺便覆盖」。

UI 改动要跑浏览器脚本并截图：

```bash
Y8Y_DEV_MODE=1 python3 src/serve.py &
python3 tests/browser_verify.py            # 默认 18086，按实际端口覆盖
python3 tests/browser_notify.py           # 通知设置面板（默认 8086）
```

---

## 5. 提交前自查

```bash
# 隐私门（CI 里也会跑：pytest workflow 的 public tree privacy gate job）
bash scripts/check-public-tree.sh

# 看清楚你要提交什么
git diff --cached --stat
git diff --cached | grep -inE 'password|secret|token|api_key|/Users/|/root/'

# 完整测试
python3 -m pytest --basetemp=.pytest-tmp
```

隐私门会拦：开发者家目录路径、私人邮箱、私钥材料、GitHub token、指向本 org 其他
仓库的引用，以及**文件名本身**带邮箱/token/凭据关键字的文件。

门的输出是**脱敏**的 —— 三条铁律：

1. 命中的**内容**永远不打印，只报「问题类型 + 文件位置 + 行号」；
2. **文件名**本身带敏感信息时也不打印（路径里的敏感片段会被打码，控制字符会被
   替换，否则一行输出会裂成多行）；
3. 扫描器自己出错时**门必须红** —— 扫描没跑起来却报 CLEAN 是最坏的失败模式。

门的输出会进 CI 日志 —— 日志长期留存，仓库一旦公开更是人人可见。
把要找的东西再原样打印一遍没有任何好处。要看原文自己打开那个文件那一行。

这道门是**公开仓库的二次检查**。机器级（push 前）的隐私检查 —— 覆盖将要上传的
commit 历史、作者邮箱、敏感文件 —— 属于全局 Agent 基础设施，由**私有运维系统**统一管理，不在本仓库里。
它**不是**万能的——你自己也要用眼看一遍 diff。

---

## 6. PR 描述模板

```markdown
## 改了什么
（一两句，说清行为变化）

## 为什么
（问题是什么）

## 测试
- `python3 -m pytest --basetemp=.pytest-tmp` → N passed
- UI：`tests/browser_verify.py` → 通过（附截图）

## 影响面
- [ ] 数据格式
- [ ] 认证 / 权限
- [ ] API 契约（y8ysystems 兼容）
- [ ] 无

## 需要人工验收
- [ ] 是（UI 改动，请看截图）
- [ ] 否

## 建议
- [ ] 可以 merge
- [ ] 建议再改（说明）
- [ ] 需要讨论（说明）

## 升级方案
- [ ] 不需要
- [ ] 需要（已写进 docs/upgrade-runbook.md §X）
```

---

## 7. 三道门

```
### CI Gate（改动测试 job 时要同步）

仓库的 Ruleset 只配 4 个**稳定名称**的 required check：

```
gate / pytest     gate / ubuntu     gate / e2e     gate / upgrade
```

内部测试 job 增删改名时，**Ruleset 不用动** —— 但你要同步该 workflow 的
gate（`needs:` + `RESULT_*` env）。漏改的话 CI 会红：

```bash
bash scripts/check-ci-gates.sh     # 提交前自查
```

详细规则见 [`AGENTS.md`](../AGENTS.md) §4.1。

---

开发完成 ──▶ PR ──▶ CI 全绿 ──▶ 【人工批准】──▶ merge 到 main
                                                      │
                                                      ▼
                                       【另行授权】生产部署
```

`main` = **可部署的候选**，不是「已上线」。
生产部署在**私有运维系统**里管，本仓库的 CI **不碰生产**。

详见 [`AGENTS.md`](../AGENTS.md) §6 和 [`release-process.md`](release-process.md)。
