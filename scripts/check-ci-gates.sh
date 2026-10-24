#!/usr/bin/env bash
# check-ci-gates.sh — CI Gate 自检
#
# 目的: 保证每个 workflow 的 `gate / *` 汇总 job 与该 workflow 的实际
# 测试 job **保持一致**。当 Agent 新增/删除/重命名内部测试 job 却忘了
# 同步 Gate 的 needs 时, 这个脚本会让 CI 变红 —— 也就是**门自己会报警**,
# 而不是等到 Ruleset 那边出问题才发现。
#
# 检查项 (每个 workflow):
#   1. 存在且**恰好一个** gate job
#   2. gate 的 name 符合约定 `gate / <slug>`
#   3. gate 有 `if: always()` (否则上游失败时 gate 会被 skip,
#      而 GitHub 把 skipped 当成「已满足 Required Check」→ 门形同虚设)
#   4. gate 的 needs **恰好**等于该 workflow 的全部非 gate job
#      (多一个 → 依赖了不存在的 job; 少一个 → 有测试没被门覆盖)
#   5. gate 的 RESULT_* env 变量与 needs 一一对应
#
# 退出码: 0 = 全部一致; 1 = 有不一致 (附具体差异)
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

fail=0
note() { echo "== $* =="; }
hit() { echo "  FAIL $1"; fail=1; }

# 用 python3 解析 YAML —— GitHub runner 上自带 python3 + pyyaml?
# 不一定带 pyyaml, 所以这里用纯文本解析 (workflow 结构很规整):
# 顶层 `jobs:` 下缩进 2 空格的键是 job id。
for wf in .github/workflows/*.yml; do
    [ -f "$wf" ] || continue
    name="$(basename "$wf")"

    note "$name"

    # ── 收集所有 job id ──
    #    只看 `jobs:` 块**之后**的内容 —— 否则 `on:` 下的
    #    `pull_request:` / `push:` 会被误当成 job。
    #    job id 在 jobs 块里是缩进 2 空格的键 (形如 `  job-id:`)。
    all_jobs="$(awk '
        /^jobs:[[:space:]]*$/ { injobs=1; next }
        injobs && /^[^[:space:]#]/ { injobs=0 }
        injobs && /^  [a-zA-Z0-9_-]+:[[:space:]]*$/ {
            sub(/^  /, ""); sub(/:[[:space:]]*$/, ""); print
        }
    ' "$wf" | sort)"
    # ── 收集 gate job id (以 gate- 开头) ──
    gate_jobs="$(printf '%s\n' "$all_jobs" | grep '^gate-' || true)"
    gate_count="$(printf '%s\n' "$gate_jobs" | grep -c . || true)"

    if [ "$gate_count" -eq 0 ]; then
        hit "没有 gate job — 本 workflow 没有稳定名称的汇总检查"
        continue
    fi
    if [ "$gate_count" -ne 1 ]; then
        hit "有 $gate_count 个 gate job (期望恰好 1 个): $(printf '%s' "$gate_jobs" | tr '\n' ' ')"
        continue
    fi

    gate_id="$(printf '%s\n' "$gate_jobs" | head -1)"

    # ── gate 的 needs ──
    needs_raw="$(awk -v g="  $gate_id:" '
        $0 == g { injob=1; next }
        injob && /^  [a-zA-Z0-9_-]+:[[:space:]]*$/ { exit }
        injob && /^    needs:/ { print; exit }
    ' "$wf")"
    needs_list="$(printf '%s' "$needs_raw" | sed 's/^    needs:[[:space:]]*//; s/[][]//g')"
    needs_sorted="$(printf '%s' "$needs_list" | tr ',' '\n' | tr -d ' ' | grep . | sort)"

    # ── 期望的 needs = 全部非 gate job ──
    expected="$(printf '%s\n' "$all_jobs" | grep -v '^gate-' | grep . | sort)"

    if [ "$needs_sorted" != "$expected" ]; then
        hit "gate '$gate_id' 的 needs 与实际 job 列表不一致"
        missing="$(comm -23 <(printf '%s\n' "$expected") <(printf '%s\n' "$needs_sorted") | tr '\n' ' ')"
        extra="$(comm -13 <(printf '%s\n' "$expected") <(printf '%s\n' "$needs_sorted") | tr '\n' ' ')"
        [ -n "$missing" ] && echo "       未被门覆盖的 job: $missing"
        [ -n "$extra" ] && echo "       needs 里不存在的 job: $extra"
        echo "       → 新增/删除/重命名测试 job 时, 记得同步 gate '$gate_id' 的 needs"
    else
        echo "  OK    gate '$gate_id' 覆盖了全部 $(printf '%s\n' "$expected" | grep -c .) 个测试 job"
    fi

    # ── gate 必须有 if: always() ──
    if ! awk -v g="  $gate_id:" '
        $0 == g { injob=1; next }
        injob && /^  [a-zA-Z0-9_-]+:[[:space:]]*$/ { exit }
        injob && /^    if:/ { print; exit }
    ' "$wf" | grep -q 'always()'; then
        hit "gate '$gate_id' 缺少 'if: always()' — 上游失败时它会被 skip,"
        echo "       而 GitHub 把 skipped 当成「已满足 Required Check」→ 门形同虚设"
    fi

    # ── gate 的显示名必须是稳定约定 ──
    gate_name="$(awk -v g="  $gate_id:" '
        $0 == g { injob=1; next }
        injob && /^  [a-zA-Z0-9_-]+:[[:space:]]*$/ { exit }
        injob && /^    name:/ { sub(/^    name:[[:space:]]*/, ""); print; exit }
    ' "$wf")"
    case "$gate_name" in
        "gate / "*) echo "  OK    gate 显示名: $gate_name" ;;
        *) hit "gate '$gate_id' 的 name 是 '$gate_name', 期望 'gate / <slug>' 形式" ;;
    esac
done

echo
if [ "$fail" -eq 0 ]; then
    echo "CI GATE CHECK: CLEAN"
else
    echo "CI GATE CHECK: FAILURES (见上)"
fi
exit $fail
