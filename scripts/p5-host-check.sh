#!/usr/bin/env bash
# p5-host-check.sh — 目标主机身份硬核对 (P5.5, 供 Hermes P6 使用)
#
# 背景: Hermes 曾在错误主机上执行过危险命令。因此本项目的 Runbook 规定:
# 任何「停止服务 / 修改配置 / 复制或删除数据」步骤之前, 必须先运行本脚本
# 并通过。它是只读的 — 只做检查, 没有任何副作用; 任一条件不满足即以
# 非零码退出并明确说明缺什么。
#
# 独立审查加固 (2026-10): **检查无法执行 = 失败, 不是跳过**。缺少
# systemctl / ss / /proc 不可读 / 预期状态不符 — 一律 FAIL。静默跳过等于
# 把"没验证"伪装成"验证通过"。
#
# 阶段模型 (2026-10 独立审查修正: 不同生产阶段的"正确状态"不同, 脚本必须
# 按阶段判断, 不能用同一把尺子挡住合法的部署/回滚动作):
#
#   pre-deploy   部署准备 (Runbook §1/§3/§4): 旧服务**正在运行**, 8086 的
#                监听者必须确实是旧服务进程 (cmdline 含 serve.py + 用户
#                匹配 + 归属旧代码目录或其符号链接别名), 新版单元尚未安装。
#   pre-cutover  切换前冻结 (Runbook §4 收尾, §2 冻结顺序**开始之前**):
#                旧服务仍在运行 + 8086 归属旧服务 + 隔离实例已在 18086
#                运行 + 隔离验证证据就绪 + 新旧数据目录物理隔离。
#   cutover      停旧服后的正式切换 (Runbook §5/§6, §2 冻结**已完成**):
#                旧服务**已停止**, 8086 **已释放**, 旧数据目录仍在, 隔离
#                验证证据就绪, 数据目录隔离 — 不错误要求旧服务继续运行。
#   pre-rollback 回滚准备 (Runbook §8): 旧服务运行与否**都不是错误** (允许
#                已停/正在恢复); 8086 可释放、可被 todo 服务进程 (新版)
#                占用 (仅提示), 但被**非 todo 进程**占用必须 FAIL; 新旧
#                数据目录必须隔离; 新版数据目录若存在仅提示保留, 不删除。
#
# 脱敏 (独立审查要求): 失败信息**不输出完整进程命令行** — 其中可能含环境
# 参数或敏感信息。只输出 pid / user / cmdline 的不可逆摘要 (sha256[:8]),
# 需要完整信息时由操作员用 ss -tlnpH / journalctl 自行核对。
#
# 用法:
#   p5-host-check.sh --phase pre-deploy      # 部署准备 (旧版在跑, 新版未装)
#   p5-host-check.sh --phase pre-cutover     # 冻结前最后闸门 (旧版仍在跑)
#   p5-host-check.sh --phase cutover         # 停旧服后, 正式切换数据前
#   p5-host-check.sh --phase pre-rollback    # 回滚准备 (新旧状态不拘)
#
# 环境变量 (都有默认值, 覆盖用于隔离演练):
#   Y8Y_EXPECT_HOSTS      逗号分隔的期望主机名 (默认 todo-host)
#   Y8Y_OLD_CODE_DIR      旧版代码目录 (默认 /opt/todolist-legacy)
#   Y8Y_OLD_CODE_ALIASES  旧代码目录的符号链接别名, 逗号分隔
#                         (默认 /var/www/todolist-legacy)
#   Y8Y_OLD_DATA_DIR      旧版数据目录 (默认 /var/lib/todolist-legacy)
#   Y8Y_OLD_UNIT          旧服务单元名 (默认 todolist-web.service)
#   Y8Y_OLD_UNIT_FILE     旧单元文件路径 (默认按系统位置自动探测)
#   Y8Y_OLD_SERVICE_USER  旧服务预期运行用户 (默认 root)
#   Y8Y_OLD_PORT          旧服务端口 (默认 8086)
#   Y8Y_STAGING_PORT      新版隔离实例端口 (默认 18086)
#   Y8Y_STAGING_DATA_DIR  隔离实例数据目录 (默认 /var/lib/y8y-todo-staging;
#                         切换阶段以它非空作为"§4 隔离验证已执行"的证据)
#   Y8Y_NEW_UNIT          新版服务单元名 (默认 y8y-todo.service)
#   Y8Y_NEW_UNIT_FILE     新版单元文件路径 (默认 /etc/systemd/system/<unit>)
#   Y8Y_NEW_DATA_DIR      新版数据目录 (默认 /var/lib/y8y-todo)
#
# 演练 (CI/本地): 在隔离目录里伪造上述路径 + PATH 里的假 systemctl/ss
# 即可复现全部判定逻辑 (见 tests/test_p5_tooling.py, 含完整
# 切换/回滚状态机序列测试)。
set -euo pipefail

EXPECT_HOSTS="${Y8Y_EXPECT_HOSTS:-todo-host}"
OLD_CODE_DIR="${Y8Y_OLD_CODE_DIR:-/opt/todolist-legacy}"
OLD_CODE_ALIASES="${Y8Y_OLD_CODE_ALIASES:-/var/www/todolist-legacy}"
OLD_DATA_DIR="${Y8Y_OLD_DATA_DIR:-/var/lib/todolist-legacy}"
OLD_UNIT="${Y8Y_OLD_UNIT:-todolist-web.service}"
OLD_UNIT_FILE="${Y8Y_OLD_UNIT_FILE:-}"
OLD_SERVICE_USER="${Y8Y_OLD_SERVICE_USER:-root}"
OLD_PORT="${Y8Y_OLD_PORT:-8086}"
STAGING_PORT="${Y8Y_STAGING_PORT:-18086}"
STAGING_DATA_DIR="${Y8Y_STAGING_DATA_DIR:-/var/lib/y8y-todo-staging}"
NEW_UNIT="${Y8Y_NEW_UNIT:-y8y-todo.service}"
NEW_UNIT_FILE="${Y8Y_NEW_UNIT_FILE:-/etc/systemd/system/$NEW_UNIT}"
NEW_DATA_DIR="${Y8Y_NEW_DATA_DIR:-/var/lib/y8y-todo}"
PHASE=""

while [ $# -gt 0 ]; do
  case "$1" in
    --phase) PHASE="$2"; shift 2;;
    *) echo "usage: $0 --phase {pre-deploy|pre-cutover|cutover|pre-rollback}"; exit 2;;
  esac
done
case "$PHASE" in
  pre-deploy|pre-cutover|cutover|pre-rollback) ;;
  *) echo "usage: $0 --phase {pre-deploy|pre-cutover|cutover|pre-rollback}"; exit 2;;
esac

fail() { echo "HOST-CHECK FAIL [$PHASE]: $*"; exit 1; }
pass() { echo "HOST-CHECK PASS [$PHASE]: $*"; }
note() { echo "note [$PHASE]: $*"; }

echo "== 目标主机身份核对 (只读, 无副作用) =="
echo "phase      : $PHASE"
echo "hostname   : $(hostname)"
echo "os         : $(. /etc/os-release 2>/dev/null && echo "$PRETTY_NAME" || uname -s)"
echo "old code   : $OLD_CODE_DIR (aliases: $OLD_CODE_ALIASES)"
echo "old data   : $OLD_DATA_DIR"
echo "old service: $OLD_UNIT (port $OLD_PORT, user $OLD_SERVICE_USER)"

# ── 工具可用性: 无法执行 = 失败 (不静默跳过) ──────────────────────────
command -v systemctl >/dev/null 2>&1 \
  || fail "systemctl 不可用 — 无法验证服务状态。按'无法执行即失败'原则停止; 若在隔离环境演练, 请提供可用的 systemctl 或假实现"
command -v ss >/dev/null 2>&1 \
  || fail "ss 不可用 — 无法核对端口监听者身份; 停止 (iproute2 未安装?)"

# 1. 主机名 — 第一道防线, 错主机在这里就必须停下
HOST="$(hostname)"
case ",$EXPECT_HOSTS," in
  *",$HOST,"*) pass "hostname '$HOST' 在期望列表 ($EXPECT_HOSTS)";;
  *) fail "hostname '$HOST' 不在期望列表 ($EXPECT_HOSTS) — 停! 确认没有连错机器";;
esac

# 2. 旧版痕迹必须都在 (证明这是那台跑了多年的生产机, 不是新机器/开发机)
[ -d "$OLD_CODE_DIR" ] || fail "旧版代码目录不存在: $OLD_CODE_DIR"
pass "旧版代码目录存在"
[ -d "$OLD_DATA_DIR" ] || fail "旧版数据目录不存在: $OLD_DATA_DIR"
pass "旧版数据目录存在"
[ -f "$OLD_DATA_DIR/internal_tokens.json" ] \
  || fail "旧版数据目录缺少 internal_tokens.json — 不像生产数据目录"
pass "旧版数据目录形态正确 (internal_tokens.json 在)"
[ "$(find "$OLD_DATA_DIR" -name '*.md' | wc -l)" -gt 0 ] \
  || fail "旧版数据目录没有任何 .md — 不像生产数据目录"
pass "旧版数据目录含任务文件"
# 旧单元文件: 统一使用开头定义的内部变量 OLD_UNIT_FILE (其本身带 :-
# 默认值, set -u 下安全)。曾直接引用裸环境变量 $Y8Y_OLD_UNIT_FILE —
# 生产 VPS 不设置该变量时脚本以 unbound variable 崩溃 (P6-A 实测)。
# 显式指定与自动探测两条路都过同一道"文件必须存在"闸门 (fail-closed)。
OLD_UNIT_FOUND="$OLD_UNIT_FILE"
if [ -z "$OLD_UNIT_FOUND" ]; then
  for cand in "/etc/systemd/system/$OLD_UNIT" "/lib/systemd/system/$OLD_UNIT" \
              "/usr/lib/systemd/system/$OLD_UNIT"; do
    [ -f "$cand" ] && OLD_UNIT_FOUND="$cand" && break
  done
fi
[ -n "$OLD_UNIT_FOUND" ] \
  || fail "旧服务单元不存在: $OLD_UNIT (系统位置自动探测未找到; 可用 Y8Y_OLD_UNIT_FILE 显式指定)"
[ -f "$OLD_UNIT_FOUND" ] \
  || fail "旧服务单元文件不存在: $OLD_UNIT_FOUND — 显式指定的路径没有文件 (请修正 Y8Y_OLD_UNIT_FILE)"
pass "旧服务单元存在 ($OLD_UNIT_FOUND)"

# ── 进程身份辅助 (可移植: /proc 优先, ps 兜底; 两者都拿不到 = 失败) ────
proc_cmdline() {
  if [ -r "/proc/$1/cmdline" ]; then
    tr '\0' ' ' < "/proc/$1/cmdline" 2>/dev/null && return 0
  fi
  ps -o command= -p "$1" 2>/dev/null | sed 's/[[:space:]]*$//' && return 0
  return 1
}
proc_cwd() {
  if [ -e "/proc/$1/cwd" ]; then
    readlink -f "/proc/$1/cwd" 2>/dev/null && return 0
  fi
  if command -v lsof >/dev/null 2>&1; then
    lsof -a -p "$1" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p' | head -1 && return 0
  fi
  return 1
}
hash8() {
  # 字符串的不可逆摘要 (跨次运行可比对同一进程, 但不泄露内容)。
  # sha256sum (GNU) / shasum (BSD/macOS) 任一可用即可; 都没有时明确标注,
  # 不影响判定本身 (只影响失败信息里的相关性 id)。
  if command -v sha256sum >/dev/null 2>&1; then
    printf '%s' "$1" | sha256sum | cut -c1-8
  elif command -v shasum >/dev/null 2>&1; then
    printf '%s' "$1" | shasum -a 256 | cut -c1-8
  else
    echo "unavailable"
  fi
}
proc_brief() {
  # 脱敏进程摘要: pid / user / cmdline 摘要。**绝不输出完整命令行** —
  # 其中可能含环境参数或敏感信息 (独立审查要求)。
  local pid="$1" user cmd
  user="$(ps -o user= -p "$pid" 2>/dev/null | tr -d ' ' || true)"
  cmd="$(proc_cmdline "$pid" 2>/dev/null || true)"
  echo "pid=$pid user=${user:-unknown} cmdline-id=$(hash8 "$cmd")"
}
port_listener_pid() {
  ss -tlnpH "sport = :$1" 2>/dev/null \
    | grep -o 'pid=[0-9]*' | head -1 | cut -d= -f2 || true
}
listener_is_todo_process() {
  local cmd
  cmd="$(proc_cmdline "$1" 2>/dev/null || true)"
  case "$cmd" in *serve.py*) return 0;; *) return 1;; esac
}

require_old_service_running() {
  if systemctl is-active --quiet "$OLD_UNIT"; then
    pass "旧服务 $OLD_UNIT 正在运行"
  else
    fail "旧服务 $OLD_UNIT 未运行 — 本阶段要求它在跑 (Restart=always 的 unit 也不会自己起, 先查原因)"
  fi
}
require_old_service_stopped() {
  if systemctl is-active --quiet "$OLD_UNIT"; then
    fail "旧服务 $OLD_UNIT 仍在运行 — 正式切换数据步骤前必须先按 Runbook §2 停止并确认 8086 释放"
  else
    pass "旧服务 $OLD_UNIT 已停止 (符合切换前冻结预期)"
  fi
}
check_old_port_listener_strict() {
  # 端口监听者身份: 存在 + 进程确实是旧服务 (不是别的程序占坑)。
  # 失败输出脱敏 (不打印完整 cmdline)。
  local listener srv_user cmdline
  listener="$(port_listener_pid "$OLD_PORT")"
  [ -n "$listener" ] || fail "端口 $OLD_PORT 无监听者 — 旧服务声称 active 但没有监听; 停"
  kill -0 "$listener" 2>/dev/null || fail "监听进程 $listener 已不存在"
  pass "端口 $OLD_PORT 有监听者 (pid=$listener)"
  srv_user="$(ps -o user= -p "$listener" | tr -d ' ')"
  cmdline="$(proc_cmdline "$listener" || true)"
  [ -n "$cmdline" ] || fail "无法读取监听进程 cmdline (pid=$listener) — 无法确认进程身份; 停"
  case "$cmdline" in
    *serve.py*) pass "监听进程命令行含 serve.py";;
    *) fail "端口 $OLD_PORT 的监听者不是 todo 服务 (cmdline 不含 serve.py): $(proc_brief "$listener") — 完整命令行已脱敏; 请用 ss -tlnpH 人工核对";;
  esac
  [ "$srv_user" = "$OLD_SERVICE_USER" ] \
    || fail "监听进程用户是 $srv_user, 预期 $OLD_SERVICE_USER — 与 P5 调查不符; 停"
  pass "监听进程用户 = $OLD_SERVICE_USER"
  # 工作目录/命令行必须落在旧代码目录 (或其符号链接别名) 下
  local old_code_real cwd_real dir_ok=0 dir_how='' al al_real
  old_code_real="$(readlink -f "$OLD_CODE_DIR" 2>/dev/null || echo "$OLD_CODE_DIR")"
  cwd_real="$(proc_cwd "$listener" || true)"
  if [ -n "$cwd_real" ] && [ "$cwd_real" = "$old_code_real" ]; then
    dir_ok=1
    dir_how="cwd=$cwd_real"
  fi
  if [ "$dir_ok" = "0" ]; then
    IFS=',' read -ra _aliases <<< "$OLD_CODE_ALIASES"
    for al in "${_aliases[@]}"; do
      al="${al// /}"
      [ -n "$al" ] || continue
      al_real="$(readlink -f "$al" 2>/dev/null || echo "$al")"
      if [ "$al_real" = "$old_code_real" ] && [[ "$cmdline" == *"$al"* ]]; then
        dir_ok=1
        dir_how="cmdline 含别名 $al"
      fi
    done
  fi
  [ "$dir_ok" = "1" ] \
    || fail "监听进程的工作目录/命令行不指向旧代码目录 ($OLD_CODE_DIR, aliases $OLD_CODE_ALIASES): cwd='$cwd_real' $(proc_brief "$listener") — 完整命令行已脱敏"
  pass "监听进程归属旧代码目录 ($dir_how)"
}
require_port_released() {
  local listener
  listener="$(port_listener_pid "$OLD_PORT")"
  if [ -z "$listener" ]; then
    pass "端口 $OLD_PORT 已释放 (旧服务停止后的正确状态)"
  else
    fail "端口 $OLD_PORT 仍被占用: $(proc_brief "$listener") — 正式切换前必须释放; 完整命令行已脱敏"
  fi
}
classify_port_for_rollback() {
  # 回滚阶段: 端口占用要查, 但判定标准不同 — 被 todo 服务进程 (通常是
  # 待回滚的新版) 占用不阻止回滚 (Runbook 会先停它); 只有非 todo 进程
  # 占坑才必须停下人工处理。
  local listener
  listener="$(port_listener_pid "$OLD_PORT")"
  if [ -z "$listener" ]; then
    pass "端口 $OLD_PORT 已释放 — 旧版恢复后可直接绑定"
    return
  fi
  kill -0 "$listener" 2>/dev/null || fail "监听进程 $listener 已不存在 (端口 $OLD_PORT) — 回滚前先查清"
  if listener_is_todo_process "$listener"; then
    note "端口 $OLD_PORT 由 todo 服务进程占用 ($(proc_brief "$listener")) — 回滚流程会先停新版再确认释放; 不视为错误"
  else
    fail "端口 $OLD_PORT 被非 todo 进程占用: $(proc_brief "$listener") — 回滚前必须查清占用者; 完整命令行已脱敏"
  fi
}
require_staging_running() {
  local stage_pid
  stage_pid="$(port_listener_pid "$STAGING_PORT")"
  [ -n "$stage_pid" ] || fail "隔离实例端口 $STAGING_PORT 无监听 — 必须先完成 Runbook §4 隔离验证"
  pass "隔离实例在 $STAGING_PORT 运行 (pid=$stage_pid)"
}
require_staging_evidence() {
  # "§4 隔离验证已执行"的可检查证据: 隔离数据目录存在且含任务文件
  # (只读副本 + p5-verify-copy 的验证对象就是它)。
  [ -d "$STAGING_DATA_DIR" ] \
    || fail "隔离验证证据缺失: $STAGING_DATA_DIR 不存在 — 切换前必须先完成 Runbook §4 (只读副本 + 隔离实例验证)"
  [ "$(find "$STAGING_DATA_DIR" -name '*.md' | wc -l)" -gt 0 ] \
    || fail "隔离验证证据缺失: $STAGING_DATA_DIR 没有任何 .md — 不像经过验证的数据副本"
  pass "隔离验证证据就绪 ($STAGING_DATA_DIR 含任务文件)"
}
require_data_dir_isolation() {
  # 铁律: 两个数据目录不能是同一个 (符号链接/绑定挂载都要防)
  local old_real new_real
  old_real="$(readlink -f "$OLD_DATA_DIR")"
  new_real="$(readlink -f "$NEW_DATA_DIR")"
  [ "$old_real" != "$new_real" ] || fail "新旧数据目录解析到同一路径 — 严重!"
  pass "新旧数据目录物理隔离 ($old_real vs $new_real)"
}
note_old_service_state() {
  # 回滚阶段: 旧服务运行与否都不是错误 — 只报告事实。
  if systemctl is-active --quiet "$OLD_UNIT"; then
    note "旧服务 $OLD_UNIT 当前仍在运行 (回滚准备不要求其状态; 若已恢复请核对数据)"
  else
    note "旧服务 $OLD_UNIT 未运行 — 回滚阶段允许, 不视为错误"
  fi
}

# 3./4. 分阶段断言 (不同阶段的"正确状态"不同 — 见文件头阶段模型)
case "$PHASE" in
  pre-deploy)
    require_old_service_running
    check_old_port_listener_strict
    if [ -f "$NEW_UNIT_FILE" ]; then
      fail "新版单元 $NEW_UNIT_FILE 已存在 — P6 第一阶段不应触碰它; 若为重复执行请人工确认"
    fi
    pass "新版单元尚未安装 (符合 P6 第一阶段预期)"
    ;;
  pre-cutover)
    # Runbook §2 冻结顺序开始之前的最后一道闸门 — 此刻旧服务必须仍在跑。
    require_old_service_running
    check_old_port_listener_strict
    require_staging_running
    require_staging_evidence
    require_data_dir_isolation
    ;;
  cutover)
    # Runbook §5/§6: 冻结已完成 (旧服已停, 8086 已释放) 之后的切换闸门。
    # 不要求旧服务继续运行; 要求隔离验证证据 + 端口释放 + 目录隔离。
    require_old_service_stopped
    require_port_released
    require_staging_evidence
    require_data_dir_isolation
    ;;
  pre-rollback)
    # Runbook §8: 真正出故障时的回滚闸门 — 标准是"能安全回滚", 不是
    # "世界必须完好"。旧服务状态只报告; 新版占端口只提示; 外来占坑才停。
    note_old_service_state
    classify_port_for_rollback
    require_data_dir_isolation
    if [ -d "$NEW_DATA_DIR" ]; then
      note "新版数据目录 $NEW_DATA_DIR 仍在 — 回滚后保留供分析, 不得删除 (Runbook §8)"
    fi
    ;;
esac

pass "全部条件满足 — 可以继续本阶段步骤"
exit 0
