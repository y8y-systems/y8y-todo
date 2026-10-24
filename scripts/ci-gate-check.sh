#!/usr/bin/env bash
# ci-gate-check.sh — CI Gate 的判定逻辑 (四个 workflow 共用一份)
#
# 用途: 被各 workflow 的 `gate / *` job 调用, 判断上游测试是否全部成功。
#
# ⚠️ 为什么需要这个脚本 (而不是直接用 if: 表达式):
#
#   GitHub 的 Required Status Checks 会把 **skipped 视为「已满足」**。
#   所以如果汇总 job 因为上游失败而被 skip, 门就形同虚设 ——
#   上游红了, 但 required check 显示通过, PR 照样能合。
#
#   正确做法 (本项目采用):
#     1. 汇总 job 用 `if: always()` → 上游失败时它**仍然会跑**
#     2. 汇总 job 显式检查每个 `needs.<job>.result`
#     3. 任何非 success (failure / cancelled / skipped) → 汇总 job **失败**
#        (必须是 failure, 不能是 skipped —— skipped 会被当成满足门)
#
# 输入 (环境变量):
#   CI_GATE_UPSTREAM  必填。空格分隔的 "name=result" 列表, 例如
#                     "pytest310=success systemd249=failure"
#
# 退出码:
#   0 = 全部上游成功
#   1 = 有上游不是 success (门不通过)
#   2 = 用法错误 (CI_GATE_UPSTREAM 缺失或格式不对)
set -uo pipefail

if [ -z "${CI_GATE_UPSTREAM:-}" ]; then
    echo "::error::CI Gate: CI_GATE_UPSTREAM 未设置 — 门无法判定 (Fail Closed)"
    exit 2
fi

echo "== CI Gate: upstream results =="
failed=0
count=0

# 用 read 逐个读, 避免依赖 word splitting 的边界行为
while read -r name result; do
    [ -z "${name:-}" ] && continue
    count=$((count + 1))
    if [ "$result" = "success" ]; then
        echo "  OK    ${name}: ${result}"
    else
        # failure / cancelled / skipped 一律视为不通过
        echo "  FAIL  ${name}: ${result}"
        failed=1
    fi
done <<EOF
$(printf '%s\n' "$CI_GATE_UPSTREAM" | tr ' ' '\n' | sed 's/=/ /')
EOF

if [ "$count" -eq 0 ]; then
    echo "::error::CI Gate: 没有解析到任何上游结果 — 门无法判定 (Fail Closed)"
    exit 2
fi

echo
if [ "$failed" -ne 0 ]; then
    echo "::error::CI Gate failed — ${count} 个上游 job 中有未成功者"
    exit 1
fi

echo "::notice::CI Gate passed — ${count} 个上游 job 全部成功"
exit 0
