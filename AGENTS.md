# AGENTS.md — y8y Todo 开发契约

> 任何 AI agent（Claude Code / Codex / Cursor / Hermes / …）在这个仓库里动手前，
> 先读完本文件。它是**硬性规则入口**，不是教程。
> 详细文档看 `docs/`，别在这里重复。

---

## 1. 项目身份

- 本仓库（`y8y-systems/y8y-todo`）是 **y8y Todo 唯一的软件源代码**。
- **正式开发以 `main` 为基线。** 不存在需要同步维护的第二套生产源码。
- 私人数据、生产配置、服务器凭据**不属于本仓库**，永远不要提交进来。
- 生产部署由**私有运维系统**管辖。本仓库只管代码怎么写、怎么测、怎么合。

---

## 2. 标准开发流程

1. 读 `README.md`、本文件，以及任务相关的 `docs/`。
2. 从**最新的 `main`** 创建功能分支：
   ```bash
   git fetch origin && git checkout main && git pull --ff-only origin main
   git checkout -b feat/<short-description>     # 或 fix/ docs/ ci/ test/
   ```
3. 实现需求。
4. 添加或修改**必要的测试**（见 §4）。
5. 执行相关自动化测试：
   ```bash
   python3 -m pytest --basetemp=.pytest-tmp
   ```
6. 创建 PR（标题用 Conventional Commits：`feat:` / `fix:` / `docs:` / `test:` / `ci:`）。
   **身份要求见 §2.1。**
7. **等 CI 全绿**（`gh pr checks <N> --watch`）。
8. 在 PR 里提供：测试结果 + 必要的 UI 对比截图（有 UI 改动时必给）。
9. 等待人工验收。
10. **未经批准不自行 Merge。**

> 分支命名：`feat/` `fix/` `refactor/` `docs/` `ci/` `test/`
> 提交信息：一句话说清「做了什么」，用中文或英文都行，别写 `update` / ` wip`。

---

## 2.1 PR 身份与人工审批（硬性）

**Agent 必须用独立于人工审批者的身份创建 PR。** GitHub 不允许账号本人 Approve
自己创建的 PR —— 如果 Agent 用自己的账号建 PR，评审链从一开始就断了，没人能批。

- ❌ **不可绕过人工审批**：不自行 Approve、不改 Ruleset、不把 required check
  降级来「让门变绿」。
- ❌ **不可自动 Merge**（见 §6）。
- ❌ **不得提交凭据**：Token、私钥、`.env` 真实值、installation token、
  credential helper 配置 —— 一律不进仓库（见 §3）。
- ✅ **用哪个身份，由部署环境的 Agent 工具负责。** 本仓库只记录这条通用原则，
  **不记录任何机器专用的工具名、路径或凭据配置** —— 那是环境的事，不是软件的事。
- ✅ **commit 作者用 GitHub noreply 邮箱**，不用私人邮箱 —— 公开仓库里 commit
  作者邮箱人人可见，而 `scripts/check-public-tree.sh` 只扫文件内容，扫不到
  commit 作者。

本仓库期望的 commit 作者身份（用 `git config --local` 固定，避免被全局配置带偏）：

```bash
git config --local user.name  "y8y-systems"
git config --local user.email "y8y-systems@users.noreply.github.com"
```

---

## 3. 开发约束（硬性）

- ❌ 不把服务器硬编码路径写进应用（`/root/...`、`/opt/...`、`/Users/...` 一律不行）。
  配置走环境变量，见 `README.md` §Configuration。
- ❌ 不提交真实用户数据（任务正文、列表内容、`.md` 任务文件、真实文件名）。
- ❌ 不提交 Token、密钥、`.env` 真实值、`internal_tokens.json`、OAuth 凭据。
- ❌ **不用私人邮箱做 commit 作者**（公开仓库里 commit 作者邮箱人人可见）——
  用 GitHub noreply 邮箱，见 §2.1。
- ❌ **不用与人工审批者同一个身份创建 PR** —— 见 §2.1。
- ❌ 不直接修改生产服务器上的源码——生产只接受已合并的 commit。
- ❌ 不为了小功能引入无关的大型重构（这项目是 stdlib-only + 无构建步骤，别破坏它）。
- ✅ **保留数据格式兼容性。** 磁盘格式（frontmatter 字段、`done/<date>/`、`lists/<id>/`、
  `persistent_projects.json`）一旦发布就不能被静默改写；要改必须走数据迁移方案 + 升级 runbook。
- ✅ 改动**认证 / 存储 / API** 时，**必须**补对应回归测试（见 §4）。
- ⚠️ 与生产部署有关的任何操作，需要**单独授权**——本仓库的 CI 不部署生产。

公开树隐私门是 `scripts/check-public-tree.sh`（入口；实现在
`scripts/check-public-tree.py`，纯标准库），**CI 里真的在跑**
（`pytest` workflow 里的 `public tree privacy gate` job，是 `gate / pytest` 的
上游 —— 它红了 PR 合不进去）。
它能挡住：开发者家目录路径、私人邮箱、私钥材料、GitHub token、指向本 org 其他
私有仓库的引用，以及**文件名本身**带邮箱/token/凭据关键字的情况。

⚠️ **门的输出是脱敏的**，三条铁律：

1. 命中的**内容**永远不打印 —— 只报「问题类型 + 文件位置 + 行号」；
2. **文件名**本身带敏感信息时也不打印 —— 路径里的敏感片段会被打码，
   控制字符（换行等）会被替换（否则一行输出会裂成多行）；
3. 扫描器自己出错（列文件失败、正则不合法）时**门必须红** —— 扫描没跑起来却
   报 CLEAN 是最坏的失败模式。

门的输出会进 CI 日志 —— 日志长期留存，仓库一旦公开更是人人可见。
把要找的东西再原样打印一遍没有任何好处。要看原文自己打开那个文件那一行。

**这道门只是公开仓库的二次检查。** 机器级（push 前）的隐私检查 —— 覆盖将要上传的
commit 历史、作者邮箱、敏感文件 —— 属于全局 Agent 基础设施，由**私有运维系统**
统一管理，**不塞进本仓库**。

---

## 4. 测试要求

| 改动类型 | 必须补的测试 |
|---|---|
| 认证 / 权限 | `tests/test_auth.py`、`tests/test_security.py` |
| 任务存储 / 文件格式 | `tests/test_tasks.py`、`tests/test_legacy_compat.py` |
| 列表 / 购物 / 项目 | `tests/test_lists.py` |
| API 契约（含 y8ysystems） | `tests/test_api_compat.py` |
| 数据目录 / 配置 | `tests/test_config.py`、`tests/test_data_dir.py` |
| 旧数据兼容 | `tests/test_legacy_compat.py`、`tests/test_production_data_shapes.py` |
| 前端 / UI | `tests/browser_verify.py`（需 playwright + chromium） |

新增测试放 `tests/`，用 fixture 造**虚构数据**，绝不写真实任务内容。

---

## 4.1 CI Gate — 稳定名称的汇总检查（**你的责任**）

仓库有 4 个 CI workflow，每个带一个**固定显示名**的汇总 job：

| Gate 显示名 | 汇总的 workflow | 覆盖的测试 job |
|---|---|---|
| `gate / pytest` | `pytest.yml` | `test` |
| `gate / ubuntu` | `ubuntu-2204.yml` | `pytest-310`、`systemd-249`、`standalone-python`、`browser-2204` |
| `gate / e2e` | `linux-deploy-e2e.yml` | `linux-e2e`、`systemd-e2e`、`browser-e2e` |
| `gate / upgrade` | `p4-upgrade-rehearsal.yml` | `upgrade-rehearsal` |

**为什么有它**：仓库的 Ruleset（Required Status Checks）只配这 **4 个名字**。
内部测试 job 怎么增删改名、改矩阵、调显示名，**Ruleset 都不用动** ——
否则改一次矩阵就得手改一次 Ruleset，而且很容易漏改导致 PR 永久卡住。

### 你必须做什么

**改动内部测试 job 时，同步该 workflow 的 gate：**

| 你改了什么 | 你必须同步什么 |
|---|---|
| 新增测试 job | gate 的 `needs:` 加它 + 加一个 `RESULT_<label>` env |
| 删除测试 job | gate 的 `needs:` 删它 + 删对应 `RESULT_<label>` env |
| 重命名测试 job | 同上（`needs:` 里的名字 + `RESULT_*` 里的引用） |
| 改测试 job 的 `name:`（显示名） | **不用改** —— gate 只引用 job id，不引用显示名 |

> 判定逻辑写在 `scripts/ci-gate-check.sh`（四处共用），**不要**在 workflow 里
> 另写一份判定 —— 那样会漂移。

### 你不需要做什么

- ❌ **不用**改 Ruleset（`gate / *` 名字不变）
- ❌ **不用**保持 gate 的 `name:` 不变之外的任何东西

### 漏改会怎样（机器会抓，不是靠自觉）

`tests/test_ci_gates.py` + `scripts/check-ci-gates.sh` 会在 CI 里检查：

1. 每个 workflow 恰好一个 gate，显示名符合 `gate / <slug>` 约定
2. **gate 的 `needs:` 恰好等于该 workflow 的全部测试 job** ——
   漏一个 = 有测试没被门覆盖（会红）；多一个 = 依赖不存在的 job（会红）
3. gate 有 `if: always()`
4. `RESULT_*` env 与 `needs` 一一对应
5. 判定脚本的行为：`failure` / `cancelled` / **`skipped`** 都必须让门失败

> 也就是说：**你漏改了，CI 会红，PR 合不进去** —— 不需要人肉检查。

### ⚠️ `if: always()` 为什么不能删

GitHub 的 Required Status Checks 把 **`skipped` 视为「已满足」**。
所以如果 gate 因为上游失败而被 skip，**门就形同虚设** ——
上游红了，但 required check 显示通过，PR 照样能合。

正确做法：gate 用 `if: always()` **始终运行**，然后显式检查每个
`needs.<job>.result`，任何不是 `success` 的就让 gate **失败**
（必须是 `failure`，不能是 `skipped`）。

`tests/test_ci_gates.py` 里有一条专门锁这个语义的断言。

---

## 5. 交付标准

Agent 完成开发后，PR 描述或最终回复里必须包含这 7 项：

1. **PR 链接**
2. **修改摘要**（改了什么、为什么）
3. **测试结果**（跑了什么、通过/失败、贴关键输出）
4. **是否需要用户人工验收**（UI 改动 → 需要）
5. **是否涉及数据 / 认证 / API 兼容性**（涉及 → 说清影响面）
6. **是否建议 Merge**（建议 merge / 建议再改 / 建议找用户讨论）
7. **是否需要专门的升级方案**（涉及数据格式变更 → 需要，写进 `docs/upgrade-runbook.md`）

---

## 6. 关键区分：开发完成 ≠ PR 合并 ≠ 生产部署

这三件事是**三道独立的门**，不要混为一谈：

```
开发完成  ──▶  PR 创建  ──▶  CI 全绿  ──▶  【人工批准】  ──▶  合并到 main
                                                              │
                                                              ▼
                                          【另行授权】生产部署（私有运维系统管辖）
```

- **合并到 `main` 不等于上了生产。** `main` 只是「可部署的候选」。
- **CI 绿不等于可以合并。** 合并要人工批准。
- **生产部署是另一套系统、另一套授权。** 见私有运维系统。

---

## 7. 文档地图

| 文件 | 内容 |
|---|---|
| `README.md` | 产品介绍、配置项、systemd 部署、数据格式 |
| `docs/deployment.md` | 完整部署指南（反向代理 / systemd / 机器 token） |
| `docs/upgrade-runbook.md` | 升级 + 回滚 runbook（发布与回退） |
| `docs/legacy-data.md` | 历史数据格式与软隐藏字段 |
| `docs/acceptance.md` | 验收清单 |
| `scripts/check-public-tree.sh` | 公开树隐私门（CI 里真跑，输出脱敏）—— 入口 |
| `scripts/check-public-tree.py` | 上面那道门的实现（纯标准库；文件名/路径/内容全部脱敏，扫描失败 Fail Closed） |
| `tests/test_public_tree_gate.py` | 上面那道门的回归测试（接在 CI 上 / 真的会失败 / 输出不含敏感值 / 扫描失败必须红） |

| `scripts/ci-gate-check.sh` | CI Gate 判定逻辑（四个 workflow 共用） |
| `scripts/check-ci-gates.sh` | CI Gate 自检（门自己会报警，见 §4.1） |
| `scripts/p5-host-check.sh` | 主机身份硬核对（部署侧用，在私有运维系统里） |
| `rehearsal/legacy-baseline/` | 旧版基线快照（升级演练用） |

---

## 8. 私有运维规范在哪

生产部署、回滚、服务器操作**不在本仓库**。它在 y8y 组织下的**私有运维仓库**里
（公开仓库不引用私有仓库地址——`scripts/check-public-tree.sh` 会拦截）。

- 开发规范（公开）：就是本仓库 `y8y-systems/y8y-todo`
- 私有运维规范：另一个 PRIVATE 仓库，由有运维权限的 agent 按本地配置读取

**开发任务只读本仓库；生产运维任务两个都要读。**
本仓库永远不引用私有仓库的内容，也不把它做成 submodule，
更不让公开项目的构建和测试依赖私有仓库。
