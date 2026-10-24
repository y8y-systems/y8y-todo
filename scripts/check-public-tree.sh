#!/usr/bin/env bash
# check-public-tree.sh — 公开树隐私门（入口 wrapper）
#
# 实现在 scripts/check-public-tree.py（纯标准库）。为什么不是纯 shell:
#
#   · 文件名可以含冒号 / 换行 / 任意字符 —— shell 里按 "file:line:content"
#     切必然切错（冒号歧义、换行断行），二进制文件还会输出
#     "Binary file X matches" 把解析彻底搞乱
#   · shell 里 `hit` 写在 $(...) 子 shell 里会**丢掉** fail=1 ——
#     扫描命令报错时门会误报 CLEAN，Fail Closed 形同虚设
#   Python 里没有子 shell，错误状态不会丢；文件名也拿得到原样，无需猜分隔符。
#
# 用法: bash scripts/check-public-tree.sh
# 退出码: 0 = 干净; 1 = 有发现 / 扫描失败
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"

if ! command -v python3 >/dev/null 2>&1; then
  echo "  FAIL 找不到 python3 — 门无法判定, 按 Fail Closed 处理" >&2
  exit 1
fi

exec python3 "$HERE/check-public-tree.py" "$@"
