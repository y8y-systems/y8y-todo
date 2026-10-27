#!/usr/bin/env bash
# check-notify-isolation.sh — 通知凭据隔离审计（入口 wrapper）
#
# 实现在 scripts/check-notify-isolation.py（纯标准库）。为什么不是纯 shell:
# 需要读 /proc/<pid>/status、以别的用户身份做一次真实读取、并把"失败"
# 和"无法验证"分开 —— shell 里做这些容易把状态搞丢。
#
# ⚠️ 只读: 不建用户、不改权限、不启停服务、不读凭据内容。
#
# 用法: bash scripts/check-notify-isolation.sh [选项]
# 退出码: 0 = 全部通过; 1 = 有失败项; 2 = 有无法验证项
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"

if ! command -v python3 >/dev/null 2>&1; then
  echo "  FAIL 找不到 python3 — 无法判定, 按 Fail Closed 处理" >&2
  exit 2
fi

exec python3 "$HERE/check-notify-isolation.py" "$@"
