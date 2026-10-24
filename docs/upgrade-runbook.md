# Upgrade Runbook (generic)

A deployment-agnostic version of the upgrade procedure this project was
hardened with. It assumes you are migrating **from an older version of
this software** (any layout) to the current one, on a host you control,
with a reverse proxy in front.

**Nothing in this document is specific to any one deployment.** Paths,
hostnames, unit names and ports below are *generic defaults* — override
them via the environment variables documented in `docs/deployment.md`
and in the `p5-host-check.sh` header.

**Safety invariants (hold for every step):**

- The target-host identity gate (`scripts/p5-host-check.sh`) runs and
  passes before any stop-service / change-config / copy-or-delete-data
  step. It is read-only; any unmet condition fails closed.
- The legacy data directory is **never written, deleted or rewritten**
  by the new version. The two data directories stay physically
  isolated (the host check asserts it).
- No step deletes data unconditionally. Non-empty restore targets are
  preserved first (`scripts/p5-safe-restore.sh`).
- Everything is rehearsed on an isolated instance with fictional data
  before it touches production (the `p4-upgrade-rehearsal` CI workflow
  replays the full flow, including both rollback drills).

## Phase model (host-check)

| Phase | When | Must hold |
|---|---|---|
| `pre-deploy` | deployment prep | legacy service **running**; the legacy port's listener is really the legacy process (cmdline contains `serve.py`, expected user, belongs to the legacy code dir/alias); new unit not yet installed |
| `pre-cutover` | **before** the write freeze | legacy service still running + legacy port owned by it + isolated instance verified on the staging port + staging evidence + data-dir isolation |
| `cutover` | **after** the freeze (legacy service stopped) | legacy service **stopped** + port **released** + legacy data dir intact + staging evidence + data-dir isolation |
| `pre-rollback` | rollback prep | legacy-service state is informational only; the port may be free or held by a todo process (the version being rolled back — noted, not an error); only a **foreign** squatter fails; data dirs isolated |

## 0. Before the window

1. CI green (pytest / linux-deploy-e2e / ubuntu-2204 /
   p4-upgrade-rehearsal).
2. Record the legacy state (read-only): hostname, OS/Python, unit name,
   code dir, data dir, task count.
3. Run `scripts/p5-host-check.sh --phase pre-deploy` — it must PASS.
4. Stage the new code (from a reviewed commit; verify what you
   delivered, e.g. sha256 of the archive) and run `pre-deploy` again.

## 1. Isolated instance + read-only verification

1. Copy the legacy data **read-only** into the staging data dir
   (`rsync -a` / `cp -r`), owned by the service user.
2. Start the staging instance on its own port, bound to `127.0.0.1`
   only (unit + env like the production one, only port/data-dir differ —
   remember `ReadWritePaths` must point at the staging dir).
3. Verify the copy: `scripts/p5-verify-copy.py <legacy-dir>
   <staging-dir> --api http://127.0.0.1:<staging-port> --identity
   <user> --users <user,...>` — all PASS, exit 0. The default output
   never contains real filenames/titles (paths only behind
   `--diagnostic` in a fictional environment).
4. Optionally run the write-capable E2E against the **copy** (never
   against the real data dir).
5. Manual sanity check: counts match the recorded state; removed
   features' legacy files are still on disk (soft-hidden).
6. Gate: `scripts/p5-host-check.sh --phase pre-cutover` — PASS before
   starting the freeze.

## 2. Write freeze (in this order)

1. Pause internal consumers that write/read the service (stop their
   polling first — stop consumers before the service).
2. Put the reverse proxy into a maintenance mode for the app's paths
   (503) so browser writes stop.
3. Stop the legacy service and **confirm the port is really released**
   (`ss -tln`) — a leftover listener is the classic reason the new
   service cannot bind.

Never "freeze" by changing permissions, `kill -9`, or deleting data.

## 3. Snapshot (the rollback lifeline)

1. `scripts/p5-host-check.sh --phase cutover` — PASS (legacy stopped,
   port released, staging evidence, isolation).
2. `python3 scripts/p4-rehearsal.py backup <legacy-data-dir>
   <snapshot.tar.gz>` — must print the integrity verification PASS
   (per-file sha256, including any historical credential files).

Store the snapshot outside both data directories. From this point the
snapshot is the authoritative source for rollback scenario B.

## 4. Formal cutover

Preconditions (all required): freeze done (§2); snapshot integrity
PASS (§3); `host-check --phase cutover` PASS.

1. Restore the production data dir **from the verified snapshot**:
   `scripts/p5-safe-restore.sh <snapshot.tar.gz> <new-data-dir>
   [--original <legacy-dir>]`. The script verifies the snapshot in an
   isolated temp dir **before** touching the target; a non-empty
   target is preserved first (`<dest>-pre-restore-<TS>`); an existing
   preserve copy stops the run for human confirmation; symlinked
   targets are refused.
2. Install the new unit (same port as the legacy service so proxies
   and API consumers need no changes), disable the staging instance,
   start the new service, and confirm the listener is the new process
   (pid/user check).

## 5. Post-cutover verification

1. Port + process identity: `ss -tlnp` shows the new service's pid and
   user; `systemctl is-active` is active.
2. API contract: counts match the recorded state; response fields
   unchanged (print only counts/booleans — never task content).
3. Identity chain through the proxy: 200/401 behaviour as before.
4. Browser entry: open the UI through the proxy; lists/projects/
   recurring all render.
5. File count in the new data dir matches the pre-cutover count.

Only after everything passes: remove the maintenance page and resume
internal consumers. Any failure → §6.

## 6. Stop conditions and rollback

Immediate rollback signals: any §5 check fails; the new process cannot
stay up; task counts differ inexplicably; users report missing tasks.

### Scenario A — the new version has not written real data yet

1. `scripts/p5-host-check.sh --phase pre-rollback` (PASS; the new
   version may still hold the port — noted, not an error).
2. Stop the new service; confirm the port is released.
3. Start the legacy service — the legacy data dir was never written
   (physical isolation), no restore needed. Verify counts.
4. Keep the new data dir for analysis; do not delete it.

### Restore failure semantics (cross-filesystem safe)

The restore tool stages the verified snapshot **inside the target's own
filesystem** (a sibling `.restore-staging-<pid>` directory), re-verifies
every SHA-256 there, and only then switches files into place. So:

- `/tmp`-versus-target filesystem differences (tmpfs, separate mounts,
  containers) can no longer abort a restore mid-way with
  `OSError [Errno 18] Invalid cross-device link` — and no longer leave a
  *partial* restore behind.
- A mid-write failure (disk full, permissions, concurrent tampering)
  **rolls back completely**: the target is byte-identical to its
  pre-restore state — a failed restore never leaves a partial restore
  that looks successful.
- Corrupt snapshot / manifest mismatch / insufficient permissions all
  fail closed: the target is not touched at all.

### Scenario B — the new version has written real data

There is no lossless "switch back": tasks created/modified during the
new version's run exist only in the new data dir, and the legacy
version cannot safely interpret the new filename format.

- Prefer **fix-forward** (data is compatible).
- If you must go back: first preserve the new data dir
  (`cp -r <new-dir> <new-dir>-rolled-back-<TS>` — never overwrite),
  then stop the new service, confirm the port released, and either
  start the legacy service directly (if the legacy dir is untouched)
  or `p5-safe-restore.sh` the §3 snapshot into an empty dir and hand
  that to the legacy version. State clearly that changes made during
  the new version's run are not automatically restored.

### Rollback kit (prepare in advance)

```bash
sudo mkdir -p /var/backups/y8y-rollback-kit   # or your preferred location
cp scripts/p4-rehearsal.py scripts/p5-safe-restore.sh \
   scripts/p5-host-check.sh scripts/p5-verify-copy.py \
   /var/backups/y8y-rollback-kit/
git -C <new-code-dir> archive <release-commit> -o /var/backups/y8y-rollback-kit/new-code.tar
```

(`p5-safe-restore.sh` locates `p4-rehearsal.py` / `p5-verify-copy.py`
next to itself — copying all three into one directory satisfies that.)

## 7. After the upgrade

- Watch the service log for a clean startup summary (data dir, users).
- Take a full backup of the new data dir within 24h — it is now the
  single source of truth. Run your own backup strategy; this project
  does not manage one.
- Removed-feature data (see `docs/legacy-data.md`) is never deleted by
  upgrades; decide its fate deliberately.

## What the CI rehearses

- `p4-upgrade-rehearsal`: the full old→new→rollback flow on fictional
  legacy-format data, with the old version vendored in
  `rehearsal/legacy-baseline/` (manifest-verified), both rollback
  drills, and per-file credential verification.
- `linux-deploy-e2e` / `ubuntu-2204`: fresh install → non-root systemd
  service → restart persistence → real browser E2E, including the real
  target platform (Ubuntu 22.04, system Python 3.10, systemd 249).
