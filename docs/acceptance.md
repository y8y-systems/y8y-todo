# Deployment acceptance checklist

A complete, self-contained acceptance pass for a y8y Todo deployment.
Anyone (a new operator, or an independent agent asked to validate a
candidate release) can run it end to end on a fresh Linux machine
using only this repository and **fictional data**.

Expected duration: ~45 minutes (plus CI runs).

## 0. Ground rules

- Fresh Linux environment (VM or container). Nothing from any previous
  deployment is needed or used.
- Only this repository (a clean clone). No private context, no
  production data, no real credentials.
- All identities, tokens, emails, passwords and task content used below
  are fictional (`example.com`, invented strings).
- The app binds `127.0.0.1` throughout; nothing is exposed publicly.

### Prerequisites on the fresh machine

- **Python ≥ 3.10** — the app itself needs only the standard library.
- **iproute2** for the `ss` command used below (`apt install iproute2`
  on Debian/Ubuntu; preinstalled on most server images).
- **pytest** — only for §9, and the suite's only dependency
  (`pip install pytest`). Supported range: **7.x / 8.x** (CI pins
  `pytest>=7.0,<9.0`; that pinned range is what the suite is verified
  against — newer majors are not covered).
- Chromium + playwright — only for the optional browser checks
  (`tests/browser_verify.py`, `tests/browser_multiuser.py`).

## Repeatable process management (use this everywhere)

Shell job numbers (`%1`) are unstable across re-runs. Use PID files:

```bash
start_app() {   # start_app <port> <data-dir> [extra env as VAR=VAL ...]
    local port=$1 data=$2; shift 2
    Y8Y_DATA_DIR="$data" Y8Y_BIND=127.0.0.1 Y8Y_PORT="$port" \
      Y8Y_USERS='alice@example.com=alice,bob@example.com=bob' \
      Y8Y_TRUSTED_PROXIES=127.0.0.1 Y8Y_TIMEZONE=Asia/Tokyo "$@" \
      nohup python3 src/serve.py > "/tmp/app-$port.log" 2>&1 &
    echo $! > "/tmp/app-$port.pid"
    for _ in $(seq 1 30); do
        code=$(curl -s --noproxy '*' -o /dev/null -w '%{http_code}' \
                "http://127.0.0.1:$port/todo/api/tasks" || true)
        [ "$code" = "401" ] && { echo "app up on :$port (pid $(cat /tmp/app-$port.pid))"; return 0; }
        sleep 0.5
    done
    echo "app failed to start on :$port"; cat "/tmp/app-$port.log"; return 1
}
stop_app() {    # stop_app <port>
    local port=$1
    [ -f "/tmp/app-$port.pid" ] && kill "$(cat "/tmp/app-$port.pid")" 2>/dev/null
    rm -f "/tmp/app-$port.pid"
    for _ in $(seq 1 20); do
        ss -tln | grep -q ":$port " || { echo "port $port released"; return 0; }
        sleep 0.5
    done
    echo "WARNING: port $port still occupied"; ss -tlnp | grep ":$port "
}
```

Every section below is safe to re-run: `stop_app` first, then start.

## 1. Install from the README

```bash
git clone https://github.com/y8y-systems/y8y-todo.git y8y-todo && cd y8y-todo
python3 --version            # must be >= 3.10
```

Dev mode first (zero configuration):

```bash
Y8Y_DEV_MODE=1 nohup python3 src/serve.py > /tmp/app-dev.log 2>&1 &
echo $! > /tmp/app-dev.pid; sleep 1
curl -s http://127.0.0.1:8086/todo/api/tasks      # 200, current_user "dev"
curl -s http://127.0.0.1:8086/todo/ | grep -q 'viewTabCalendar' && echo "UI OK"
kill "$(cat /tmp/app-dev.pid)"
```

**Pass:** the server starts with zero configuration and serves the UI.

Note: `GET /todo/` (the static UI shell) answers **200 without
authentication** by design — it contains no user data. Every
`/todo/api/*` endpoint requires authentication and answers 401
otherwise (§2 verifies that). Do not mistake the 200 on the shell for
an auth bypass.

## 2. Configure authentication (production mode)

```bash
export DATA=/tmp/y8y-acceptance/data
rm -rf "$DATA"; mkdir -p "$DATA"
start_app 8086 "$DATA"
```

**Pass:** `curl -s -H 'Cf-Access-Authenticated-User-Email:
alice@example.com' http://127.0.0.1:8086/todo/api/tasks` → 200 with
`current_user: "alice"` (this simulates a same-host trusted proxy; the
real proxy is verified in §6).

## 3. Create and modify tasks (fictional)

```bash
A='Cf-Access-Authenticated-User-Email: alice@example.com'
B='Cf-Access-Authenticated-User-Email: bob@example.com'
curl -s -X POST -H "$A" -H 'Content-Type: application/json' \
  -d '{"title":"验收任务-甲","due":"2026-12-01"}' http://127.0.0.1:8086/todo/api/tasks   # 201
curl -s -X POST -H "$A" -H 'Content-Type: application/json' \
  -d '{"title":"验收共享","owner":"shared"}' http://127.0.0.1:8086/todo/api/tasks        # 201
curl -s -X POST -H "$B" -H 'Content-Type: application/json' \
  -d '{"title":"验收任务-乙"}' http://127.0.0.1:8086/todo/api/tasks                      # 201
find "$DATA" -name '*.md' | wc -l     # matches what you created
```

**Pass:** CRUD works; the markdown files exist with YAML frontmatter.

## 4. Restart the service and verify persistence

```bash
stop_app 8086
start_app 8086 "$DATA"
curl -s -H "$A" http://127.0.0.1:8086/todo/api/tasks
```

**Pass:** all tasks from §3 survive the restart, with the edits.

## 5. Verify per-user permissions

```bash
curl -s -H "$A" http://127.0.0.1:8086/todo/api/tasks   # alice: own + shared
curl -s -H "$B" http://127.0.0.1:8086/todo/api/tasks   # bob: own + shared only
```

**Pass:** bob does **not** see `验收任务-甲` (alice's private task) but
does see `验收共享`; alice cannot create a task owned by bob (403);
bob cannot fetch alice's private task by id (403).

## 6. Verify forged identity headers cannot escalate

These must all fail with **401**, never 200:

```bash
# 6.1 未配置的邮箱:
curl -s -o /dev/null -w '%{http_code}\n' \
  -H 'Cf-Access-Authenticated-User-Email: mallory@example.com' \
  http://127.0.0.1:8086/todo/api/tasks        # expect 401
# 6.2 无任何凭据:
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8086/todo/api/tasks   # expect 401
# 6.3 身份头重复且值冲突 (代理误用"追加"语义时的伪造):
python3 - <<'PY'
import socket, json
s = socket.create_connection(('127.0.0.1', 8086), timeout=5)
req = ('GET /todo/api/tasks HTTP/1.1\r\nHost: 127.0.0.1\r\n'
       'Cf-Access-Authenticated-User-Email: bob@example.com\r\n'
       'Cf-Access-Authenticated-User-Email: alice@example.com\r\n'
       'Connection: close\r\n\r\n')
s.sendall(req.encode()); data = b''
while True:
    c = s.recv(65536)
    if not c: break
    data += c
print('status:', data.split(b'\r\n')[0].split()[1].decode(), '(expect 401)')
PY
```

**Pass:** all 401. (Locked in CI by
`tests/test_auth.py::test_forwarded_for_spoofing_cannot_grant_trust`
and `::test_conflicting_duplicate_identity_header_rejected`.)

## 7. Real reverse proxy E2E (Caddy + basic auth, verified pattern)

This is the **authenticated-proxy** acceptance: the app is fronted by a
real reverse proxy that authenticates fictional users Alice and Bob.
The Caddyfile below is the one documented in `docs/deployment.md` §3
Option A (verified end to end, including every forgery case).

```bash
stop_app 8086
start_app 8086 "$DATA"          # Y8Y_TRUSTED_PROXIES=127.0.0.1 (same-host proxy)

# Caddy (official binary or docker image caddy:2-alpine)
# passwords are FICTIONAL; hashes generated with: caddy hash-password
ALICE_HASH=$(caddy hash-password --plaintext 'alice-fictional-pw')
BOB_HASH=$(caddy hash-password --plaintext 'bob-fictional-pw')
cat > /tmp/Caddyfile <<CADDY
:8080 {
    basic_auth {
        alice@example.com $ALICE_HASH
        bob@example.com $BOB_HASH
    }
    reverse_proxy 127.0.0.1:8086 {
        header_up Cf-Access-Authenticated-User-Email {http.auth.user.id}
        header_up -X-Internal-Token
    }
}
CADDY
caddy start --config /tmp/Caddyfile --adapter caddyfile
P=http://127.0.0.1:8080
cu() { curl -s --noproxy '*' -u "$1" "$P/todo/api/tasks" \
       | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("current_user"))'; }
```

Verify (all must hold):

1. **Unauthenticated cannot access the private API:**
   `curl -s -o /dev/null -w '%{http_code}' "$P/todo/api/tasks"` → **401**
   (rejected at the proxy; the app never sees the request).
2. **Alice sees own + shared:** `cu alice@example.com:alice-fictional-pw`
   → `alice`, and `验收任务-甲` + `验收共享` are in her task list.
3. **Bob sees own + shared:** `cu bob@example.com:bob-fictional-pw` → `bob`,
   `验收共享` present, `验收任务-甲` **absent**.
4. **Bob forges Alice's identity header → still Bob:**
   ```bash
   curl -s --noproxy '*' -u 'bob@example.com:bob-fictional-pw' \
     -H 'Cf-Access-Authenticated-User-Email: alice@example.com' \
     "$P/todo/api/tasks" | python3 -c 'import json,sys; print(json.load(sys.stdin)["current_user"])'
   ```
   → `bob`, and alice's private task is still invisible (the proxy
   replaced the forged header with the authenticated username).
5. **Forged forwarding headers grant nothing:** with no credentials +
   `-H 'X-Forwarded-For: 127.0.0.1' -H 'X-Real-IP: 127.0.0.1'` → **401**;
   with Bob's credentials + the same forged headers + a forged identity
   header → still `bob`.
6. **current_user equals the real login identity:** confirmed by 2–4
   (the value the app reports is exactly the proxy-authenticated user).
7. **Bob carrying Alice's valid machine token is still bob** — the
   public proxy strips `X-Internal-Token`, so the token can never
   override the authenticated identity (the app checks the token
   *before* the identity header, by design):
   ```bash
   echo '{"alice":"acceptance-fictional-token-alice"}' > "$DATA/internal_tokens.json"
   stop_app 8086; start_app 8086 "$DATA"     # restart loads the token map
   curl -s --noproxy '*' -u 'bob@example.com:bob-fictional-pw' \
     -H 'X-Internal-Token: acceptance-fictional-token-alice' \
     "$P/todo/api/tasks" | python3 -c 'import json,sys; print(json.load(sys.stdin)["current_user"])'
   ```
   → `bob`. The **same token used directly** against
   `127.0.0.1:8086` authenticates as `alice` (§8) — machine clients keep
   their own local entry; the public entry is for humans only. Without
   the `header_up -X-Internal-Token` line this check returns `alice`
   (impersonation) — that is the risk the line removes.

Cleanup: `caddy stop`.

Note: hitting the app **directly** on `127.0.0.1:8086` with a forged
header succeeds *by design* — loopback is in `Y8Y_TRUSTED_PROXIES`.
That is exactly why the app must never be reachable except through the
proxy (see `docs/deployment.md` §3 checklist).

### 7.1 nginx equivalent (verified end to end, nginx 1.26.2)

The same ten checks were run against a real nginx 1.26.2 (compiled from
source, `/tmp/nginx-e2e`) using the config documented in
`docs/deployment.md` §3 — unauthenticated 401, Alice/Bob login, Bob
cannot read Alice's private task, forged identity header ignored,
invalid password 401, forged `X-Forwarded-For` / `X-Real-IP` grant
nothing, normal writes work, and the machine-token boundary of §7
check 7 both ways (stripped at the proxy; still valid direct).

**Finding (fixed in `docs/deployment.md` §3):** the token-stripping
directive must live in the **same block** as the other
`proxy_set_header` directives. nginx inherits `server`-level
`proxy_set_header` directives only when the `location` defines none of
its own — so the originally documented "defaults at `server` level,
identity override in `location`" split silently **dropped the token
stripping**. Real-request evidence (Bob logged in, carrying Alice's
valid token):

| nginx config variant                                   | app sees |
| ------------------------------------------------------ | -------- |
| stripping directive inside `location` (documented now) | `bob`    |
| stripping directive at `server` level only (old docs)  | `alice` ← impersonation |
| no stripping directive at all (control)                | `alice` ← impersonation |

If you run your own nginx E2E, keep this table in mind: a config that
"looks right" can still forward the client's machine token.

## 8. Machine token (optional, fictional)

```bash
echo '{"alice":"acceptance-fictional-token-001"}' > "$DATA/internal_tokens.json"
stop_app 8086; start_app 8086 "$DATA"     # restart — the token map is loaded at startup
curl -s -H 'X-Internal-Token: acceptance-fictional-token-001' \
  http://127.0.0.1:8086/todo/api/tasks      # 200, current_user alice
curl -s -o /dev/null -w '%{http_code}\n' -H 'X-Internal-Token: wrong' \
  http://127.0.0.1:8086/todo/api/tasks      # 401
```

**Pass:** correct token authenticates; wrong token 401. Remember the
map is loaded at process start — editing the file needs a restart
(`docs/legacy-data.md` §5).

Note: this section talks to the app **directly** (localhost / private
network). Through the public proxy of §7 the token is stripped **by
design** — browser traffic must never be able to carry it (§7 item 7).
The JSON key must be a **local identity id** (the right-hand side of
the `Y8Y_USERS` mapping, e.g. `alice`), never an e-mail.

## 9. Notifications (optional, only if the deployment uses them)

The notification subsystem is opt-in. If the deployment does not install
`y8y-todo-notify.service` / `.timer`, **skip this section entirely** — the
Todo app must be unaffected.

> ⚠️ Use **fictional** recipients only. Do not point this at a real LINE
> account or a real mailbox unless you intend to receive the messages.

> ℹ️ The commands below drive the two **helper units**
> (`y8y-todo-notify-dry-run.service` and `y8y-todo-notify-test@<user>.service`,
> see `docs/deployment.md` §5.1) instead of running the runner by hand.
> They load the credential file through systemd's `EnvironmentFile=`, so
> the token and the API key never appear in the command line, in the
> shell history, or in the journal. Install them before continuing:
>
> ```bash
> sudo cp /opt/y8y-todo/packaging/y8y-todo-notify-dry-run.service \
>         /etc/systemd/system/
> sudo cp /opt/y8y-todo/packaging/y8y-todo-notify-test@.service \
>         /etc/systemd/system/
> sudo systemctl daemon-reload
> ```

```bash
# 0. Nothing is configured yet → the scheduler must be a no-op, and the
#    app must not care.
sudo systemctl start y8y-todo-notify-dry-run.service
sudo systemctl show -p ExecMainStatus y8y-todo-notify-dry-run.service
sudo journalctl -u y8y-todo-notify-dry-run.service -n 50 --no-pager
#    → "notifications disabled (Y8Y_NOTIFY_ENABLED=0)"; ExecMainStatus=0
```

```bash
# 1. Master switch on, still no per-user opt-in → still nothing sent.
sudo -e /etc/y8y-todo/y8y-todo-notify.env     # Y8Y_NOTIFY_ENABLED=1
sudo systemctl restart y8y-todo-notify.service
sudo systemctl start y8y-todo-notify-dry-run.service
sudo systemctl show -p ExecMainStatus y8y-todo-notify-dry-run.service
#    → "no user has enabled notifications"; ExecMainStatus=0
```

```bash
# 2. The user opts in in the UI (Settings → Notifications): enable,
#    pick a channel, set the lead time and the digest time, save.
#    Then check what would be sent:
sudo systemctl start y8y-todo-notify-dry-run.service
sudo journalctl -u y8y-todo-notify-dry-run.service -n 50 --no-pager
#    → "[plan] user=alice channel=line kind=reminder …"
```

```bash
# 3. Credential check for one channel (does it actually deliver?).
#    The instance name is the user id, not a credential.
sudo systemctl start y8y-todo-notify-test@alice.service
sudo journalctl -u 'y8y-todo-notify-test@alice.service' -n 50 --no-pager
#    → "line: sent"
```

**Pass:** with the subsystem off the app behaves exactly as before; with
it on but no user opt-in nothing is sent; after opting in the dry-run
lists the expected reminders; `--test` confirms the credential works.

### 9.1 Privacy checks (must all pass)

```bash
# a. The web service must NOT be able to read the notification credentials.
sudo grep -c "EnvironmentFile" /etc/systemd/system/y8y-todo.service
#    → must not mention y8y-todo-notify.env

# b. The capability table the web app reads must contain no secrets.
sudo python3 -c "import json;d=json.load(open('$DATA/notification_channels.json'));\
print(json.dumps(d)[:2000])"
#    → no token, no api key, no "secret"

# c. Another user's private task must never appear in someone's digest.
#    Create a private task owned by bob, then dry-run as alice:
sudo systemctl start y8y-todo-notify-dry-run.service
sudo journalctl -u y8y-todo-notify-dry-run.service -n 50 --no-pager
#    → bob's private task title must not appear

# d. The credential values must not appear in the journal, in the delivery
#    state file, or in any process argument list.
sudo python3 - <<'PY'
import pathlib, subprocess
env = pathlib.Path('/etc/y8y-todo/y8y-todo-notify.env').read_text()
secrets = [line.split('=', 1)[1].strip() for line in env.splitlines()
           if line.startswith(('Y8Y_NOTIFY_LINE_TOKEN=', 'Y8Y_NOTIFY_EMAIL_API_KEY='))]
secrets = [s for s in secrets if s]
journal = subprocess.run(
    ['journalctl', '-u', 'y8y-todo-notify-dry-run.service', '--no-pager'],
    capture_output=True, text=True).stdout
state = pathlib.Path('/var/lib/y8y-todo/notification_state.json')
blobs = [journal, state.read_text() if state.exists() else '']
argv = subprocess.run(['ps', '-eo', 'args'], capture_output=True, text=True).stdout
blobs.append(argv)
print('LEAK' if any(s in blob for s in secrets for blob in blobs) else 'CLEAN')
PY
#    → CLEAN
```

**Pass:** (a) the web unit never reads the notify env file; (b) no
credential material in the capability table; (c) no other user's private
task in any plan; (d) no credential value in the journal, in the state
file, or in a process argument list.

### 9.2 Idempotency and recovery

```bash
# Run twice in a row — the second run must not re-send anything.
sudo systemctl start y8y-todo-notify.service
sudo systemctl start y8y-todo-notify.service
sudo python3 -c "import json;print(json.dumps(json.load(open('$DATA/notification_state.json')),indent=2))"
#    → each occurrence appears exactly once, with its delivery status
```

**Pass:** a second run produces no duplicate send; the state file
records the delivery, so a restart of the scheduler does not re-notify.

### 9.3 Credential isolation (process permissions)

These steps need a real host (real systemd, real `/proc`) and **root**.
They cannot be simulated in a container — see the "Unverified" note at
the end.

```bash
# 0. Baseline: the scheduler runs as the same user as the web service.
systemctl show -p User y8y-todo.service y8y-todo-notify.service
#    → both "y8y"   ← this is the condition that makes the check below fail

# 1. The scheduler must have hardened itself (PR_SET_DUMPABLE = 0).
#    Linux 5.15's /proc/<pid>/status has NO "Dumpable" field, so the
#    process must report it itself — the runner prints it on every run.
sudo systemctl start y8y-todo-notify-dry-run.service
sudo journalctl -u y8y-todo-notify-dry-run.service -n 20 --no-pager | grep -o 'dumpable=[0-9]*'
#    → dumpable=0        ← PASS (hardening active)
#    → dumpable=1        ← FAIL: the escape hatch Y8Y_NOTIFY_KEEP_DUMPABLE=1
#                           is set, or an old copy of src/procguard.py is running

# 2. The decisive test: read the scheduler's environment as the web user.
#    The shell that runs this must already be the web user.
PID=$(pgrep -f 'notify_runner.py' | head -1)
sudo -u y8y cat /proc/$PID/environ >/dev/null
#    → cat: /proc/<pid>/environ: Permission denied   ← PASS
#    → (contents printed)                              ← FAIL, escalate

# 3. The control group: cmdline must stay readable, so systemd, ps and
#    the CI job that reads /proc/<pid>/exe keep working.
sudo -u y8y cat /proc/$PID/cmdline
#    → readable (may look garbled: NUL separators)

# 4. The web unit must still not reference the credential file.
sudo grep -c "EnvironmentFile" /etc/systemd/system/y8y-todo.service
sudo grep -c "y8y-todo-notify.env" /etc/systemd/system/y8y-todo.service
#    → 0

# 5. The full audit (see docs/deployment.md §5.2.8).
sudo python3 /opt/y8y-todo/scripts/check-notify-isolation.py \
        --unit-dir /etc/systemd/system \
        --env-file /etc/y8y-todo/y8y-todo-notify.env \
        --data-dir /var/lib/y8y-todo
#    → all five checks OK, exit code 0
#    → "C1 FAIL" means both units still share a user: see §5.2.7
```

**Pass:** step 1 shows `dumpable=0`; step 2 is denied; step 3 still
reads; step 4 shows no credential file in the web unit; step 5 exits 0.

Note: Linux 5.15 (the Ubuntu 22.04 kernel) has **no** `Dumpable` field
in `/proc/<pid>/status` — checked against `fs/proc/array.c` v5.15 — so
the dumpable state has to be read from the process's own output, which
is why the runner prints `dumpable=0` on every run.

**Expected state today, before stage 2 (separate system user):**
step 5 reports `C1 FAIL` and `C3 FAIL` — that is the known, documented
gap of §5.2.7 and the reason the stage-2 migration exists. The
credential read in step 2 must nevertheless be denied, because the
scheduler hardens itself (§5.2.6). If step 2 succeeds, the scheduler
is running an old copy of `src/procguard.py` or the hardening was
disabled with `Y8Y_NOTIFY_KEEP_DUMPABLE=1`.

**Not verified by this repository:** the behaviour of systemd 249's
real unit files, `/proc` access across two real system users, and the
ownership/ACL migration of §5.2.7 were all developed and reasoned
about against the kernel and systemd sources, but the actual
production host is the only place they can be confirmed. The audit
script above is the instrument for that confirmation; run it as root
and paste the output into the deployment report.

## 10. Run the full test suite and CI

```bash
python3 -m pytest --basetemp=.pytest-tmp -q     # all green (Linux-only
                                                # cases need Linux; macOS skips 9)
```

pytest is the suite's **only** dependency (the app itself has none).
Supported range: **7.x / 8.x** — CI pins `pytest>=7.0,<9.0`, and that
pinned range is what the suite is verified against; newer majors are
not covered by the project's verification.

Then confirm the candidate repository's own GitHub Actions are green:
pytest (3.10/3.11/3.12), ubuntu-2204 (systemd 249), linux-deploy-e2e,
p4-upgrade-rehearsal.

**Pass:** suite green locally; all CI checks green on the PR/commit
under review.

## 11. Report

State for each section: PASS/FAIL, the exact commands that failed (if
any), and anything unexpected (even if it did not fail). Use fictional
data everywhere; never paste real task content into the report.
