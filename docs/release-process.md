# Release Process

> 从「改动写完」到「生产跑上新版本」的完整链路。
> 硬性规则在 [`AGENTS.md`](../AGENTS.md)；本文讲流程细节。

---

## 0. 一句话

```
分支 → PR → CI 全绿 → 人工批准 → merge 到 main
                                      │
                                      ▼
                 运维方在私有运维系统里按 commit SHA 部署
```

**merge ≠ 上线。** `main` 只是「可部署的候选」。

---

## 1. 三道独立的门

| 门 | 谁管 | 通过标准 | 过了之后 |
|---|---|---|---|
| ① CI 全绿 | GitHub Actions | 所有 workflow `success` | PR 可以被审 |
| ② 人工批准 merge | 仓库 owner | owner 明确同意 | `main` 前进 |
| ③ 生产部署授权 | 生产 owner（Kara） | 明确指定要部署的 commit SHA | 生产跑上新版本 |

**任何一道没过，下一道就不许开始。**
特别是 ③：它跟 ① ② 完全独立——CI 再绿、main 再新，没有明确部署授权也不许动生产。

---

## 2. ① CI

本仓库有 4 条 workflow：

| Workflow | 覆盖什么 |
|---|---|
| `pytest.yml` | 主 pytest 套件（快门） |
| `ubuntu-2204.yml` | Ubuntu 22.04 + 系统 Python 3.10 + 真 systemd 249 + Chromium E2E + 独立 3.12 runtime |
| `linux-deploy-e2e.yml` | 全新克隆 → 非 root 安装 → 启停 → 数据完整的完整生命周期 |
| `p4-upgrade-rehearsal.yml` | 旧↔新数据兼容演练（备份→迁移→回滚 drill A/B） |

```bash
gh pr checks <N> --watch
```

**CI 红了不要 merge。** 修完再推。自动修复循环见 `github-pr-workflow` skill。

---

## 3. ② Merge

- 用 **Squash merge**（保持 `main` 历史线性）。
- PR 标题用 Conventional Commits 前缀（`feat:` `fix:` `docs:` `test:` `ci:`）。
- **未经 owner 批准不要自行 merge。** Agent 的默认动作是「开 PR 并停下」。

---

## 4. ③ 发布

### 4.1 确定发布版本

**必须是明确的 commit SHA**，不能是「最新 main」——否则每次发布不可追溯。

```bash
git fetch origin
git rev-parse origin/main          # 记录这个 SHA
git log --oneline -1 origin/main
```

记下这个 SHA，它就是本次发布的版本号。

### 4.2 发布前检查

- [ ] PR 已 merge，CI 全绿
- [ ] 目标 commit SHA 已确定并记录
- [ ] 本次改动**不涉及**数据格式变更
      → 涉及则必须先走 [`upgrade-runbook.md`](upgrade-runbook.md) 的数据迁移流程
- [ ] 本次改动**不涉及**认证 / API 契约破坏
      → 涉及则必须先验证下游消费者（y8ysystems 等机器对端）
- [ ] `docs/` 已同步更新（配置项变了要改 README §Configuration 和 `packaging/y8y-todo.env.example`）
- [ ] 生产数据已有可用备份（备份由生产侧负责，不在本仓库流程内）

### 4.3 部署

**部署不在本仓库进行。** 完整流程见 y8y 组织下的**私有运维仓库**
（公开仓库不引用私有仓库地址）。

部署方按私有运维系统的部署流程文档执行，核心要求：

1. 用**明确的 commit SHA**，不追 `main`
2. 部署前核对当前生产版本与目标版本的差异
3. 核对目标主机身份
4. 验证备份与回滚条件成立
5. **得到用户明确授权后**才修改生产环境

### 4.4 发布后验证

部署方在私有运维系统的验证清单上逐项确认：

- [ ] 服务 active，监听在预期地址
- [ ] 匿名访问被拒（401）
- [ ] UI 可达（200）
- [ ] 真实用户 API 返回正常数据
- [ ] 机器对端（y8ysystems）契约仍成立
- [ ] 数据文件数未变（换代码绝不能动数据）
- [ ] 日志无新错误

---

## 5. 回滚

回滚**不是**「切回旧 commit 就完事」。分两种情况：

### A 类：纯前端 / 不改变数据格式的代码更新

切回旧 commit + 重启 + 重新验证即可。因为旧代码能正常读新数据（数据格式没变）。

### B 类：改了任务存储 / 身份认证 / API 契约的重大更新

**切回旧 commit 不等于安全回滚。** 旧代码可能：
- 用文件名当标题（旧版没有独立 `title` 字段）→ 标题显示成 `<slug>-<8位uid>`
- 同名创建静默覆盖（旧版行为）
- 丢失 P1/P2 的安全修复（受信代理检查、原子写）

安全路径是 **fix-forward**，或 **冻结写入 + 恢复升级前备份 + 人工补录升级期新数据**。

完整分级、停止条件、回滚 kit 见 [`upgrade-runbook.md`](upgrade-runbook.md) §6。

---

## 6. 什么算「已发布」

| 状态 | 含义 |
|---|---|
| 开发完成 | 代码写完、测试过了 |
| PR 已开 | 等审 |
| CI 全绿 | 可以被 merge |
| 已 merge | `main` 前进，**仍可能没上线** |
| 已部署 | 生产跑的是那个 commit SHA |
| 已验证 | 部署后验证清单全过 |

只有最后两个才是「上线了」。
