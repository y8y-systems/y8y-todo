#!/usr/bin/env bash
# Root-only CI fixture: disposable users, directories and fictional data.
set -euo pipefail
repo=$(cd "$(dirname "$0")/.." && pwd)
fixture=$(mktemp -d /tmp/todo-notify-acl.XXXXXX)
trap 'rm -rf "$fixture"' EXIT
web=y8y
notify=y8y-notify
# Let the two fixture users traverse the temporary parent before setup.
chmod 0711 "$fixture"
install -d -o "$web" -g "$web" -m 0700 "$fixture/data"
runuser -u "$web" -- mkdir -p "$fixture/data/todo/old" "$fixture/data/recurring"
runuser -u "$web" -- sh -c 'printf "fictional\n" > "$1/todo/old/task.md"; printf "fictional\n" > "$1/recurring/task.md"; printf "fixture\n" > "$1/internal_tokens.json"; chmod 600 "$1/internal_tokens.json"' sh "$fixture/data"
install -d -o "$notify" -g "$notify" -m 2770 "$fixture/data/notify"
# Traverse the private data root, without allowing root listing.
setfacl -m u:y8y-notify:--x "$fixture/data"
find -P "$fixture/data/todo" "$fixture/data/recurring" -type d -exec setfacl -m u:y8y-notify:r-x,d:u:y8y-notify:r-x {} +
find -P "$fixture/data/todo" "$fixture/data/recurring" -type f -name '*.md' -exec setfacl -m u:y8y-notify:r-- {} +
runuser -u "$web" -- mkdir -p "$fixture/data/todo/new/deep"
install -d -m 0755 "$fixture/code"
install -m 0644 "$repo/src/taskstore.py" "$fixture/code/taskstore.py"
runuser -u "$web" -- python3 - "$fixture/code" "$fixture/data/todo/new/deep/task.md" <<'PY'
import sys
sys.path.insert(0, sys.argv[1])
from taskstore import atomic_write
atomic_write(sys.argv[2], 'first')
atomic_write(sys.argv[2], 'replacement')
PY
runuser -u "$notify" -- test -r "$fixture/data/todo/old/task.md"
runuser -u "$notify" -- test -r "$fixture/data/todo/new/deep/task.md"
runuser -u "$notify" -- test -r "$fixture/data/recurring/task.md"
if runuser -u "$notify" -- test -r "$fixture/data/internal_tokens.json"; then exit 1; fi
if runuser -u "$notify" -- test -w "$fixture/data"; then exit 1; fi
mkdir "$fixture/units"
cp "$repo/packaging/y8y-todo.service" "$repo/packaging/y8y-todo-notify.service" "$fixture/units/"
install -m 600 /dev/null "$fixture/notify.env"
python3 "$repo/scripts/check-notify-isolation.py" --unit-dir "$fixture/units" --env-file "$fixture/notify.env" --data-dir "$fixture/data"
# An unreadable nested task and an exposed token must each fail C5.
setfacl -m u:y8y-notify:--- "$fixture/data/todo/new/deep/task.md"
if python3 "$repo/scripts/check-notify-isolation.py" --unit-dir "$fixture/units" --env-file "$fixture/notify.env" --data-dir "$fixture/data" > "$fixture/result"; then exit 1; fi
grep -q 'FAIL.*C5' "$fixture/result"
setfacl -m u:y8y-notify:r-- "$fixture/data/todo/new/deep/task.md"
setfacl -m u:y8y-notify:r-- "$fixture/data/internal_tokens.json"
if python3 "$repo/scripts/check-notify-isolation.py" --unit-dir "$fixture/units" --env-file "$fixture/notify.env" --data-dir "$fixture/data" > "$fixture/result"; then exit 1; fi
grep -q 'FAIL.*C5' "$fixture/result"

# ── LINE 自助绑定: 权威绑定目录的跨 UID 最小权限演练 ──────────────────
# 修复前 Web 与 Webhook 共用 2770 目录, Web 可以 unlink / rename / 新建
# binding-*.json, 等于绕过 LINE Webhook 的签名验证伪造收件人。
# notify/ 的 sticky 位阻止 Web 把通知侧拥有的 line-link/ 整个改名。
notify_dir="$fixture/data/notify"
trusted="$notify_dir/line-link"
inbox="$notify_dir/line-link-inbox"
chmod 3770 "$notify_dir"
install -d -o "$notify" -g "$notify" -m 2750 "$trusted"
install -d -o "$notify" -g "$notify" -m 2770 "$inbox"

# 通知侧写一条权威绑定 / 一条解绑墓碑
runuser -u "$notify" -- sh -c '
  printf "%s\n" "{\"version\":1,\"user\":\"alice\",\"line_user_id\":\"Uaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\"}" > "$1/binding-x.json"
  printf "%s\n" "{\"version\":1,\"user\":\"bob\"}" > "$1/unlinked-x.json"
  chmod 644 "$1/binding-x.json" "$1/unlinked-x.json"
' sh "$trusted"

# Web 必须能读自己的绑定状态 (2750 给 group r-x)
runuser -u "$web" -- test -r "$trusted/binding-x.json"
runuser -u "$web" -- test -r "$trusted/unlinked-x.json"
# Web 绝不能在权威目录里新建 / 替换 / 删除 / 改名
if runuser -u "$web" -- sh -c 'printf "forged" > "$1/binding-forged.json"' sh "$trusted" 2>/dev/null; then
  echo "FAIL: Web 能在权威目录伪造 binding 文件"; exit 1
fi
# 把副本放进 Web 可写的 inbox, 才能真正测到「mv 覆盖权威文件」这一步
if runuser -u "$web" -- sh -c 'cp "$1/binding-x.json" "$2" && mv -f "$2" "$1/binding-x.json"' sh "$trusted" "$inbox/replacement" 2>/dev/null; then
  echo "FAIL: Web 能替换权威 binding 文件"; exit 1
fi
rm -f "$inbox/replacement"
if runuser -u "$web" -- rm -f "$trusted/binding-x.json" 2>/dev/null; then
  echo "FAIL: Web 能删除权威 binding 文件"; exit 1
fi
if runuser -u "$web" -- rm -f "$trusted/unlinked-x.json" 2>/dev/null; then
  echo "FAIL: Web 能删除解绑墓碑"; exit 1
fi
if runuser -u "$web" -- mv "$trusted" "$notify_dir/line-link-swapped" 2>/dev/null; then
  echo "FAIL: notify/ 缺 sticky, Web 能把权威目录整个挪走"; exit 1
fi
# 原文没被改动
grep -q 'alice' "$trusted/binding-x.json"
# Web 仍能在请求队列里创建绑定请求 / 解绑请求 / 测试通知请求
runuser -u "$web" -- sh -c '
  printf "%s\n" "{\"version\":1,\"user\":\"alice\"}" > "$1/code-x.json"
  printf "%s\n" "{\"version\":1,\"user\":\"alice\"}" > "$1/unlink-x.json"
  printf "%s\n" "{\"version\":1,\"id\":\"abc\"}" > "$1/test-abc.req.json"
' sh "$inbox"
runuser -u "$notify" -- test -r "$inbox/code-x.json"
runuser -u "$notify" -- test -r "$inbox/unlink-x.json"
# 通知侧能读/写/删 inbox (消费请求)
runuser -u "$notify" -- sh -c 'printf "ok" > "$1/result-x.json"; rm -f "$1/code-x.json"' sh "$inbox"
runuser -u "$web" -- test -r "$inbox/result-x.json"
echo "OK: LINE 绑定目录跨 UID 权限 (读允许 / 伪造·替换·删除·改名拒绝)"

# ── P0-2: 父目录属主 —— Linux sticky 不限制「目录所有者」 ─────────────
# 构造一个权限位**完全合规** (3770 / 2750 / 2770) 但 notify/ 归 Web 用户
# 所有的部署。若只检查 sticky 而不验属主, 这个错误配置会蒙混过关。
bad_data="$fixture/bad-data"
install -d -o "$web" -g "$notify" -m 0755 "$bad_data"
bad_notify="$bad_data/notify"
install -d -o "$web" -g "$notify" -m 3770 "$bad_notify"
install -d -o "$notify" -g "$notify" -m 2750 "$bad_notify/line-link"
install -d -o "$notify" -g "$notify" -m 2770 "$bad_notify/line-link-inbox"

# A/B: 同一个通知侧拥有的空权威目录, 父目录属主不同 → sticky 效果不同。
install -d -o "$notify" -g "$notify" -m 2750 "$notify_dir/line-link-empty"
install -d -o "$notify" -g "$notify" -m 2750 "$bad_notify/line-link-empty"
if runuser -u "$web" -- rmdir "$notify_dir/line-link-empty" 2>/dev/null; then
  echo "FAIL: 父目录归通知侧时 sticky 没挡住 Web 删权威目录"; exit 1
fi
if ! runuser -u "$web" -- rmdir "$bad_notify/line-link-empty" 2>/dev/null; then
  echo "FAIL: 没能复现 sticky 对目录所有者无效 (检查 rmdir 语义)"; exit 1
fi
# 复现攻击: 删掉通知侧目录后原地重建一个 Web 自己拥有的, 再伪造绑定文件。
runuser -u "$web" -- mkdir "$bad_notify/line-link-empty"
runuser -u "$web" -- sh -c 'printf "forged\n" > "$1/binding-forged.json"' \
  sh "$bad_notify/line-link-empty"
grep -q forged "$bad_notify/line-link-empty/binding-forged.json"
runuser -u "$web" -- test -w "$bad_notify/line-link-empty/binding-forged.json"
echo "OK: 父目录归 Web 时 sticky 不足以保护权威目录 (已实测可伪造绑定)"

# 产品自检必须 fail closed, 且失败原因明确指向 **父目录** 属主。
install -m 0644 "$repo/src/line_link.py" "$fixture/code/line_link.py"
if ! runuser -u "$notify" -- python3 -c '
import sys
sys.path.insert(0, sys.argv[1])
import line_link
problem = line_link.LinkStore(sys.argv[2]).verify_trusted_layout()
assert "notification state directory is owned by a different uid" in problem, problem
' "$fixture/code" "$bad_data"; then
  echo "FAIL: Webhook 启动自检没有拒绝 Web 拥有的 notify/ 父目录"; exit 1
fi
if ! runuser -u "$notify" -- python3 -c '
import sys
sys.path.insert(0, sys.argv[1])
import line_link
problem = line_link.LinkStore(sys.argv[2]).verify_trusted_layout()
assert problem == "", problem
' "$fixture/code" "$fixture/data"; then
  echo "FAIL: 合规布局被启动自检误拒"; exit 1
fi

# Operator 部署前置检查 (C8) 也必须抓到同一个错误配置。
python3 - "$repo/scripts" "$bad_notify" "$notify" "$web" <<'PY'
import importlib.util
import pwd
import sys
spec = importlib.util.spec_from_file_location(
    'checker', sys.argv[1] + '/check-notify-isolation.py')
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
nuid = pwd.getpwnam(sys.argv[3]).pw_uid
wuid = pwd.getpwnam(sys.argv[4]).pw_uid
problems = mod._line_link_layout_problems(sys.argv[2], nuid, wuid)
assert any('notify' in p and 'Web' in p for p in problems), problems
PY
echo "OK: 父目录属主检查 (启动自检 fail closed + Operator 前置检查 C8)"

# 合规布局跑一次完整的前置检查 (先把 C5 用过的 ACL 复原)。
setfacl -m u:y8y-notify:--- "$fixture/data/internal_tokens.json"
if ! python3 "$repo/scripts/check-notify-isolation.py" \
      --unit-dir "$fixture/units" --env-file "$fixture/notify.env" \
      --data-dir "$fixture/data" --line-link-layout > "$fixture/c8-check"; then
  echo "FAIL: 合规布局没通过带 C8 的 Operator 前置检查"
  cat "$fixture/c8-check"
  exit 1
fi
grep -q 'C8 LINE 绑定目录属主/权限' "$fixture/c8-check"
echo "OK: Operator 前置检查 --line-link-layout 在合规部署上通过"
