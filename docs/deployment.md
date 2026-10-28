# Deployment Guide

y8y Todo is designed to sit **behind an authenticating reverse proxy**.
The server itself binds to `127.0.0.1` by default and treats any
identity header as untrusted unless the request comes from a configured
proxy address.

## 1. Authentication model

```
Internet ──▶ Reverse proxy (authenticates the user!)
                 │  injects header: Cf-Access-Authenticated-User-Email: alice@example.com
                 ▼
            y8y-todo (127.0.0.1:8086) ──▶ <data_dir> (markdown files)
```

**Two independent layers — do not confuse them:**

| Layer | What it does | What it is NOT |
|---|---|---|
| **App layer** (`Y8Y_TRUSTED_PROXIES`) | A *network-position gate*: the identity header is only read when the request's **TCP peer** is a configured proxy address. `X-Forwarded-For` and friends are ignored — the peer address cannot be forged by the client. | **Not authentication.** It answers "may this header be believed?", never "is this user who they claim?". |
| **Proxy layer** (your reverse proxy) | **The only place users are authenticated.** The proxy verifies the user (password, SSO, …) and only then injects the identity header. | Nothing in the app authenticates proxy traffic for you. |

**The critical rule:** if your proxy does not authenticate, then anyone
who can reach the proxy can impersonate anyone. A bare
`reverse_proxy` forwards the client's headers as-is — a forged
`Cf-Access-Authenticated-User-Email` would arrive at the app from the
trusted proxy address and be believed. §3 shows complete, tested
setups; the bare example that used to live here was removed for exactly
this reason.

- Machine-to-machine services authenticate with `X-Internal-Token`
  (see §4) — that token *is* the credential (constant-time compared,
  accepted from any source by design; keep it off public networks).
- If you don't run a proxy: use **dev mode** for local single-user
  access only. Production without an authenticating proxy is not
  supported (the server refuses to start in production mode with no
  identities configured).

**Static UI vs API — two different answers to "is it public?"**

`GET /todo/` and the vendored assets are served **without
authentication** (they are one static shell — no user data is ever in
them), so they answer **200** for everyone. Every `/todo/api/*` endpoint
requires authentication and answers **401** otherwise. Do not read the
200 on the UI shell as an auth bypass: no task, list, or preference data
is ever served unauthenticated.

## 2. Environment reference

| Variable | Default | Notes |
| --- | --- | --- |
| `Y8Y_DATA_DIR` | OS user data dir; dev mode `./dev-data` | task storage root; created on demand |
| `Y8Y_PORT` | `8086` | kept stable for API clients |
| `Y8Y_BIND` | `127.0.0.1` | keep loopback unless you know why |
| `Y8Y_USERS` | *(empty)* | `email=id,email=id`; `id` becomes the task owner name |
| `Y8Y_TRUSTED_PROXIES` | `127.0.0.1` | comma-separated IPs that may set the identity header |
| `Y8Y_AUTH_HEADER` | `Cf-Access-Authenticated-User-Email` | header carrying the authenticated email |
| `Y8Y_PUBLIC_URL` | `http://localhost:PORT` | informational (display); no OAuth since the Google integration was removed |
| `Y8Y_TIMEZONE` | `Asia/Tokyo` | IANA name; affects due dates |
| `Y8Y_DEV_MODE` | *(off)* | `1` = single local user, forced loopback, `./dev-data` |
| `Y8Y_DEV_USER` | `dev` | identity used in dev mode |
| `Y8Y_CN_HOLIDAYS` | `1` | master switch for the Chinese-holiday display in the calendar |
| `Y8Y_CN_HOLIDAY_USERS` | *(empty = all)* | comma-separated identity ids that see Chinese holidays by default |

The notification subsystem has **its own** variables, read only by the
scheduler process — see [§ 5.1 Notifications](#51-notifications-optional).
They are intentionally *not* in the table above: the web service must
never be able to read a LINE token or an email API key.

### User mapping

`Y8Y_USERS="alice@example.com=alice,bob@example.com=bob"` maps the
proxy-verified email to a local identity. That identity is what appears
in the task `owner` field and controls visibility:

- each user sees **their own** tasks and **shared** tasks
- private tasks are invisible to other users
- legacy data without an `owner` field is attributed to the **first
  configured user** (zero-migration compatibility)

### Chinese-holiday display (per-user preference)

The calendar can show Chinese statutory holidays and make-up workdays.
Before 2026-10 this was hardcoded to one fixed username; it is now
configuration:

- `Y8Y_CN_HOLIDAYS=0` turns the feature off for everyone.
- `Y8Y_CN_HOLIDAY_USERS=alice,carol` makes the default **per user**
  (only listed identities see holidays by default; empty = everyone).
- Each user can still override locally in the UI (Settings → Calendar →
  Chinese holidays); the override is stored per identity in the browser.

**Migrating an old single-user default** (e.g. a pre-2026-10 behavior
where only one identity saw Chinese holidays): set
`Y8Y_CN_HOLIDAY_USERS=<that-identity-id>` — every other user then
defaults to off and can opt in locally. No data migration is needed;
the preference is display-only.

## 3. Reverse proxy setups (complete and tested)

The proxy has three jobs, and all of them are security-critical:

1. **Authenticate** the user — never let an unauthenticated request
   through to the app.
2. **Inject the identity header from the authentication result** — a
   client-supplied value of the same header must never survive. A
   client that forges `Cf-Access-Authenticated-User-Email` against your
   proxy must not be able to choose their identity.
3. **Strip the client's `X-Internal-Token`** on the public entry — the
   app accepts the machine token *as a credential from any source*, so a
   browser user who obtained someone else's token could otherwise
   impersonate that user **through your public proxy** (the token is
   checked before the identity header). Machine clients must reach the
   app by their own local entry instead (see §4).

> **⚠️ A bare `reverse_proxy` is NOT a deployment.** Caddy forwards the
> client's headers as-is, so a forged identity header would reach the
> app *from the trusted proxy address* and be believed — the app's
> network-position gate cannot help you there (it only proves the
> request came from the proxy, not that the proxy authenticated
> anyone). Every setup below authenticates first.

All configs below were **verified end to end** (fictional users Alice /
Bob, including the forgery cases *and* the token-carrying browser case)
— see `docs/acceptance.md` §6 for the reproducible test.

### Option A — Caddy with built-in basic auth (self-contained)

No external identity provider needed; suitable for a household or small
team over HTTPS. Passwords are bcrypt-hashed (`caddy hash-password`),
and the identity header is taken from the **authentication result**
(`{http.auth.user.id}`), never from the client request:

```caddy
todo.example.com {
    basic_auth {
        alice@example.com $2a$14$...    # hash of alice's password
        bob@example.com $2a$14$...      # hash of bob's password
    }
    reverse_proxy 127.0.0.1:8086 {
        header_up Cf-Access-Authenticated-User-Email {http.auth.user.id}
        header_up -X-Internal-Token      # 公开入口剥离机器 Token (浏览器用户不得挟带)
    }
}
```

Why this is safe (all verified):

- `basic_auth` rejects requests without valid credentials (**401 at the
  proxy**, the app never sees them).
- `{http.auth.user.id}` is the username **basic_auth authenticated** —
  it is not a request header, so a client cannot influence it.
- `header_up` **replaces** the field for the upstream request: a
  client-supplied `Cf-Access-Authenticated-User-Email` is discarded.
  Do **not** use the `+` form (`header_up +X` *adds* a second value and
  would let a forged one through).
- `header_up -X-Internal-Token` **removes** the machine token from the
  upstream request. The app checks the token *before* the identity
  header, so without this line a logged-in browser user carrying a
  valid internal token would be identified as **that token's user**,
  not as themselves. Verified: Bob logged in with Alice's valid token is
  still `bob` (`tests/test_auth.py::test_public_proxy_strips_machine_token`).
- A user who logs in as Bob but sends `Cf-Access-Authenticated-User-
  Email: alice@example.com` is still identified as **bob** by the app.

Note: basic auth sends the password on every request — only use it over
HTTPS, and prefer Option B if you have (or want) an SSO provider.
(Directive name: `basic_auth` since Caddy v2.8; `basicauth` is the
deprecated alias.)

### Option B — Caddy + an authenticating proxy (SSO)

Put oauth2-proxy / Authelia / Cloudflare Access in front of Caddy. That
component authenticates the user and sets **its own** identity header
(which it always overwrites — a client-supplied value never survives
it); Caddy then maps that authenticated value into the header the app
expects:

```caddy
todo.example.com {
    forward_auth 127.0.0.1:4180 {
        uri /oauth2/auth
        copy_headers Cf-Access-Authenticated-User-Email {http.reverse_proxy.X-Auth-Request-Email}
    }
    reverse_proxy 127.0.0.1:8086 {
        header_up -X-Internal-Token      # 公开入口剥离机器 Token (同 Option A)
    }
}
```

Sketch of the chain: client → oauth2-proxy (authenticates, sets
`X-Auth-Request-Email`) → Caddy (`forward_auth` verifies with the
provider and copies the authenticated email into the identity header)
→ app. Consult your provider's docs for the exact header names; the
security requirements are the same as Option A: authenticate first,
inject the identity only from the authentication result, and strip the
client's machine token.

### nginx (equivalent, verified end to end with nginx 1.26.2)

```nginx
server {
    listen 443 ssl;
    server_name todo.example.com;

    location / {
        auth_basic "y8y todo";
        auth_basic_user_file /etc/nginx/y8y-todo.htpasswd;   # bcrypt (htpasswd -B)
        proxy_pass http://127.0.0.1:8086;
        # $remote_user is the username nginx authenticated — proxy_set_header
        # REPLACES any client-supplied value of the same name.
        proxy_set_header Cf-Access-Authenticated-User-Email $remote_user;
        # An empty value means nginx does not send this header upstream at
        # all, so a client-supplied machine token never reaches the app.
        proxy_set_header X-Internal-Token "";
    }
}
```

Keep **all** `proxy_set_header` directives in the same block. nginx
inherits `server`-level `proxy_set_header` directives **only when the
`location` defines none of its own** — so a tempting "defaults at
`server` level, override in `location`" split silently drops the token
stripping. Measured with a real nginx 1.26.2 E2E (both variants, plus a
no-stripping control): with the split, a logged-in browser user
carrying someone else's valid token is identified as **that user**
through the proxy. See `docs/acceptance.md` §6 for the test.

Without `auth_basic`, `$remote_user` is empty and nginx drops an empty
header — so the request would reach the app unauthenticated (401).
That is a misconfiguration, not a bypass: the app still refuses it.

### Proxy security checklist

- [ ] The proxy authenticates **before** the app sees the request;
      unauthenticated requests never reach the app.
- [ ] The identity header is injected **from the authentication
      result** (`{http.auth.user.id}` / `$remote_user` / provider
      header), and client-supplied values of that header are
      **replaced** (stripped).
- [ ] The client's `X-Internal-Token` is **stripped** on the public
      entry (`header_up -X-Internal-Token` / `proxy_set_header
      X-Internal-Token ""`). The app checks the token before the
      identity header — without this, a browser user carrying someone
      else's token impersonates that user through your proxy.
- [ ] `Y8Y_BIND=127.0.0.1` (the default) — the app is not reachable
      except through the proxy. Never bind `0.0.0.0` "just to test".
      Anything that can reach the app directly from a trusted address
      can mint any identity.
- [ ] `Y8Y_TRUSTED_PROXIES` lists **only** the real proxy addresses
      (same-host proxy → `127.0.0.1`). Every address in this list can
      mint any user identity.
- [ ] `X-Internal-Token` is treated as a credential: it is accepted
      from any source, so keep it off public networks and rotate it by
      editing `internal_tokens.json` + restarting (the token map is
      loaded at process start — see `docs/legacy-data.md` §5).
- [ ] Defense in depth: even if a forged header ever reached the app
      from a non-proxy address, it is ignored — the app only honors
      the header from addresses in `Y8Y_TRUSTED_PROXIES`
      (`tests/test_auth.py` locks this, including `X-Forwarded-For`
      spoofing).

## 4. Machine-to-machine access (y8ysystems-style services)

Other services on the same host/network can pull the task API with a
shared token:

1. Create `<data_dir>/internal_tokens.json`:

   ```json
   {"alice": "a-long-random-hex-string"}
   ```

2. Call:

   ```
   GET http://127.0.0.1:8086/todo/api/tasks
   Header: X-Internal-Token: a-long-random-hex-string
   ```

The token maps to the user identity in the JSON key. Tokens are compared
in constant time and can be rotated by editing the file and restarting.

**The JSON key must be a local identity id** — the right-hand side of
the `Y8Y_USERS` mapping (`alice`), never the e-mail
(`alice@example.com`) and never a group name. The key *is* the identity
the caller becomes; a token keyed by an e-mail authenticates as the
literal string `alice@example.com`, which matches no task owner, so the
caller would see only `shared` data and get 403 on everything else.

**Public proxy vs machine clients — the boundary:**

- **Browser/human traffic** goes through the public authenticating
  proxy. That entry **strips** `X-Internal-Token` (§3), so the token can
  never override the authenticated identity there.
- **Machine clients** must **not** go through that public entry (their
  token would be stripped). Give them their own local entry instead:
  connect to `127.0.0.1:8086` directly (same host / private network),
  or a dedicated internal vhost/port that does **not** strip the token.
  The app still accepts the token from any source — that is by design;
  what keeps it safe is that the token never travels through the public
  entry in the first place.

**Contract stability**: the path prefix (`/todo/api/...`), the response
field names, and the default port `8086` are part of the machine
contract. The test suite (`tests/test_api_compat.py`) guards them.

## 5. systemd (non-root)

Ready-to-use unit and env-file examples ship in `packaging/`. The
walkthrough below is the complete path from a fresh clone to a running
service; every step runs as root via `sudo`, the service itself never
does. (The CI `systemd-e2e` job rehearses exactly these steps on a
fresh Ubuntu 24.04 runner.)

```bash
# 1. Code → /opt/y8y-todo (the unit's WorkingDirectory and ExecStart
#    expect this path). Code stays root-owned and world-readable —
#    the service only reads it, and ProtectSystem=strict keeps it
#    read-only for the service anyway.
sudo git clone https://github.com/y8y-systems/y8y-todo.git /opt/y8y-todo

# 2. Dedicated non-root user; its home IS the data directory
#    (useradd --system does not create the home dir, hence the mkdir).
sudo useradd --system --user-group -d /var/lib/y8y-todo \
    --shell /usr/sbin/nologin y8y
sudo mkdir -p /var/lib/y8y-todo
sudo chown y8y:y8y /var/lib/y8y-todo

# 3. Environment file — identity config, never inline in the unit.
#    root:root mode 600 is correct: systemd (PID 1) reads the
#    EnvironmentFile as root BEFORE dropping privileges to the service
#    user, so there is no need (and no reason) to chown it to y8y.
sudo mkdir -p /etc/y8y-todo
sudo cp /opt/y8y-todo/packaging/y8y-todo.env.example \
    /etc/y8y-todo/y8y-todo.env
sudo chmod 600 /etc/y8y-todo/y8y-todo.env
sudo -e /etc/y8y-todo/y8y-todo.env      # set Y8Y_USERS etc.

# 4. Register and start the unit.
sudo cp /opt/y8y-todo/packaging/y8y-todo.service \
    /etc/systemd/system/y8y-todo.service
sudo systemctl daemon-reload
sudo systemctl enable --now y8y-todo
systemctl status y8y-todo --no-pager
```

Notes — why the pieces are compatible with each other:

- **Code ownership**: `/opt/y8y-todo` is root-owned and world-readable.
  The service runs as `y8y` and only reads it; it can never rewrite its
  own source. (`ProtectSystem=strict` additionally makes `/opt`
  read-only for the service regardless of file modes.)
- **Working directory**: `WorkingDirectory=/opt/y8y-todo/src` matches
  the clone layout; `ExecStart` uses absolute paths.
- **Env file permissions**: `/etc/y8y-todo/y8y-todo.env` is root:root
  600 — readable by systemd before the privilege drop, invisible to
  the service user afterwards.
- **Hardening vs. writable state**: `NoNewPrivileges=true`,
  `ProtectSystem=strict`, `ProtectHome=true` (only `/home`, `/root`,
  `/run/user` are hidden — the data dir at `/var/lib/y8y-todo` is
  unaffected), `PrivateTmp=true`. The single writable path is
  `ReadWritePaths=/var/lib/y8y-todo`, which covers tasks, lists and
  any historical `google_calendar/` leftovers; logs go to journald.
- **Upgrades**: `git -C /opt/y8y-todo pull` as root, then
  `sudo systemctl restart y8y-todo`. The on-disk data format is
  untouched by code updates.

### Standalone Python runtime (when the system Python is too old)

The core needs **Python ≥ 3.10 and nothing else** — no pip packages.
Ubuntu 22.04 ships Python 3.10, which works as-is; the packaged unit's
`ExecStart=/usr/bin/python3 ...` is fine there. When you prefer a newer
interpreter (longer support window) **without touching the system
Python** and without disturbing other services on the host, install a
standalone build and point the unit at it:

```bash
# Recommended: uv manages standalone CPython builds per-user (no root,
# no apt PPA, no RC versions). Keep the pin exact — it is your runtime.
sudo -u y8y bash -lc 'uv python install 3.12'
sudo -u y8y bash -lc 'uv python find 3.12'          # note the path

# Example ExecStart with the standalone interpreter:
#   ExecStart=/var/lib/y8y-todo/.local/share/uv/python/cpython-3.12.*/bin/python3 \
#            /opt/y8y-todo/src/serve.py
```

Alternative on Ubuntu 22.04: `add-apt-repository ppa:deadsnakes/ppa &&
apt install python3.12` — works, but pulls a third-party PTA into the
host. `uv` avoids that and is version-explicit.

Whatever interpreter you pick, verify it under systemd exactly like CI
does — the running process's `/proc/<pid>/exe` must be the intended
interpreter (sudo's `secure_path` and systemd's minimal environment
both resolve bare `python3` differently than your shell).

### Verifying a deployment end to end

`scripts/e2e-api.py` and `scripts/e2e-browser.py` (stdlib-only /
playwright) can be pointed at any running instance to re-run the same
checks CI performs. **They mutate their target** (create / complete /
restore tasks) — only point them at an isolated test instance with
fictional data, never at a deployment with real tasks. `full` refuses
to run against a non-empty instance unless `Y8Y_E2E_ALLOW_NONEMPTY=1`
is set. See the README section "Verifying a deployment end to end"
for full usage.

### What CI actually verifies

The `linux-deploy-e2e` workflow (Ubuntu 24.04) contains three jobs:

- `systemd-e2e` — runs the walkthrough above for real:
  `systemd-analyze verify` (static unit validation), then
  `systemctl enable --now` / `restart` / `stop` with the packaged unit,
  asserting active state, service user `y8y`, the running interpreter,
  and data persistence across the restart (via `scripts/e2e-api.py`
  full + persist phases).
- `linux-e2e` (Python 3.11 / 3.12 matrix) — the same lifecycle with the
  service started directly as a non-root user. The matrix interpreter
  is pinned explicitly (sudo's `secure_path` would otherwise silently
  fall back to the system Python) and verified through
  `/proc/<pid>/exe`; full API E2E (46 checks) + restart persistence
  (11 checks).
- `browser-e2e` — the real UI in Chromium against a dev-mode instance.

The `pytest` workflow runs the suite on Python 3.11 and 3.12; the
`ubuntu-2204` workflow additionally verifies the **real target
platform** (Ubuntu 22.04, system Python 3.10, systemd 249): the pack-
aged unit under real systemd, a standalone-Python variant, and the
full pytest suite on the system interpreter.

### 5.1 Notifications (optional)

The notification subsystem is a **separate one-shot program** driven by
its own systemd timer. It is off by default and completely optional —
a deployment that never installs it behaves exactly as before.

```
/etc/systemd/system/y8y-todo-notify.service   ← Type=oneshot
/etc/systemd/system/y8y-todo-notify.timer     ← every 5 minutes
/etc/systemd/system/y8y-todo-notify-dry-run.service  ← manual dry-run
/etc/systemd/system/y8y-todo-notify-test@.service     ← manual channel test
/etc/y8y-todo/y8y-todo-notify.env             ← credentials, root:root 600
/opt/y8y-todo/src/notify_runner.py             ← the scheduler
```

#### Why a separate process and a separate env file

The web service must never be able to read a LINE channel token or an
email API key. The scheduler therefore owns the credentials, and it
publishes a **credential-free capability table** (`notification_channels.json`)
that says *which user may use which channel*. The web app reads only
that file to render the settings panel.

`y8y-todo.service` does **not** include `y8y-todo-notify.env` in its
`EnvironmentFile=` list — only `y8y-todo-notify.service` does.

The scheduler also runs under its **own system account** (`y8y-notify`),
not under the web account (`y8y`). That is what actually stops a
compromised web process from reading the LINE token or the email API key
out of the scheduler's process — see §5.2 for the full model, the
measurement behind it, and the rollback.

#### Install

First follow the complete account, ACL, state directory and controlled Web restart
procedure in §5.2.6. The abbreviated commands below are not sufficient on
a private data directory:

```bash
sudo useradd --system --user-group --no-create-home \
        --shell /usr/sbin/nologin y8y-notify
sudo usermod --append --groups y8y-notify y8y
sudo install -d -o y8y-notify -g y8y-notify -m 2770 /var/lib/y8y-todo/notify
# Apply the task ACLs and restart Web as described in §5.2.6 before proceeding.
```

Then install the units:

```bash
sudo cp /opt/y8y-todo/packaging/y8y-todo-notify.service \
        /etc/systemd/system/y8y-todo-notify.service
sudo cp /opt/y8y-todo/packaging/y8y-todo-notify.timer \
        /etc/systemd/system/y8y-todo-notify.timer
sudo cp /opt/y8y-todo/packaging/y8y-todo-notify-dry-run.service \
        /etc/systemd/system/y8y-todo-notify-dry-run.service
sudo cp /opt/y8y-todo/packaging/y8y-todo-notify-test@.service \
        /etc/systemd/system/y8y-todo-notify-test@.service
sudo cp /opt/y8y-todo/packaging/y8y-todo-notify.env.example \
        /etc/y8y-todo/y8y-todo-notify.env
sudo chmod 600 /etc/y8y-todo/y8y-todo-notify.env   # root:root
sudo -e /etc/y8y-todo/y8y-todo-notify.env          # fill in token / api key / targets
sudo systemctl daemon-reload
# Follow §5.2.8 and verify all checks before enabling the timer.
sudo python3 /opt/y8y-todo/scripts/check-notify-isolation.py && \
    sudo systemctl enable --now y8y-todo-notify.timer
```

All three scheduler units read `/etc/y8y-todo/y8y-todo.env` as well
(identity mapping and data directory), so the scheduler knows who the
users are and where the tasks live.

#### Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `Y8Y_NOTIFY_ENABLED` | `0` | master switch; `0` = the scheduler does nothing at all |
| `Y8Y_NOTIFY_LINE_TOKEN` | *(empty)* | LINE Messaging API channel access token |
| `Y8Y_NOTIFY_LINE_TARGETS` | *(empty)* | `user=LINE-user-id,...` — per-user LINE recipient |
| `Y8Y_NOTIFY_EMAIL_API_KEY` | *(empty)* | Resend API key |
| `Y8Y_NOTIFY_EMAIL_FROM` | *(empty)* | sender address (a domain you verified with Resend) |
| `Y8Y_NOTIFY_EMAIL_TARGETS` | *(empty)* | `user=mail@example.com,...`; empty = use the address from `Y8Y_USERS` |
| `Y8Y_NOTIFY_TASK_SCOPE` | `shared` | which tasks may be announced: `shared` (shared tasks only) or `own_and_shared` (own + shared) |
| `Y8Y_NOTIFY_RECIPIENTS` | *(empty)* | `user,...` allow-list of recipients; empty = everyone who enabled notifications and has a target |
| `Y8Y_NOTIFY_WINDOW_MINUTES` | `10` | grace window after the target moment; beyond it the notification is **missed, not sent late** |
| `Y8Y_NOTIFY_RETENTION_DAYS` | `30` | how long delivery state is kept before it is pruned |
| `Y8Y_NOTIFY_TIMEOUT` | `15` | per-request timeout in seconds |

The `user` key in the target maps is the identity id from
`Y8Y_USERS="email=id"` — not the email address.

A **double opt-in**: `Y8Y_NOTIFY_ENABLED=1` only wakes the subsystem
up; each user still has to enable notifications for themselves in
**Settings → Notifications** and pick at least one channel. If a
channel has no credentials, it is simply skipped — it never makes the
Todo app error out.

#### Rolling out the shared-task policy (LINE, canary user)

The shipped defaults announce **shared tasks only**, digest them once a
day at **06:00** (Asia/Tokyo), and remind **30 minutes** before a task
that has a clock time. Delivery is at-most-once: nothing is back-filled
and nothing is retried.

To canary this on a single user, set in
`/etc/y8y-todo/y8y-todo-notify.env`:

```bash
Y8Y_NOTIFY_ENABLED=1
Y8Y_NOTIFY_TASK_SCOPE=shared          # shared tasks only (default)
Y8Y_NOTIFY_RECIPIENTS=kara            # pin the canary: nobody else receives
Y8Y_NOTIFY_WINDOW_MINUTES=10          # grace window after the target moment
Y8Y_NOTIFY_LINE_TOKEN=<token>         # from the LINE Messaging API console
Y8Y_NOTIFY_LINE_TARGETS=kara=<LINE-user-id>
# Email stays off: leave Y8Y_NOTIFY_EMAIL_API_KEY / _EMAIL_TARGETS empty.
```

> ⚠️ `Y8Y_NOTIFY_CATCHUP_HOURS` was **removed** — the catch-up window
> contradicted the "never send late" rule. Delete the line from the env
> file; leaving it does nothing, but keeping it hides the change from
> the next reader.

The recipient's own preferences still apply (double opt-in): in
**Settings → Notifications** that user has to turn notifications on,
pick the **LINE** channel, and turn the **daily digest** on. Their lead
time and digest time default to 30 minutes / 06:00.

**Keep the timer disabled until the Operator has accepted the change:**

```bash
sudo systemctl is-enabled y8y-todo-notify.timer   # → disabled
sudo systemctl start y8y-todo-notify-dry-run.service
sudo journalctl -u y8y-todo-notify-dry-run.service -n 50 --no-pager
```

Only after the dry-run shows exactly the expected recipients, titles
and timings should the Operator enable the timer by hand.

#### Verify before enabling

Use the two helper units. They load the same `EnvironmentFile=` as the
timer, so the LINE token and the Resend API key stay in the process
environment — they never appear in the command line (where any local
user could read them out of `/proc/<pid>/cmdline`), in the shell
history, or in the journal. Expanding the env file into an argument
list by hand is exactly what these units exist to avoid.

```bash
# What would be sent right now? (computes everything, sends nothing)
sudo systemctl start y8y-todo-notify-dry-run.service
sudo journalctl -u y8y-todo-notify-dry-run.service -n 50 --no-pager

# Does the credential work for one user?
sudo systemctl start y8y-todo-notify-test@alice.service
sudo journalctl -u 'y8y-todo-notify-test@alice.service' -n 50 --no-pager

# Logs
journalctl -u y8y-todo-notify.service -n 50 --no-pager
systemctl list-timers y8y-todo-notify.timer
```

`--dry-run` also prints the planned messages, so you can confirm the
wording and the recipients before anything leaves the server.

The instance name of the test unit (`alice` above) is a **user id**,
not a credential — it is the id part of `Y8Y_USERS="email=id"`. Keep it
to letters, digits, `.`, `_` and `-`: systemd splits the command line on
whitespace.

Credential values are redacted everywhere they could show up: if a
provider call fails, the error text stored in `notification_state.json`,
printed by the runner and written to the journal replaces the token and
the API key with `***`.

#### Operational notes

- The timer runs every 5 minutes, so a reminder is delivered at most
  ~5 minutes late. Set `AccuracySec=1min` (already in the packaged
  timer) and systemd aligns the wake-up to whole minutes.
- `Persistent=false` (the shipped timer) means a reboot does **not**
  run the missed tick. Nothing is ever back-filled: a reminder whose
  window has passed is dropped, and a digest missed at 06:00 is not
  re-sent later that day.
- The scheduler never crashes on a provider error, and it never retries:
  a failed delivery is recorded as a final outcome (`failed` /
  `exhausted`, with the attempt count and the redacted error) and is
  never attempted again. A transient network fault therefore loses that
  one message — the accepted trade-off for never double-sending.
- A reminder is only sent inside its window — from the target moment
  (`due_time` minus the lead time) up to `Y8Y_NOTIFY_WINDOW_MINUTES`
  later (default 10). Beyond the window it is dropped, not delivered
  late; the runner reports it as `missed`.
- Delivery state is keyed by *occurrence* (user, owner, file, created
  stamp, due date/time), so rescheduling a task produces a fresh
  reminder while an unchanged task is never sent twice.
- `notify/notification_state.json` should be part of your backup: without
  it a restored deployment would re-send reminders that already went out.
  The whole data directory is what you back up, so it is included
  (§7).

### 5.2 Credential isolation (process permissions)

#### 5.2.1 The question

> **If the web process is compromised, can it read the LINE token and the
> Resend API key out of the scheduler's process?**

With the configuration this release ships, **no** — and the reason is
kernel-enforced, not a configuration convention: the two services run
under **different system accounts**, so the kernel refuses the read
before any permission bit or unit option is even consulted.

#### 5.2.2 The shipped configuration

| | web service | notification scheduler |
| --- | --- | --- |
| unit | `y8y-todo.service` | `y8y-todo-notify.service` (+ `.timer`) |
| user | `y8y` | `y8y-notify` |
| credential file | **not referenced** | `/etc/y8y-todo/y8y-todo-notify.env` (root:root 600) |
| writable path | `/var/lib/y8y-todo` | `/var/lib/y8y-todo/notify` **only** |
| reads | tasks, lists, preferences | tasks (read-only), preferences |

Both accounts are members of the group `y8y-notify`, which owns the
notification state directory `/var/lib/y8y-todo/notify`. That directory
is the **only** place both accounts may write — see §5.2.4 for why it
has to be a directory and not a set of files.

Five things hold the boundary up:

1. `y8y-todo.service` does **not** list `y8y-todo-notify.env` in its
   `EnvironmentFile=` — the web process's own environment contains no
   credential values at all.
2. The credential file is `root:root 0600`. systemd (PID 1) reads it as
   root and injects the values into the scheduler's environment.
3. The two units run as **different users** → the kernel denies the
   cross-process environment read (§5.2.3).
4. The scheduler publishes only a **credential-free capability table**
   (`notification_channels.json`); the web app renders the settings
   panel from it.
5. Credentials never appear in a command line (systemd's
   `EnvironmentFile=`, never a shell that expands the file into an
   argument list) and never appear in the journal (errors are
   redacted).

#### 5.2.3 Why a separate system user

Whether one user may read another process's environment is decided by
`ptrace_may_access()` (`kernel/ptrace.c`) — and for the "same user"
case it waves the request through:

```c
if (uid_eq(caller_uid, tcred->euid) && uid_eq(caller_uid, tcred->suid) &&
    uid_eq(caller_uid, tcred->uid)  && gid_eq(caller_gid, tcred->egid) …)
        goto ok;                       /* same UID → allowed */
if (ptrace_has_cap(tcred->user_ns, mode))
        goto ok;                       /* different UID → needs CAP_SYS_PTRACE */
return -EPERM;
```

With **different** UIDs the comparison fails and the call returns
`-EPERM` — before any LSM hook is consulted. Measured on the target
platform (Ubuntu 22.04, kernel 5.15):

| attempt | result |
| --- | --- |
| same-UID read of `/proc/<pid>/environ` | **succeeds** — the token is in cleartext |
| same-UID `ptrace(PTRACE_ATTACH)` | `EPERM` |
| `kernel.yama.ptrace_scope` | `1` |
| cross-UID read of `/proc/<pid>/environ` | **denied** (`EACCES`) |

The Yama setting is what makes a same-UID setup *look* protected, but it
is not: `yama_ptrace_access_check()` (`security/yama/yama_lsm.c`) only
intercepts requests carrying `PTRACE_MODE_ATTACH`. Reading
`/proc/<pid>/environ` uses the READ mode, which Yama does not touch.
`/proc` is also mounted without `hidepid=`.

What does **not** protect you:

| mechanism | what it really does | helps against the environ read? |
| --- | --- | --- |
| `ProtectSystem=strict` | makes the filesystem read-only for the unit | **no** — `/proc` is not part of it |
| `ProtectHome=true` | hides `/home`, `/root`, `/run/user` | **no** — the secrets are in process memory |
| `NoNewPrivileges=yes` | forbids gaining privileges via setuid binaries | **no** — reading needs no privilege at all |
| `PrivateTmp=yes` | a private `/tmp` | **no** |
| `kernel.yama.ptrace_scope=1` | blocks `ptrace` attach | **no** — READ mode (environ) is not intercepted |
| `ProtectProc=invisible` / `ptraceable` | hides other users' processes | **no** — `hidepid=2`/`hidepid=4` still show same-UID processes |
| `LoadCredential=` | puts the credential in `/run/credentials/<unit>/` | **no** — that directory is `0700` owned by the *service user*; with a shared UID the web process reads it directly |

One more dead end worth recording: **systemd has no `Dumpable=` unit
option.** Neither v249 (the Ubuntu 22.04 version) nor current systemd
exposes a way to say "start this service with `PR_SET_DUMPABLE=0`" —
checked in `src/core/execute.c`, `exec-invoke.c`, `service.c` and the
`systemd.exec` manual. The in-process hardening of §5.2.5 therefore has
to be done by the program itself.

#### 5.2.4 The permission model — and the contradiction this release fixes

**The problem with a file-level ACL.** The three notification JSON
files are written with `atomic_write()`, which is *create a temporary
file, then rename it over the target*. Both steps need **write +
execute on the containing directory**:

- creating the temporary file needs write+execute on the directory;
- `rename(2)` — which `os.replace()` uses — needs write+execute on the
  directory too. The permissions of the *target file* are irrelevant
  for the rename.

So "give the scheduler write access to these three files" is not a
sufficient recipe: with the directory only `r-x`, the scheduler cannot
create its temporary file; and granting the directory `rwx` to let it
create one also lets it replace **anything else in that directory** —
including `internal_tokens.json`, the machine-to-machine credential map
(`docs/deployment.md` §4).

**The resolution: one dedicated directory.** All three notification
files live in `/var/lib/y8y-todo/notify`, owned by the scheduler, with
the shared group and the setgid bit set:

```bash
sudo install -d -o y8y-notify -g y8y-notify -m 2770 /var/lib/y8y-todo/notify
```

| file | written by | read by | how |
| --- | --- | --- | --- |
| `notification_settings.json` | web (`y8y`) | scheduler | group `y8y-notify` |
| `notification_state.json` | scheduler | backup / operator | group `y8y-notify` |
| `notification_channels.json` | scheduler | web (`y8y`) | group `y8y-notify` |
| `notification.lock` | scheduler | scheduler | group `y8y-notify` |

`notification.lock` is an empty `flock` file the scheduler creates on its
first run. It serialises whole scheduler runs, so two timer ticks (or a
timer tick racing a manual run) can never claim the same notification. It
holds no data: if it was created by another account and the scheduler can no
longer open it, the scheduler **stops** (fail closed) instead of delivering
without the lock — recreate it with
`sudo install -o y8y-notify -g y8y-notify -m 0664 /var/lib/y8y-todo/notify/notification.lock`.

Why each piece is there:

- **group `y8y-notify` (2770)** — both accounts are members, so both
  may create and rename files in the directory. "Other" has no access
  to this directory. Web retains access to its data outside it.
- **setgid (the `2`)** — every file created in the directory inherits
  the group, so the file one account writes stays readable by the
  other even after `os.replace()` swaps in a fresh inode. Without it
  the group would revert to the writer's primary group on every write.
- **umask 0022** (both units) — files land as `0644`, i.e. readable by
  the other account. Either group member can replace or delete any file
  through the writable directory, regardless of the file mode.
- **`ReadWritePaths=-/var/lib/y8y-todo/notify`** in the notify unit —
  a systemd-level allowlist on top of the Unix permissions. The data
  directory root is read-only for the scheduler, so even a compromised
  scheduler cannot replace `internal_tokens.json`.

The data directory root stays private. A named-user ACL grants only traversal
to `y8y-notify`; named and default ACLs on `todo/` and `recurring/` grant
read access to their task trees (§5.2.6). The scheduler reads all users’
active tasks and writes only its shared state directory.

> If the operator skips the `install -d` step, the web app creates the
> directory on its first write — owned by `y8y`, group `y8y`, and the
> scheduler can then neither write its delivery state nor read the
> preferences. That is **fail-closed**: the scheduler refuses to send
> anything (see below) and the audit script reports it. It is not a
> silent partial failure.

**Fail-closed on an unwritable state directory.** The delivery state is
the only thing that prevents a reminder from being sent twice. So the
scheduler probes the state directory *before* it sends anything: if the
directory is not writable it stops with a clear message and sends
**nothing**. A run that cannot record what it sent must not send.

#### 5.2.5 Process hardening (defense in depth, not the boundary)

`src/procguard.py` performs an in-process hardening, and `notify_runner.py`
calls it as the first thing `main()` does — i.e. the process that
*holds* the credentials is the one that closes its own exposure. It sets
`prctl(PR_SET_DUMPABLE, 0)`, after which:

  * `/proc/<pid>/environ` → EACCES
  * `/proc/<pid>/mem`    → EACCES
  * ptrace attach        → refused (no longer depends on Yama)
  * core dump            → not written (credentials do not land on disk)

`/proc/<pid>/cmdline` and `/proc/<pid>/status` **stay readable** — so
systemd's process supervision, `ps`, and the CI job that reads
`/proc/<pid>/exe` keep working unchanged.

| observation | before | after |
| --- | --- | --- |
| same-UID read of `/proc/<pid>/environ` | readable | `EACCES` |
| same-UID read of `/proc/<pid>/mem` | readable | `EACCES` |
| `ptrace` attach | refused (Yama) | refused (kernel) |
| core dump | possible | none |
| `/proc/<pid>/cmdline`, `/status`, `/exe` | readable | **still readable** |

With the separate system account of §5.2.3 this is the **second** layer,
not the only one: the kernel already denies the read because the UIDs
differ, and the hardening additionally covers the case where somebody
later puts both units back on one account.

Its known limitation: `execve()` resets the dumpable flag to 1, so
between the Python interpreter starting and the process calling
`prctl()` (tens of milliseconds) the environment of a *same-UID* peer
is still readable. The separate account closes that window too.

Linux 5.15's `/proc/<pid>/status` has no `Dumpable` field (checked
against `fs/proc/array.c` v5.15), so the state cannot be read from
another process — the scheduler therefore reports it itself: every run
prints `dumpable=0` (or `dumpable=1` when the escape hatch is on) in
the journal.

Debugging escape hatch — set in the env file, no file or unit change
needed:

```bash
Y8Y_NOTIFY_KEEP_DUMPABLE=1   # keep core dumps / gdb attach for this run
```

#### 5.2.6 Setup

The notification units are optional: a deployment that never installs
them never creates the second account and is unaffected. Installing them
requires the following preparation **before** the timer is enabled. Adapt
paths and account names to the installed unit. Install the Linux `acl` tools
using the operator's normal change process. Back up ACL metadata and review
existing ACLs, masks, ownership and symlinks before changing anything; ACL
mask recalculation can affect existing group permissions. Do not recursively
follow symlinks. Resolve any symlinks in the two task trees before proceeding.

```bash
sudo useradd --system --user-group --no-create-home \
        --shell /usr/sbin/nologin y8y-notify
sudo usermod --append --groups y8y-notify y8y
sudo install -d -o y8y-notify -g y8y-notify -m 2770 /var/lib/y8y-todo/notify
# Create the two task roots as Web if absent, preserving existing directories.
sudo -u y8y mkdir -p /var/lib/y8y-todo/todo /var/lib/y8y-todo/recurring
# Only traversal at the private root: no listing or writing.
sudo setfacl -m u:y8y-notify:--x /var/lib/y8y-todo
# Existing directories: read and traverse. Default ACLs inherit into newly
# created directories and files (including atomic_write's replacement files).
sudo find -P /var/lib/y8y-todo/todo /var/lib/y8y-todo/recurring -type d \
    -exec setfacl -m u:y8y-notify:r-x,d:u:y8y-notify:r-x {} +
# Existing Markdown tasks only.
sudo find -P /var/lib/y8y-todo/todo /var/lib/y8y-todo/recurring -type f -name '*.md' \
    -exec setfacl -m u:y8y-notify:r-- {} +
# A running service does not acquire supplementary groups after usermod.
# Schedule this controlled restart and verify Web health before enabling timer.
sudo systemctl restart y8y-todo.service
```

Default ACLs can grant read access to other newly created files *inside these
 two task trees*, not just Markdown files. Do not put credentials there.
Restrictive creation modes, later chmod/ACL changes, moving a pre-existing file
into the tree, alternate writers or symlinks can defeat inheritance; re-audit
and repair such cases. The audit checks current files, not every future writer.
The `internal_tokens.json` file must exist and remain inaccessible to the
notification account; do not grant ACLs on it. Both accounts can replace any
file in `notify/`: this directory is a shared trust boundary, not a state
integrity boundary between Web and the scheduler.

Then install the units as in §5.1, run the audit in §5.2.8, and enable the
timer only after all checks pass. If setup fails, leave Todo Web running and
disable the timer:

```bash
sudo systemctl disable --now y8y-todo-notify.timer
```

Do not switch the scheduler to the Web UID as a fallback. Preserve data and
investigate or revert only the specific changes under the operator's change
procedure. No production operations are performed by this repository's CI.

**Acceptance.** Run the audit script from §5.2.8 as root: every check
must report `OK` and the script must exit `0`.

#### 5.2.7 What the scheduler may and may not do

| | may | may not |
| --- | --- | --- |
| tasks | read | write, rename, delete |
| `notification_settings.json` | read, replace | — |
| `notification_state.json` | write | — |
| `notification_channels.json` | write | — |
| `internal_tokens.json` | — | read *or* write |
| the credential file | read (via systemd) | — |

Both accounts can create, replace, and delete every file in the shared
state directory. File mode 0644 does not prevent directory-level replacement.

#### 5.2.8 Audit script

`scripts/check-notify-isolation.py` (wrapper: `scripts/check-notify-isolation.sh`)
is an audit that does not change persistent host state. It starts a short-lived
process under the notify UID with a fictional environment, and reads no real
credential values. It never creates a user, changes permissions, or starts or
stops a service.

```bash
# On the target host, as root:
sudo python3 scripts/check-notify-isolation.py \
        --unit-dir /etc/systemd/system \
        --env-file /etc/y8y-todo/y8y-todo-notify.env \
        --data-dir /var/lib/y8y-todo
```

| check | what it asserts |
| --- | --- |
| C1 | the web unit and the notify unit do **not** run as the same user |
| C2 | the credential file is owned by root and unreadable by group/other |
| C2b | the web unit does **not** reference the credential file |
| C3 | a controlled probe has the notify UID, distinct from the Web UID |
| C4 | measured: the Web identity cannot read the controlled probe’s `/proc/<pid>/environ` |
| C5 | both task trees and Markdown files are accessible, state directory writable, root not writable and internal token inaccessible |
| C6 | the web user can read the capability table and write the preferences |
| C7 | the notify unit's writable paths do **not** cover the whole data directory |

Exit codes: `0` = all checks passed, `1` = a check failed, `2` = a
check could not be verified (for example C4 needs root and Linux `/proc`). **`2` is not a pass.** Checks that cannot be decided are
reported as `UNKNOWN` on purpose: a container or an unprivileged shell
cannot emulate the target host's `/proc` and systemd behaviour, and
this project does not want a simulated result presented as a
production one. The same rule applies *inside* the script: when a
permission probe cannot be decided (dropping privileges failed, the
helper process did not start), the result is `UNKNOWN` — never "OK".

`--repo-packaging` makes the script audit the unit templates in
`packaging/` instead of the installed ones — useful for CI.

## 6. Upgrading from versions with Google Calendar

Earlier releases had an optional two-way Google Calendar sync. It was
removed in 2026-10; this section is what changes when you upgrade.

**What the new version does NOT do**

- Never contacts Google (no OAuth, no token refresh, no API calls).
- Never starts a background sync worker or queue.
- Serves no Google Calendar UI; every leftover
  `/todo/api/google-calendar/*` request gets `410 Gone`.

**What is preserved (data safety first)**

- The whole `google_calendar/` directory inside the data dir stays on
  disk, byte for byte — the backup/restore tooling includes it in its
  manifest and per-file SHA-256 checks. You can delete it manually
  whenever you like; the app never touches it either way.
- `task_uid` / `gcal_event_id` frontmatter fields on historical tasks
  survive every operation (edit, complete, restore, project move).
- Events already on your Google calendar remain there but will no
  longer be updated by the app. Removing them is a manual, one-time
  choice in Google Calendar itself.

**Cleanup (optional, manual)**

```bash
# after stopping the service and taking a backup:
sudo rm -rf /var/lib/y8y-todo/google_calendar
```

`tests/test_google_removed.py` pins this contract (no network, 410
endpoints, historical files untouched).

## 7. Backups

Back up the data directory; that's everything:

```bash
tar czf y8y-todo-backup-$(date +%F).tar.gz -C /var/lib y8y-todo
```

Because the store is plain markdown, partial restores and manual edits
are straightforward. Writes are atomic (temp file + rename), so a
crash can never leave a half-written task behind.

## 8. Upgrades

Source updates never touch the data directory. Pull the new code,
restart the service, done. The on-disk format is byte-compatible across
versions; new fields are additive and unknown fields are preserved.

When upgrading **from a version that had Google Calendar**, read §6
first: nothing Google-related runs anymore, and all historical data is
kept.
