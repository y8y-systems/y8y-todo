#!/bin/bash
# P4 演练 CI 专用 — 服务生命周期助手 (仅 GitHub Actions runner 使用)
#
# 背景 (独立审查问题 B): 演练曾出现"第二次启动失败 (端口被旧进程占用),
# 但遗留进程仍在响应 HTTP, 健康检查误判回滚成功"的假阳性路径。
# 这两个函数把每个阶段的服务生命周期变成硬断言:
#   p4_stop_service   停服务 + 等 HTTP 静默 + 等端口真正无 listener
#   p4_assert_listener 断言端口正由本阶段启动的进程监听 (排除遗留顶替)

# p4_stop_service <pidfile> <port> <name>
p4_stop_service() {
  local pidfile=$1 port=$2 name=$3
  if [ -f "$pidfile" ]; then
    local pid; pid=$(cat "$pidfile")
    if sudo kill -0 "$pid" 2>/dev/null; then
      echo "stopping $name (pid $pid)"
      sudo kill "$pid" 2>/dev/null || true
    else
      echo "$name pid $pid already gone"
    fi
  else
    echo "$name: no pidfile $pidfile"
  fi
  local i
  for i in $(seq 1 20); do
    if curl -s --noproxy '*' --max-time 2 -o /dev/null \
        "http://127.0.0.1:$port/todo/api/tasks"; then
      sleep 1
    else
      break
    fi
  done
  if curl -s --noproxy '*' --max-time 2 -o /dev/null \
      "http://127.0.0.1:$port/todo/api/tasks"; then
    echo "FATAL: $name still answering on port $port"; exit 1
  fi
  if sudo ss -tln | grep -q ":$port "; then
    echo "FATAL: port $port still has a listener after stopping $name"
    sudo ss -tlnp | grep ":$port " || true
    exit 1
  fi
  echo "$name stopped; port $port released"
}

# p4_assert_listener <port> <pidfile> <name>
p4_assert_listener() {
  local port=$1 pidfile=$2 name=$3
  local pid; pid=$(cat "$pidfile")
  sudo kill -0 "$pid" 2>/dev/null || {
    echo "FATAL: $name started process $pid is not alive"; exit 1; }
  # || true: pipefail 下 grep 无匹配/head 提前退出会让替换失败 — 空值由下方逻辑判 FATAL
  local listener
  listener=$(sudo ss -tlnpH "sport = :$port" \
    | grep -o 'pid=[0-9]*' | head -1 | cut -d= -f2 || true)
  if [ "$listener" != "$pid" ]; then
    echo "FATAL: port $port is served by pid '$listener', expected $pid ($name)"
    echo "       — a leftover process is serving this port!"
    sudo ss -tlnp | grep ":$port " || true
    exit 1
  fi
  echo "$name: port $port is served by pid $pid (this phase's own process)"
}

# p4_assert_distinct <new-pidfile> <old-pidfile> <label>
p4_assert_distinct() {
  local newf=$1 oldf=$2 label=$3
  local new old
  new=$(cat "$newf"); old=$(cat "$oldf" 2>/dev/null || echo none)
  if [ "$new" = "$old" ]; then
    echo "FATAL: $label did not start a fresh process (pid $new unchanged)"
    exit 1
  fi
  echo "$label: fresh process pid $new (previous: $old)"
}
