<div align="center">
  <img src="vendor/y8y-shared-chrome/brand/y8y-logo.svg" alt="y8y Todo — official logo" width="125">
</div>

# y8y Todo

A self-hosted task manager for small teams and households. Calendar-first,
with lists, projects, and recurring tasks.

Built to run anywhere Python 3 runs: **the core has zero third-party
dependencies** (Python standard library only), data lives as plain
markdown files, and there is no build step.

- **Backend**: single-file Python `http.server` app (`src/serve.py`)
- **Frontend**: single-file vanilla-JS SPA (`src/index.html`), no framework
- **Storage**: filesystem — tasks are markdown files with YAML-ish
  frontmatter, kept physically separate from the code
- **Runs on Python 3.10 – 3.13**: verified on 3.10 (Ubuntu 22.04 system
  Python), 3.11, 3.12 and 3.13; no third-party packages needed

> **Google Calendar (historical)**: an optional two-way sync existed in
> earlier versions and was removed in 2026-10. Old `google_calendar/`
> directories and `task_uid` / `gcal_event_id` frontmatter fields are
> preserved untouched — this version never contacts Google.

## Features

- Tasks: create / edit / complete / restore / delete, colors, due dates,
  due ranges, times, free-text markdown bodies
- Recurring tasks (multiple dates per period; API-level)
- Projects: tasks grouped into subdirectories, project-wide color sync
- Lists: shopping list built in, create your own lists, private/shared
- Persistent projects: pin projects so they survive "all done" filtering
- Three views: calendar (default) / list / shopping, deep-linkable via
  `?view=...`
- Drag tasks on the calendar to reschedule (with undo toast)
- Chinese and Japanese holiday display (configurable per deployment)
- Notifications (V1, opt-in): a separate scheduler delivers **LINE Messaging
  API** and **email (Resend)** reminders — per-task "remind me N minutes
  before" for tasks that carry a clock time, plus a **daily digest** for
  all-day tasks; every user picks their own channels and times
- Dark / light / auto theme
- Pixel font: [Fusion Pixel](https://github.com/TakWolf/fusion-pixel-font)
  (OFL-1.1) is bundled with the app — no CDN, no network access needed at
  runtime; Chinese, English and Japanese are covered
- Offline-friendly: the local markdown store is always the source of
  truth, and nothing ever blocks the UI on network calls

## Quick start (development)

```bash
git clone https://github.com/y8y-systems/y8y-todo.git y8y-todo
cd y8y-todo
Y8Y_DEV_MODE=1 python3 src/serve.py
```

Then open <http://127.0.0.1:8086/todo/>.

Dev mode gives you a single local user (`dev`), forces a loopback bind,
and stores data in `dev-data/` inside the repo — it can never collide
with a real deployment's data.

## Quick start (production)

```bash
# 1. Configure (environment variables; see docs/deployment.md)
export Y8Y_USERS="alice@example.com=alice,bob@example.com=bob"
export Y8Y_DATA_DIR=/var/lib/y8y-todo
export Y8Y_PUBLIC_URL=https://todo.example.com

# 2. Run behind a reverse proxy that authenticates users and injects
#    the identity header (example config in docs/deployment.md)
python3 src/serve.py
```

**Never expose the server directly to the internet.** It binds to
`127.0.0.1` by default and only trusts the identity header from
configured proxy addresses. See [docs/deployment.md](docs/deployment.md)
for the full deployment guide (reverse proxy, systemd, machine-to-machine
tokens).

## Configuration

All configuration is via environment variables (no config files, no
secrets in the repo). Highlights:

| Variable | Default | Meaning |
| --- | --- | --- |
| `Y8Y_DATA_DIR` | OS user data dir (`~/Library/Application Support/y8y-todo` etc.; dev mode: `./dev-data`) | where tasks live |
| `Y8Y_PORT` | `8086` | listen port |
| `Y8Y_BIND` | `127.0.0.1` | listen address (keep loopback + proxy) |
| `Y8Y_USERS` | *(empty)* | `email=id,email=id` — proxy-authenticated users |
| `Y8Y_TRUSTED_PROXIES` | `127.0.0.1` | IPs allowed to set the identity header |
| `Y8Y_PUBLIC_URL` | `http://localhost:PORT` | externally reachable URL (informational) |
| `Y8Y_TIMEZONE` | `Asia/Tokyo` | IANA timezone |
| `Y8Y_CN_HOLIDAYS` | `1` | Chinese-holiday display in the calendar (master switch) |
| `Y8Y_CN_HOLIDAY_USERS` | *(empty = all users)* | comma-separated identity ids that see Chinese holidays by default |
| `Y8Y_DEV_MODE` | *(off)* | single local user, forced loopback |

Full reference: [docs/deployment.md](docs/deployment.md).

### Notification variables (separate environment file)

Notifications are **off by default** and read their own environment file, so
the Web process never holds a LINE token or an API key:

| Variable | Default | Meaning |
| --- | --- | --- |
| `Y8Y_NOTIFY_ENABLED` | `0` | master switch for the whole notification subsystem |
| `Y8Y_NOTIFY_LINE_TOKEN` | *(empty)* | LINE Messaging API channel access token |
| `Y8Y_NOTIFY_LINE_TARGETS` | *(empty)* | `user=LINE-user-id,...` — who gets LINE |
| `Y8Y_NOTIFY_EMAIL_API_KEY` | *(empty)* | Resend API key |
| `Y8Y_NOTIFY_EMAIL_FROM` | *(empty)* | verified sender address |
| `Y8Y_NOTIFY_EMAIL_TARGETS` | *(empty)* | `user=mail@example.com,...` — overrides the `Y8Y_USERS` address |
| `Y8Y_NOTIFY_TASK_SCOPE` | `shared` | which tasks may be announced: `shared` (shared tasks only) or `own_and_shared` (own + shared, the V1 behaviour) |
| `Y8Y_NOTIFY_RECIPIENTS` | *(empty)* | `user,...` — allow-list of recipients; empty means "everyone who enabled notifications and has a target" |
| `Y8Y_NOTIFY_WINDOW_MINUTES` | `10` | grace window after the target moment; beyond it the notification is **missed, not sent late** |
| `Y8Y_NOTIFY_RETENTION_DAYS` | `30` | how long delivery state is kept before it is pruned |
| `Y8Y_NOTIFY_TIMEOUT` | `15` | per-request timeout (seconds) when calling the provider API |
| `Y8Y_NOTIFY_KEEP_DUMPABLE` | *(off)* | debug only — keep the scheduler dumpable (core dumps / `gdb`); normally it hardens itself so a same-UID process cannot read its environment |

See [Notifications](#notifications-v1) below and
[docs/deployment.md](docs/deployment.md) §5.1 and §5.2.

## Python runtime

The core needs **only the Python standard library** — no `pip install`,
no virtualenv requirement. Verified on CPython 3.10, 3.11, 3.12, 3.13.

On hosts whose system Python is older than 3.10 (e.g. Ubuntu 20.04's
3.8), install a standalone runtime and point the systemd unit at it —
see [docs/deployment.md](docs/deployment.md) §5 ("Standalone Python").
Ubuntu 22.04's system Python 3.10 works as-is; the packaged unit can
also be switched to a standalone 3.12 for a longer support window.

## Deploying as a service (systemd, non-root)

Complete walkthrough — from a fresh clone to a running service. Every
step below runs as root via `sudo`; the service itself never does.
(Also see [docs/deployment.md](docs/deployment.md) §5.)

```bash
# 1. Code → /opt/y8y-todo (the packaged unit expects this path).
#    The code stays root-owned and world-readable: the service only
#    ever reads it, and ProtectSystem=strict keeps it read-only.
sudo git clone https://github.com/y8y-systems/y8y-todo.git /opt/y8y-todo

# 2. Dedicated non-root user; its home IS the data directory.
sudo useradd --system --user-group -d /var/lib/y8y-todo \
    --shell /usr/sbin/nologin y8y
sudo mkdir -p /var/lib/y8y-todo
sudo chown y8y:y8y /var/lib/y8y-todo

# 3. Environment file (identity config — no secrets in the unit).
#    root:root mode 600 is correct: systemd reads it as root BEFORE
#    dropping to the service user. Do not chown it to y8y.
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

Why the pieces fit together:

- `/opt/y8y-todo` — the unit's `WorkingDirectory` and `ExecStart`
  point here; root-owned code means the service can never rewrite its
  own source (`ProtectSystem=strict` also makes `/opt` read-only for
  it regardless).
- `y8y` system user with home `/var/lib/y8y-todo` — `ProtectHome=true`
  only hides `/home`, `/root` and `/run/user`, so the data directory
  stays reachable; `ReadWritePaths=/var/lib/y8y-todo` is the only
  path the service may write (tasks, lists and any historical
  `google_calendar/` leftovers all live there; logs go to journald).
- Env file at `/etc/y8y-todo/y8y-todo.env`, mode 600 root-owned —
  referenced via `EnvironmentFile`, never inline in the unit.
- The unit binds `127.0.0.1` only — put an authenticating reverse
  proxy in front (see [docs/deployment.md](docs/deployment.md) §3).
- Need a newer Python than the system one? Point `ExecStart` at a
  standalone interpreter (see the "Standalone Python" note in the
  unit and docs/deployment.md §5).

## Notifications (V1)

A **separate scheduler** delivers reminders. It is a one-shot program
(`src/notify_runner.py`) driven by a systemd timer — not a daemon inside
the web app — so a web restart never loses or duplicates a notification,
and the scheduler can be scaled, paused or rolled back on its own.

```
src/serve.py        web app  ──▶ writes user preferences only
src/notifications.py engine  ──▶ decides what to send, tracks delivery state
src/notify_runner.py CLI    ──▶ one run, then exit (systemd timer)
src/taskstore.py    shared task-file parsing (web + engine, one implementation)
```

### What gets sent

**Notification scope is narrower than visibility scope.** The web app shows
a user their own tasks *plus* the shared ones (`taskstore.can_access`),
but the notifier only ever announces **shared** tasks (`owner: shared`).
Nobody's private task — not even its own owner's — is ever put into a
digest or a reminder. The scope is selected with `Y8Y_NOTIFY_TASK_SCOPE`
(`shared` by default, `own_and_shared` restores the older behaviour).

A task is only considered when it is **not completed**, has a **valid due
date**, and is in scope. Ordinary and recurring tasks are treated the same.

| Task shape | Notification | When |
| --- | --- | --- |
| has `due` **and** `due_time` | single reminder | at `due_time` minus the user's *lead time* (default 30 min) |
| all-day (no `due_time`) | part of the daily digest | once a day at the user's digest time (default 06:00, Asia/Tokyo) |

The daily digest contains **every** shared task due today — the ones with
a clock time *and* the all-day ones — sorted by time, with the all-day
tasks in their own section. It never contains overdue tasks: yesterday's
leftovers do not come back the next morning. If nothing is due today, no
digest is sent at all. At most one digest per day, and a digest missed
because the scheduler was down is **not** sent later that day.

The two channels are independent: an event that already showed up in the
06:00 digest still gets its own 30-minutes-before reminder.

Per user, under **Settings → Notifications** you can enable/disable
notifications, pick the channels (LINE, email), set the lead time and
the digest time.

### Privacy

- The web process **never** holds a LINE token or an email API key. The
  scheduler publishes a credential-free *capability table*
  (`notification_channels.json`) that says which user may use which
  channel; the web app only reads that.
- The web service and the scheduler run as **two different system
  accounts** (`y8y` and `y8y-notify`). The kernel refuses a cross-account
  read of `/proc/<pid>/environ`, so a compromised web process cannot pull
  the token out of the scheduler's process — this does not depend on any
  permission bit or unit option.
- The two accounts share exactly one directory, `notify/` under the data
  directory, and nothing else: the scheduler may not write the task tree
  nor the machine-to-machine credential file. Both accounts can replace
  any file in the shared notification state directory.
- Only tasks the recipient may see are ever considered — and the
  notification scope is narrower still: **shared tasks only**
  (`Y8Y_NOTIFY_TASK_SCOPE`). A private task of one user never reaches
  another user's digest or reminder, and never reaches its own owner's
  either. Shared tasks are delivered once per entitled user.
- Messages are plain text (no HTML), so task titles cannot inject markup
  into a mail client or a LINE message.
- The scheduler **hardens its own process** at startup
  (`prctl(PR_SET_DUMPABLE, 0)`): a same-account process — for example a
  compromised web service — cannot read the scheduler's
  `/proc/<pid>/environ`, so the credentials stay inside the process
  that needs them. Every run reports `dumpable=0` in the journal. This is
  the second layer on top of the separate account; see
  [docs/deployment.md](docs/deployment.md) §5.2 for the full isolation
  model, the measurements behind it, and the rollback.

### Delivery guarantees

Delivery is **at-most-once**. A message is either sent once or not sent
at all; there is no retry, no catch-up and no second chance.

- Each reminder is keyed by *occurrence* (`user|owner|file|created|due`),
  so changing a task's time produces a **new** reminder and the old
  state becomes irrelevant; a task that is completed, deleted or rolled
  forward by a recurring rule stops being reminded.
- Delivery state is persisted, so a restart never re-sends a message
  that already went out, and a task that was completed and later
  restored is not reminded twice.
- A reminder fires only inside its window — from the target moment
  (`due_time` minus the lead time) up to `Y8Y_NOTIFY_WINDOW_MINUTES`
  later (default 10, enough to cover the 5-minute timer tick). Outside
  that window the reminder is **dropped, never fired late**; the runner
  reports it as `missed`.
- The daily digest is likewise only sent near its digest time. If the
  scheduler was down at 06:00, the digest is recorded as `missed` and
  **not** re-sent later that day.
- A failed delivery is a **final** outcome: it is recorded (with the
  attempt count and the redacted error) as `failed` or `exhausted`, and
  it is never retried. A transient network fault therefore means a lost
  message — an accepted trade-off in exchange for never double-sending.
- The systemd timer runs with `Persistent=false`, so a reboot does not
  trigger a backlog of missed notifications.

### Install (server side)

```bash
# 0. The scheduler's own account + the shared state directory.
sudo useradd --system --user-group --no-create-home \
        --shell /usr/sbin/nologin y8y-notify
sudo usermod --append --groups y8y-notify y8y
sudo install -d -o y8y-notify -g y8y-notify -m 2770 /var/lib/y8y-todo/notify
# Apply the task ACLs and perform the controlled Web restart in
# docs/deployment.md §5.2.6 before continuing.

# 1. Units + env file (credentials stay on the server, root:root 600).
sudo cp /opt/y8y-todo/packaging/y8y-todo-notify.{service,timer} \
        /etc/systemd/system/
sudo cp /opt/y8y-todo/packaging/y8y-todo-notify-dry-run.service \
        /etc/systemd/system/
sudo cp /opt/y8y-todo/packaging/y8y-todo-notify-test@.service \
        /etc/systemd/system/
sudo cp /opt/y8y-todo/packaging/y8y-todo-notify.env.example \
        /etc/y8y-todo/y8y-todo-notify.env
sudo chmod 600 /etc/y8y-todo/y8y-todo-notify.env
sudo -e /etc/y8y-todo/y8y-todo-notify.env     # token / api key / targets

# 2. Run the timer.
sudo systemctl daemon-reload
sudo python3 /opt/y8y-todo/scripts/check-notify-isolation.py && \
    sudo systemctl enable --now y8y-todo-notify.timer
systemctl list-timers y8y-todo-notify.timer
```

Check what *would* be sent without sending it, and verify the
credentials of one channel. Use the two helper units — they load the
same `EnvironmentFile` as the timer, so the token and the API key stay
in the process environment and never reach the command line, the
journal, or the shell history:

```bash
sudo systemctl start y8y-todo-notify-dry-run.service
sudo journalctl -u y8y-todo-notify-dry-run.service -n 50 --no-pager

sudo systemctl start y8y-todo-notify-test@alice.service
sudo journalctl -u 'y8y-todo-notify-test@alice.service' -n 50 --no-pager
```

If no channel is configured, the scheduler simply does nothing — the
Todo app itself is unaffected.

### New data files

The notification system adds three **side-car** files in a dedicated
`notify/` sub-directory of the data directory. They are not part of the
task format, no task file is ever rewritten, and removing them only
resets notification preferences and history:

| File | Contents |
| --- | --- |
| `notification_settings.json` | per-user preferences (channels, lead time, digest time) |
| `notification_state.json` | delivery state — what was already sent, and how it ended |
| `notification_channels.json` | credential-free capability table written by the scheduler |

The scheduler also creates an empty `notification.lock` in the same
directory: an `flock` file lock that keeps two scheduler processes from
claiming the same notification at the same time. It holds no data and
can be deleted while no scheduler is running.

They live in their own directory (not in the data directory root) because
the scheduler and the web app both need to write in there, and `atomic_write`
creates a temporary file *in the same directory* before renaming it over
the target — which needs **write + execute on the directory**, not on the
file. Giving the scheduler write access to the data directory root would
therefore also let it replace `internal_tokens.json`, the machine-to-machine
credential map. The dedicated directory keeps "both may write here, nowhere
else" true.

Delivery is **at-most-once**, and the state file is the only thing that
makes that true:

- every attempt is written to `notification_state.json` **before** the LINE
  API is called, so a crash between "sent" and "recorded" leaves a
  `sending` marker instead of an empty slot — the next round does not send
  again (a lost message is preferred over a duplicate one);
- a state file that cannot be read, is not valid JSON, or contains a
  malformed **record** (a reminder whose `channels` entry has the wrong
  type, a channel state with no `status`, a digest entry that is not an
  object — anything that would make "did this already go out?" unanswerable)
  **stops** the scheduler instead of being treated as an empty history
  (which would re-send everything); a *missing* state file is a normal
  first run;
- the state file is written with `fsync` (file **and** directory entry). If
  the flush fails — full disk, I/O error — the scheduler stops before
  sending: a record that never reached the platter is not a record;
- an `flock` on `notification.lock` serialises whole runs, so two timer
  ticks can never claim the same notification.

**Recovering a corrupt state file**: stop the timer, back the file up,
repair it, then start the timer again — in that order.

```bash
sudo systemctl stop y8y-todo-notify.timer
sudo cp /var/lib/y8y-todo/notify/notification_state.json \
        /var/lib/y8y-todo/notify/notification_state.json.broken
# repair the file, then: python3 -m json.tool <file> > /dev/null
sudo systemctl start y8y-todo-notify.timer
```

Do **not** simply move the state file aside and let the scheduler rebuild
it: that throws away the entire dedup history, and every reminder that
already went out will be sent again.

## Backups, restore, upgrades

- **Backup**: the data directory is the whole state.
  `tar czf y8y-todo-$(date +%F).tar.gz -C /var/lib y8y-todo`
  (this includes the notification side-car files, so restored
  deployments do not re-send reminders that already went out)
- **Restore**: stop the service, extract the archive, start again.
- **Upgrade**: pull new code, restart the service. The on-disk format
  is additive and never rewritten by upgrades; historical Google
  Calendar association fields (`task_uid` / `gcal_event_id`) live in
  the tasks' frontmatter and survive.

The CI `linux-deploy-e2e` workflow verifies exactly this lifecycle
(fresh install → use → stop → restart → data intact) on Ubuntu 24.04
as a non-root user, using `scripts/e2e-api.py` — you can run the same
script against your own deployment. Its `systemd-e2e` job additionally
rehearses the systemd walkthrough above on a fresh runner: the packaged
unit is statically verified with `systemd-analyze verify`, then driven
for real with `systemctl enable --now / restart / stop` (service user,
state, interpreter, persistence all checked). The API jobs start the
service directly as the non-root user, on the Python version from the
CI matrix, and verify the running process's interpreter via
`/proc/<pid>/exe`.

The `ubuntu-2204` workflow repeats the verification on the **other
supported target** — Ubuntu 22.04 with its system Python 3.10 and
systemd 249 (full pytest, packaged unit under real systemd, a
standalone-Python 3.12 runtime via `uv`, and the Chromium browser E2E).

## Data format

Tasks are markdown files with frontmatter; completed tasks move to
`done/<date>/`; lists live in `lists/<id>/`; cross-device state in
`persistent_projects.json`. The format is intentionally simple and
inspectable — you can edit tasks with any text editor. Upgrades never
rewrite your data.

Since 2026-10 the **display title and the filename are separate**:

- New tasks store the exact title in the frontmatter `title` field and
  get a unique filename (`<slug>-<8-hex>.md`). Two tasks with the same
  title are two independent files — no silent overwrites.
- Renaming a task updates the frontmatter title and keeps the filename
  (and the task id) stable, so historical association fields and open
  editor sessions survive.
- Legacy files (title = filename, no `title` field) keep working
  unchanged: the filename is used as the display title.

### Recurring tasks (`recurring_dates`)

Recurring tasks live in `recurring/` and carry a `recurring` list. Each
entry is one of two formats (validated on write; invalid input is
rejected with `400`):

| Format | Example | Meaning |
| --- | --- | --- |
| `D` (day of month, 1–31) | `5`, `20` | day D of the **current due's month**; if past, the same day next year |
| `MM-DD` (yearly date) | `03-01`, `02-29` | every year on that date (must be a real calendar date) |

```yaml
---
title: 月底复盘
due: 2026-01-05
recurring:
  - 5
  - 20
---
```

Completing a recurring task rolls `due` to the next listed date and
keeps the task active (nothing is archived). If the rule cannot produce
a next date for the current due (e.g. rule `31` with the due in
February, or a hand-corrupted legacy rule), completion fails with a
clear `400` **and the task stays untouched and visible** — a broken
rule never silently strands a task in an invisible state.

Removed-feature data (`owner: legacy-agent` etc.) is never deleted and is
soft-hidden; see [docs/legacy-data.md](docs/legacy-data.md).

Notification preferences and delivery history live in three **separate**
JSON files in the data directory root (`notification_settings.json`,
`notification_state.json`, `notification_channels.json`) — see
[Notifications](#notifications-v1). They are additive side-cars: no task
file, list file or frontmatter field is touched, and an older version of
the app that knows nothing about notifications keeps working against the
same data directory unchanged.

## Development

**Working in this repo (human or AI agent)? Read [`AGENTS.md`](AGENTS.md) first** —
it is the entry point for the hard rules. The full how-to is in
[`docs/development.md`](docs/development.md); the release path is in
[`docs/release-process.md`](docs/release-process.md).

```bash
python3 -m pytest --basetemp=.pytest-tmp    # 385 tests (9 Linux-only: host-check process identity + cutover/rollback state machine)
```

The suite boots the real server on an ephemeral port with an isolated
data directory and covers task/list CRUD, recurring logic (format
validation, boundary/cross-year/leap-year, fail-loud completion), auth,
historical-data compatibility (a fictional legacy fixture), same-title
safety, concurrency, the y8ysystems API contract, data-dir isolation,
the removed-Google-Calendar contract (410 Gone endpoints, no network,
historical fields preserved), backup/restore hardening
(verify-before-write, same-filesystem staging, rollback on mid-write
failure, the public-proxy token boundary), the destructive-E2E safety
guard, and security regressions (path traversal, static guard).
The notification subsystem has its own suites (`tests/test_notifications.py`
and `tests/test_notification_policy.py`): channel providers, per-user
settings, delivery state, at-most-once semantics, idempotency, the
shared-task scope (a private task must never reach any digest or
reminder) and the digest/reminder window rules.
A browser-level verification script (`tests/browser_verify.py`, needs
`pip install playwright` and a running dev server) additionally checks
XSS rendering, same-title creation and renaming through the real UI.
`browser_verify.py` expects the dev server on **port 18086** by default
(historical; the app's own default is 8086) — override with
`Y8Y_VERIFY_PORT` / `Y8Y_VERIFY_BASE`. A second script,
`tests/browser_multiuser.py`, drives the real UI for **Alice and Bob
through a stdlib authenticating proxy** (identity isolation, shared
tasks, the Chinese-holiday preference toggle, list-ownership switching,
and the public-proxy token boundary). A third script,
`tests/browser_notify.py`, drives the **notification settings panel**
in the real UI (toggle notifications, pick channels, set the lead time
and the digest time, save, and reload to confirm the settings
persisted and stayed scoped to that user).

### Production upgrade rehearsal (old ↔ new data compatibility)

`scripts/p4-rehearsal.py` (stdlib only) rehearses a full production
cutover in isolation, using fictional legacy-format data: the **old
production baseline** (an anonymized, behavior-equivalent copy of the
old version — see `rehearsal/legacy-baseline/README.md`) reads and
writes the legacy data → backup + sha256
verification → data migration → the new version's full lifecycle →
browser smoke → pre/post snapshot diff → rollback drill A (restore the
backup; old version fully usable) → rollback drill B (old version reads
post-upgrade data without restore). The `p4-upgrade-rehearsal` CI
workflow runs the whole thing on a fresh Ubuntu 24.04 runner.

Findings and mitigations are documented in
[docs/upgrade-runbook.md](docs/upgrade-runbook.md) (release + rollback
runbook).

### Verifying a deployment end to end

Two stdlib-only scripts can be pointed at any running instance (CI
uses them on a fresh Ubuntu 24.04 install as a non-root user):

> **⚠️ These scripts mutate their target.** `e2e-api.py full` creates,
> completes, restores and advances tasks; `e2e-browser.py` creates a
> task through the real UI. Only ever point them at an **isolated test
> instance with fictional data** — never at a deployment that holds
> real tasks. `e2e-api.py persist` is read-only. As a safety net,
> `full` refuses to run when the instance already holds tasks for the
> given token (override with `Y8Y_E2E_ALLOW_NONEMPTY=1` if you really
> mean it).

Prerequisites: a running instance, and `<data_dir>/internal_tokens.json`
on it containing the token you pass, mapped to a user identity
(see [docs/deployment.md](docs/deployment.md) §4).

```bash
# full lifecycle: auth boundaries, tasks/recurring/projects/lists,
# complete/restore, y8ysystems contract, disk layout.
# Use a throwaway data dir like /tmp/y8y-test-data, NOT a real one.
python3 scripts/e2e-api.py full \
    http://127.0.0.1:8086 you@example.com YOUR-TOKEN-HERE \
    /tmp/e2e-state.json /tmp/y8y-test-data

# after a restart of the service: everything must still be intact
python3 scripts/e2e-api.py persist \
    http://127.0.0.1:8086 you@example.com YOUR-TOKEN-HERE \
    /tmp/e2e-state.json /tmp/y8y-test-data

# real-UI smoke (needs playwright + chromium) against a dev instance
python3 scripts/e2e-browser.py http://127.0.0.1:8086
```

See `.github/workflows/linux-deploy-e2e.yml` for the complete CI
rehearsal (fresh clone → install → non-root service → restart).

## Project layout

```
src/            backend + frontend (serve.py, index.html, config.py)
                + taskstore.py (shared task-file parsing)
                + notifications.py (notification engine) + notify_runner.py (scheduler CLI)
vendor/         vendored shared UI shell (tokens.css, icons, theme engine)
vendor/y8y-shared-chrome/fonts/
                bundled pixel font (Fusion Pixel, OFL-1.1) + its licenses
tests/          pytest suite + legacy fixture + browser verification
scripts/        E2E + P4 upgrade-rehearsal scripts (stdlib only)
packaging/      systemd units + env-file examples (non-root deployment)
docs/           deployment guide, generic upgrade runbook, legacy data guide
AGENTS.md       hard rules entry point for every agent working in this repo
docs/development.md       day-to-day dev loop, testing, PR template
docs/release-process.md   branch -> PR -> CI -> merge -> deploy, and rollback
```

## License

- **Software source code**: MIT License — see [LICENSE](LICENSE).
- **Official y8y branding and logos**: separately licensed — see
  [BRANDING.md](BRANDING.md) and
  [`vendor/y8y-shared-chrome/brand/LICENSE`](vendor/y8y-shared-chrome/brand/LICENSE).
  The official logo is **not** MIT-licensed open-source material.
- **UI icons**: [Pixelarticons](https://github.com/halfmage/pixelarticons)
  (MIT); attribution and licenses in
  [THIRD_PARTY_LICENSES.md](THIRD_PARTY_LICENSES.md).
- **UI font**: [Fusion Pixel Font](https://github.com/TakWolf/fusion-pixel-font)
  (SIL OFL 1.1), bundled with the app — see
  [`vendor/y8y-shared-chrome/fonts/SOURCES.txt`](vendor/y8y-shared-chrome/fonts/SOURCES.txt)
  and [THIRD_PARTY_LICENSES.md](THIRD_PARTY_LICENSES.md).

The code is open source; the official brand is not. A full map of what is
licensed under what lives in [NOTICE](NOTICE).
