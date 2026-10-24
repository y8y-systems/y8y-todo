#!/usr/bin/env bash
# p5-safe-restore.sh — 安全恢复正式数据目录 (Runbook §6.1 的可执行形式)
#
# 场景: 从切换前快照恢复 /var/lib/y8y-todo。旧 Runbook 在这里写过
# `sudo rm -rf /var/lib/y8y-todo` — 独立审查判定为误删除风险: 目标非空
# 时 (隔离实例遗留 / 上次切换产物 / 中断现场) 无条件删除可能毁掉唯一
# 副本。本脚本的纪律:
#
#   0. **先验证快照, 后动目标** (独立审查问题 B 修复): 归档存在 + 清单
#      存在 + 结构/文件集合/逐文件 SHA-256 全部通过 (p4-rehearsal.py
#      verify, 隔离临时目录只读完成) — 之后才允许改变恢复目标。损坏备份
#      或无效清单在这一步就被拒绝: 目标不会被移动、覆盖、删除, 也不会
#      产生不完整的替代目录。
#   1. 目标不存在或为空 → 直接恢复 (p4-rehearsal.py restore 自己也是
#      "先验证后写入, 拒绝覆盖非空目标")
#   2. 目标非空 → **先完整保全** (mv 成 <dest>-pre-restore-<TS>, 同文件
#      系统瞬时且可逆), 再恢复到新目录 — 任何情况下不 rm 数据
#   3. 已存在更早的保全副本 → **停止** (退出码 1), 打印现有副本, 要求
#      人工确认 — 防止重复执行把第二份数据也卷进去
#   4. 中断语义: mv 后 restore 前中断 → 目标不存在但保全副本与快照都
#      完好, 重跑走"目标不存在"分支; restore 后中断 → 目标已是校验过的
#      快照内容, 重跑走"非空→保全再恢复"分支。两种中断都不丢数据,
#      也都不需要从旧生产目录拷贝任何东西 (旧目录自始至终未被写过)。
#   5. O3-N3 起 restore 自身也 fail closed: staging 建在目标文件系统内
#      (跨文件系统恢复不再 Errno 18), 写入中途失败自动回滚。**回滚完整时**
#      目标逐字节回到恢复前状态; **回滚本身不完整时**工具输出 [FATAL]/
#      [WARN] 并保留现场 — 那不是"目标保持原状", 而是需要人工介入
#      (见下方三种失败语义, 不得夸大为所有故障都保证原路径不变)。
#
# 用法:
#   p5-safe-restore.sh <snapshot.tar.gz> <dest-dir> [--original <dir>]
#     --original <dir>  恢复后与该目录做只读一致性比对 (p5-verify-copy.py)
#
# 退出码: 0 = 恢复并校验完成; 1 = 拒绝执行/恢复失败 (含数据保全现场);
#         2 = 用法或前置条件错误。
# 失败语义 (退出码 1 的三种, 输出中明确区分):
#   a. restore 在既有目标上失败 (无保全副本) — 目标保持恢复前状态;
#   b. 旧目录已移到保全路径后新恢复失败 — 旧数据完好保留在保全副本;
#   c. 恢复后回滚本身不完整 — 需人工介入, 不得删除任何目录/现场。
set -euo pipefail

SNAPSHOT=""
DEST=""
ORIGINAL=""
while [ $# -gt 0 ]; do
  case "$1" in
    --original) ORIGINAL="$2"; shift 2;;
    -h|--help) sed -n '2,39p' "$0"; exit 0;;
    -*) echo "unknown option: $1"; exit 2;;
    *)
      if [ -z "$SNAPSHOT" ]; then SNAPSHOT="$1"
      elif [ -z "$DEST" ]; then DEST="$1"
      else echo "too many arguments"; exit 2; fi
      shift;;
  esac
done
[ -n "$SNAPSHOT" ] && [ -n "$DEST" ] || { echo "usage: $0 <snapshot.tar.gz> <dest-dir> [--original <dir>]"; exit 2; }

HERE="$(cd "$(dirname "$0")" && pwd)"
RESTORE_TOOL="$HERE/p4-rehearsal.py"

die() { echo "SAFE-RESTORE FAIL: $*"; exit 1; }
usage_err() { echo "SAFE-RESTORE usage: $*"; exit 2; }

# ── 前置条件 (全部只读) ──────────────────────────────────────────────
[ -f "$SNAPSHOT" ] || usage_err "快照不存在: $SNAPSHOT"
[ -f "$SNAPSHOT.sha256" ] || usage_err "缺少快照清单: $SNAPSHOT.sha256 (p4 备份格式)"
[ -f "$RESTORE_TOOL" ] || usage_err "找不到恢复工具: $RESTORE_TOOL"
command -v python3 >/dev/null 2>&1 || usage_err "python3 不可用"
DEST_PARENT="$(dirname "$DEST")"
[ -d "$DEST_PARENT" ] || usage_err "目标父目录不存在: $DEST_PARENT"
KEEP_GLOB="$DEST-pre-restore-*"

echo "== 安全恢复 (先验证快照, 再动目标; 任何情况下不删除数据) =="
echo "snapshot: $SNAPSHOT"
echo "target  : $DEST"

# ── 目标状态判定 (只读分类, 本阶段不做任何修改) ──────────────────────
if [ -L "$DEST" ]; then
  die "目标是符号链接: $DEST — 符号链接数据目录会造成 ID/队列 key 分歧 (P3 已修问题), 请人工处置"
fi
if [ ! -e "$DEST" ]; then
  echo "状态: 目标不存在 → 直接恢复到新目录"
elif [ -z "$(ls -A "$DEST")" ]; then
  echo "状态: 目标为空目录 → 直接恢复"
else
  # 非空: 保全优先。已有更早保全副本时停止 (重复执行保护)。
  # shellcheck disable=SC2086
  EXISTING="$(ls -d $KEEP_GLOB 2>/dev/null || true)"
  if [ -n "$EXISTING" ]; then
    echo "已存在更早的保全副本:"; echo "$EXISTING" | sed 's/^/  /'
    die "目标非空且已有保全副本 — 停止。人工确认保留哪一份、删除哪一份之后再继续 (Runbook §8.2 纪律: 不自动丢弃任何数据)。"
  fi
  echo "状态: 目标非空 ($(ls -A "$DEST" | wc -l) 个条目) → 快照验证通过后先保全再恢复"
fi

# ── 快照预验证 (独立审查问题 B: 必须在移动目标之前) ──────────────────
# 用隔离临时目录完成: 归档结构 + 文件集合 + 逐文件 SHA-256。失败即退出,
# 此刻目标目录尚未被移动/覆盖/创建 — 损坏备份碰不到任何已有数据。
if python3 "$RESTORE_TOOL" verify "$SNAPSHOT"; then
  echo "快照预验证通过 (归档结构 + 文件集合 + 逐文件 SHA-256)"
else
  rc=$?
  echo "SAFE-RESTORE FAIL: 快照预验证失败 (退出码 $rc) — 目标目录未被改动 (未移动/未覆盖/未创建)。"
  echo "处置: 修复或更换备份后重跑本脚本; 不要绕过预验证直接恢复。"
  exit 1
fi

# ── 保全 + 恢复 (仅当预验证通过) ─────────────────────────────────────
if [ -e "$DEST" ] && [ -n "$(ls -A "$DEST")" ]; then
  KEEP="$DEST-pre-restore-$(date +%Y%m%d-%H%M%S)"
  mv "$DEST" "$KEEP"
  echo "已保全: $KEEP (mv, 未删除任何内容)"
  mkdir -p "$DEST"
fi

# ── 恢复 (工具自身: 隔离解压→布局/集合/逐文件 sha256 全过才写入) ──────
# O3-N3 起 restore 还保证: staging 建在目标文件系统内 (跨文件系统可恢复),
# 写入中途失败自动回滚。回滚完整 → 目标回到恢复前状态; 回滚不完整 → 工具
# 输出 [FATAL]/[WARN] 并保留现场, 需人工介入。下方按工具输出区分三种
# 失败语义, 不混为一谈, 也不夸大保全范围。
RESTORE_LOG="$(mktemp)"
trap 'rm -f "$RESTORE_LOG"' EXIT
if python3 "$RESTORE_TOOL" restore "$SNAPSHOT" "$DEST" 2>&1 | tee "$RESTORE_LOG"; then
  echo "恢复完成: $DEST"
else
  rc=$?
  if grep -qE '回滚不完整|保留回滚现场|\[FATAL\] 回滚失败' "$RESTORE_LOG"; then
    # 语义 3 — 恢复后回滚本身不完整: 旧内容的唯一副本可能只在工具保留的
    # 现场里, 任何自动清理都可能毁掉它 — 停在这里, 人工处置。
    echo "SAFE-RESTORE FAIL: 恢复失败且回滚不完整 (退出码 $rc) — 需要人工介入, 不得删除任何目录。"
    echo "  - 工具保留的回滚现场 (路径见上方 [FATAL]/[WARN]) 含有未能自动还原的旧内容;"
    if [ -n "${KEEP:-}" ]; then
      echo "  - 本脚本的保全副本同样完好: $KEEP (mv, 未删除任何内容);"
    fi
    echo "  处置: 按 Runbook §8 人工恢复; 确认每一份旧内容都已取回后方可清理现场。"
  elif [ -n "${KEEP:-}" ]; then
    # 语义 2 — 旧目录已移到保全路径, 新恢复失败 (工具未留下部分写入:
    # 失败先于写入, 或已完整回滚 — 工具输出中无回滚不完整标记)。
    echo "SAFE-RESTORE FAIL: 恢复失败 (退出码 $rc) — 工具未留下部分写入 (失败先于写入或已完整回滚)。"
    echo "  - 旧数据完好保留在: $KEEP (mv, 未删除任何内容);"
    echo "  - 新目标 $DEST 处于恢复前状态 (空目录, 或写入前的内容)。"
    echo "  处置: 修复快照/清单后重跑 (会再次走'非空→保全'分支), 或按 Runbook §8 回滚。"
  else
    # 语义 1 — restore 自身在既有目标上失败, 本脚本未创建保全副本
    # (工具未留下部分写入: 失败先于写入, 或已完整回滚)。
    echo "SAFE-RESTORE FAIL: 恢复失败 (退出码 $rc) — 工具未留下部分写入 (失败先于写入或已完整回滚)。"
    echo "  - 恢复目标保持恢复前状态; 本脚本未创建任何保全副本。"
    echo "  处置: 检查快照/清单后重跑, 或按 Runbook §8 回滚。"
  fi
  exit 1
fi

# ── 可选: 与原件只读比对 (数量与匿名标识, 不输出真实文件名) ───────────
if [ -n "$ORIGINAL" ]; then
  [ -d "$ORIGINAL" ] || die "--original 目录不存在: $ORIGINAL"
  echo "== 恢复后一致性比对 (只读) =="
  python3 "$HERE/p5-verify-copy.py" "$ORIGINAL" "$DEST"
fi

echo "SAFE-RESTORE OK: $DEST"
exit 0
