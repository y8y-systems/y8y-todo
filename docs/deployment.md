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

#### Install

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
| `Y8Y_NOTIFY_CATCHUP_HOURS` | `24` | how long after the due time a missed reminder may still be sent |
| `Y8Y_NOTIFY_RETENTION_DAYS` | `30` | how long delivery state is kept before it is pruned |
| `Y8Y_NOTIFY_TIMEOUT` | `15` | per-request timeout in seconds |

The `user` key in the target maps is the identity id from
`Y8Y_USERS="email=id"` — not the email address.

A **double opt-in**: `Y8Y_NOTIFY_ENABLED=1` only wakes the subsystem
up; each user still has to enable notifications for themselves in
**Settings → Notifications** and pick at least one channel. If a
channel has no credentials, it is simply skipped — it never makes the
Todo app error out.

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
- `Persistent=true` makes systemd run the missed tick once after a
  reboot.
- The scheduler never crashes on a provider error: a failed delivery is
  recorded, retried with backoff (`5 · 2ⁿ` minutes, max 60, 5 attempts)
  and finally marked as exhausted. Permanent 4xx errors (other than
  408/425/429) are not retried.
- A reminder whose due time is more than `Y8Y_NOTIFY_CATCHUP_HOURS` in
  the past is dropped rather than delivered late — after a long outage
  you get the daily digest, not a burst of stale reminders.
- Delivery state is keyed by *occurrence* (user, owner, file, created
  stamp, due date/time), so rescheduling a task produces a fresh
  reminder while an unchanged task is never sent twice.
- `notification_state.json` should be part of your backup: without it a
  restored deployment would re-send reminders that already went out.

### 5.2 Credential isolation (process permissions)

#### 5.2.1 The question

`y8y-todo.service` and `y8y-todo-notify.service` run as the **same**
system user (`y8y`). The web process holds no credentials of its own —
but it shares a UID with the process that does.

> **If the web process is compromised, can it read the LINE token and
> the Resend API key out of the scheduler's process?**
>
> **Yes. In the default configuration it can, directly and trivially.**
> This is a real, exploitable weakness — not a theoretical one — and
> §5.2.7 describes the two-stage fix.

#### 5.2.2 What the current setup already gets right

These protections exist and are worth keeping:

- `y8y-todo.service` does **not** list `y8y-todo-notify.env` in its
  `EnvironmentFile=` — the web process's own environment contains no
  credential values.
- The env file is `root:root 0600`. systemd (PID 1) reads it as root
  and injects the values into the scheduler's environment.
- The web app never imports the notification config module and never
  opens a credential file.
- The scheduler publishes only a **credential-free capability table**
  (`notification_channels.json`).
- Credentials never appear in a command line (systemd's
  `EnvironmentFile=`, never a shell that expands the file into an
  argument list) and never appear in the journal (errors are
  redacted).

All of that stops the web service from *reading the credentials
directly*. None of it stops the web service from *inspecting another
process*.

#### 5.2.3 The actual exposure: same UID ⇒ `/proc/<pid>/environ` is readable

Whether one user may read another process's environment is decided by
`ptrace_may_access()` (`kernel/ptrace.c`) — and for the "same user"
case it waves the request through:

```c
if (uid_eq(caller_uid, tcred->euid) && uid_eq(caller_uid, tcred->suid) &&
    uid_eq(caller_uid, tcred->uid)  && gid_eq(caller_gid, tcred->egid) …
        goto ok;                       /* same UID → allowed */
if (ptrace_has_cap(tcred->user_ns, mode))
        goto ok;                       /* different UID → needs CAP_SYS_PTRACE */
return -EPERM;
```

Measured on the target platform (Ubuntu 22.04, kernel 5.15):

| attempt | result |
| --- | --- |
| same-UID read of `/proc/<pid>/environ` | **succeeds** — the token is in cleartext |
| same-UID `ptrace(PTRACE_ATTACH)` | `EPERM` |
| `kernel.yama.ptrace_scope` | `1` |

The Yama setting is what makes this *look* protected, but it is not:
`yama_ptrace_access_check()` (`security/yama/yama_lsm.c`) only
intercepts requests carrying `PTRACE_MODE_ATTACH`. Reading
`/proc/<pid>/environ` uses the READ mode, which Yama does not touch.
`/proc` is also mounted without `hidepid=`, so the scheduler's entry is
visible in the first place.

**Consequence.** A compromised web process can walk `/proc`, find the
scheduler by its `cmdline`, and read its environment — the LINE channel
token and the Resend API key, in cleartext, with no privilege required.
The scheduler is started every 5 minutes by a timer and exits, but the
web process is long-lived, so in practice the window is essentially
always open. This is the definition of an isolation boundary that does
not isolate.

#### 5.2.4 What does *not* protect you

| mechanism | what it really does | helps against the environ read? |
| --- | --- | --- |
| `ProtectSystem=strict` | makes the filesystem read-only for the unit | **no** — `/proc` is not part of it |
| `ProtectHome=true` | hides `/home`, `/root`, `/run/user` | **no** — the secrets are in process memory |
| `NoNewPrivileges=yes` | forbids gaining privileges via setuid binaries | **no** — reading needs no privilege at all |
| `PrivateTmp=yes` | a private `/tmp` | **no** |
| `kernel.yama.ptrace_scope=1` | blocks `ptrace` attach | **no** — READ mode (environ) is not intercepted |
| `ProtectProc=invisible` / `ptraceable` | hides other users' processes | **no** — `hidepid=2`/`hidepid=4` still show same-UID processes, and the reader is same-UID here |
| `LoadCredential=` (see §5.2.5 C) | puts the credential in `/run/credentials/<unit>/` | **no** — that directory is `0700` owned by the *service user*, so a same-UID process reads it directly |

One more dead end worth recording: **systemd has no `Dumpable=` unit
option.** Neither v249 (the Ubuntu 22.04 version) nor current systemd
exposes a way to say "start this service with
`PR_SET_DUMPABLE=0`" — checked in `src/core/execute.c`,
`exec-invoke.c`, `service.c` and the `systemd.exec` manual. The unit
file therefore cannot express the in-process hardening described in
§5.2.6; it has to be done by the program itself.

#### 5.2.5 Options A / B / C

**A — keep one user, harden the process**

Only viable form is in-process `prctl(PR_SET_DUMPABLE, 0)` (systemd
cannot express it, §5.2.4). It genuinely closes the vector: the
kernel's `get_dumpable()` check in `ptrace_may_access()` fails, so
`/proc/<pid>/environ` returns `EACCES`, `/proc/<pid>/mem` is refused,
`ptrace` attach is refused and no core dump is written.

- ✔ Zero deployment change, no new user, no file-ownership migration.
- ✔ Backward compatible, stdlib-only.
- ✘ **Residual window:** `execve()` resets the dumpable flag to 1, so
  between the Python interpreter starting and the process calling
  `prctl()` (tens of milliseconds) the environment is still readable.
- ✘ No core dumps and no `gdb` attach for post-mortem debugging.

**B — give the scheduler its own system user (`y8y-notify`)**

Different UID ⇒ `ptrace_may_access()` no longer takes the "same
user" shortcut and returns `-EPERM` at the UID comparison — before any
LSM hook is consulted. This is a *kernel-enforced* boundary rather
than a mitigation, and it also closes the residual window from option
A.

- ✔ Blocks the environ read at the source, with no privileges involved.
- ✔ Closes A's start-up window too.
- ✔ Composes with `LoadCredential=` (see C).
- ✘ Requires a new system user, a shared group or ACLs on the data
  directory, and a change of ownership for the three notification
  JSON files.
- ✘ The scheduler must be able to read every user's task files (it
  computes digests for all users) — so `y8y-notify` becomes a
  second principal that may read private task content. That is not a
  *widening* (today the same `y8y` UID reads everything too), but the
  notify account must be treated as trusted.

**C — systemd credentials (`LoadCredential=`)**

`LoadCredential=` exists since systemd v247, so Ubuntu 22.04 /
systemd 249 supports it. But it is a **file-delivery mechanism, not a
security boundary**: `/run/credentials/<unit>/` is `0700` owned by the
unit's own user, so with a shared UID the web process reads the
credential file directly. On its own it changes nothing about the
attack surface; it only becomes meaningful once option B is in place.

| | A — in-process hardening | B — separate user | C — `LoadCredential=` |
| --- | --- | --- | --- |
| Blocks the environ read | yes, after the start-up window | yes, kernel-enforced | no (on its own) |
| Blocks the start-up window | no | yes | no |
| Needs new system user | no | yes | no |
| Needs ownership/ACL migration | no | yes | no |
| Blocks `ptrace` attach | yes | yes | no |
| Needs core dump / gdb | no (escape hatch exists) | yes | unchanged |
| Stdlib-only, no new service | yes | yes | yes |
| Verdict | **ship now** | **recommended, staged** | only together with B |

**Recommendation — layered, in this order.**

1. **Ship A now.** It is a few lines of stdlib-only code, needs no
   deployment change, breaks nothing, and closes the real vector.
2. **Do B as a documented, manual follow-up** (§5.2.7). It is the
   only thing that also removes A's residual window, and it is the
   only complete answer to "the web service must never be able to
   obtain the credentials".
3. **Add C only if B is adopted.** Doing C alone would create the
   *appearance* of a hardened deployment without any actual
   protection — exactly the kind of configuration this project avoids.

#### 5.2.6 What this release already ships (option A)

`src/procguard.py` performs the hardening, and `notify_runner.py`
calls it as the first thing `main()` does — i.e. the process that
*holds* the credentials is the one that closes its own exposure. The
web service needs no change (it holds no secrets; hardening it would
also take away core dumps for debugging, which is out of scope here).

| observation | before | after |
| --- | --- | --- |
| same-UID read of `/proc/<pid>/environ` | readable | `EACCES` |
| same-UID read of `/proc/<pid>/mem` | readable | `EACCES` |
| `ptrace` attach | refused (Yama) | refused (kernel) |
| core dump | possible | none |
| `/proc/<pid>/cmdline`, `/status`, `/exe` | readable | **still readable** |

The last row matters: systemd's process supervision, `ps`, and the CI
job that reads `/proc/<pid>/exe` keep working unchanged. Only
`environ` and `mem` are affected.

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

**Known limitation:** the ~tens of milliseconds between the Python
interpreter starting and `prctl()` running. `execve()` resets the
dumpable flag to 1, so during that window the environment is still
readable by a same-UID process. Only option B removes it.

#### 5.2.7 Stage 2 — separate system user (manual, not part of this release)

This changes the host: it creates a production system user and moves
ownership of files. It is therefore **not** executed by this
repository and **not** bundled into the PR — it is documented here so
that the host operator can perform it deliberately, with a rollback.

**Prerequisites.** A backup of the data directory (§7). Confirm the
scheduler currently works (§5.1 "Verify before enabling").

**Step 1 — create the account and the shared group.**

```bash
sudo groupadd --system y8y-notify-share
sudo useradd --system --no-create-home \
        --home-dir /var/lib/y8y-notify --shell /usr/sbin/nologin \
        --gid y8y-notify-share y8y-notify
sudo usermod --append --groups y8y-notify-share y8y
```

**Step 2 — let the scheduler read the task tree (read-only).**

The scheduler needs to read every user's task files, but must not be
able to modify them. Use a group ACL on the data directory rather than
widening the mode bits:

```bash
sudo setfacl -R -m g:y8y-notify-share:r-X "$DATA"
sudo setfacl -R -d -m g:y8y-notify-share:r-X "$DATA"
```

`-X` (execute only on directories) keeps traversal possible without
granting write access. Do **not** use `chmod -R o+r` or `chmod 777`.

**Step 3 — hand the three notification files to the shared group.**

```bash
sudo chgrp y8y-notify-share "$DATA/notification_settings.json" \
                        "$DATA/notification_state.json" \
                        "$DATA/notification_channels.json"
sudo chmod 660 "$DATA/notification_settings.json" \
               "$DATA/notification_state.json" \
               "$DATA/notification_channels.json"
```

Ownership intent:

| file | written by | read by | mode |
| --- | --- | --- | --- |
| `notification_settings.json` | web (`y8y`) | scheduler | `660` group-shared |
| `notification_state.json` | scheduler | backup / operator | `660` group-shared |
| `notification_channels.json` | scheduler | web (`y8y`) | `660` group-shared |

If the files do not exist yet, create them as `root` with the group
already set, or let the scheduler create them and then apply the
`chgrp`/`chmod` above.

**Step 4 — switch the unit.**

```bash
sudo sed -i 's/^User=.*/User=y8y-notify/' /etc/systemd/system/y8y-todo-notify.service
sudo systemctl daemon-reload
sudo systemctl restart y8y-todo-notify.timer
```

Leave `y8y-todo.service` on `User=y8y`. Do **not** add
`y8y-todo-notify.env` to the web unit's `EnvironmentFile=` — that
would undo the whole exercise.

**Risks.**

- If the ACL or group is missing, the scheduler cannot read the tasks
  and silently delivers nothing. This is fail-closed (no data loss),
  but it looks like "reminders stopped working" — check the journal
  for permission errors first.
- If the scheduler runs with the wrong UID it cannot write
  `notification_state.json` and will re-send reminders it already
  delivered (the occurrence key is per-file, so the file's content is
  what protects you — losing it means duplicates, not data loss).
- The notify account can read every user's private task files. That is
  inherent to a digest scheduler and is not a regression (the shared
  `y8y` UID already could), but treat the account as trusted: no
  shell, no home directory, `nologin`.
- The web user can no longer write `notification_state.json` — that is
  intended; only the scheduler writes it.

**Rollback.**

```bash
sudo sed -i 's/^User=.*/User=y8y/' /etc/systemd/system/y8y-todo-notify.service
sudo systemctl daemon-reload && sudo systemctl restart y8y-todo-notify.timer
sudo chgrp y8y "$DATA"/notification_*.json
sudo chmod 600 "$DATA"/notification_*.json
sudo setfacl -R -x g:y8y-notify-share "$DATA"
sudo setfacl -R -d -x g:y8y-notify-share "$DATA"
sudo userdel y8y-notify && sudo groupdel y8y-notify-share
```

The rollback restores the previous behaviour exactly. **No task data is
rewritten at any point** — the markdown files, their frontmatter and
the JSON schemas are untouched by both the migration and the rollback;
only ownership and group bits change.

**Acceptance.** Run the audit script from §5.2.8 as root: all five
checks must report `OK` and the script must exit `0`.

#### 5.2.8 Audit script

`scripts/check-notify-isolation.py` (wrapper: `scripts/check-notify-isolation.sh`)
is a **read-only** audit. It never creates a user, never changes a
mode, never starts or stops a service, and never reads the credential
*values* (only the file's ownership and permission bits).

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
| C3 | the running scheduler's UID differs from the web user's |
| C4 | measured: the web user cannot read the scheduler's `/proc/<pid>/environ` |
| C5 | the scheduler user can read the task tree and write the state files |

Exit codes: `0` = all checks passed, `1` = a check failed, `2` = a
check could not be verified (for example C4 needs root and a running
scheduler). **`2` is not a pass.** Checks that cannot be decided are
reported as `UNKNOWN` on purpose: a container or an unprivileged shell
cannot emulate the target host's `/proc` and systemd behaviour, and
this project does not want a simulated result presented as a
production one.

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
