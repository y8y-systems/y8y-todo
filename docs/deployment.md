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
sudo cp /opt/y8y-todo/packaging/y8y-todo-notify.env.example \
        /etc/y8y-todo/y8y-todo-notify.env
sudo chmod 600 /etc/y8y-todo/y8y-todo-notify.env   # root:root
sudo -e /etc/y8y-todo/y8y-todo-notify.env          # fill in token / api key / targets
sudo systemctl daemon-reload
sudo systemctl enable --now y8y-todo-notify.timer
```

Both units read `/etc/y8y-todo/y8y-todo.env` as well (identity mapping
and data directory), so the scheduler knows who the users are and where
the tasks live.

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

```bash
# What would be sent right now? (computes everything, sends nothing)
sudo -u y8y env $(cat /etc/y8y-todo/y8y-todo-notify.env) \
     /usr/bin/python3 /opt/y8y-todo/src/notify_runner.py --dry-run

# Does the credential work for one user?
sudo -u y8y env $(cat /etc/y8y-todo/y8y-todo-notify.env) \
     /usr/bin/python3 /opt/y8y-todo/src/notify_runner.py --test alice

# Logs
journalctl -u y8y-todo-notify.service -n 50 --no-pager
systemctl list-timers y8y-todo-notify.timer
```

`--dry-run` also prints the planned messages, so you can confirm the
wording and the recipients before anything leaves the server.

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
