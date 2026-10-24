# Legacy Data: Removed Features (legacy-hardware-calendar / legacy-bridge / legacy-agent)

The legacy hardware-calendar integration, the `legacy-bridge` identity and the
`legacy-agent` task owner were **removed** from the application in P1/P2.
No compatibility layer or feature flag was kept — the code is gone.

One distinction matters for operations: `legacy-bridge` is gone as a
*configured user / feature*, but a `"legacy-bridge"` entry left inside
`internal_tokens.json` is still a working machine credential (the
token map does not consult `Y8Y_USERS`). See §5 before deciding what
to do with it.

**No data was deleted.** Any historical files that used the removed
identities remain on disk, untouched, and are handled as described below.

## What legacy data looks like

| Location | Legacy marker |
| --- | --- |
| `todo/<file>.md`, `recurring/<file>.md` | frontmatter `owner: legacy-agent` |
| `done/<date>/<file>.md` | frontmatter `owner: legacy-agent` |
| `lists/<id>/meta.json` | `"owner": "legacy-agent"` (unlikely, but possible) |
| `internal_tokens.json` | entries like `"legacy-bridge": "<hex>"` — **still a live machine credential** for the identity `legacy-bridge` (see §5) |

## How the current version treats it

- **Soft-hidden, never auto-deleted.** `legacy-agent` is no longer any
  configured user's identity, so `_user_can_access` can never match it:
  those tasks are invisible through the API to every user *and* to the
  machine token. Direct id access is refused (403).
- The files stay byte-for-byte on disk. Upgrades never rewrite or clean
  the data directory.
- The regression test `tests/test_legacy_compat.py::
  test_removed_owner_data_never_leaks` guards this behavior.

## Recommended manual handling (optional, your choice)

Because the store is plain markdown, all handling can be done with
normal file tools while the server is stopped (or any time — reads are
atomic and the files are inert):

### 1. Inspect what exists

```bash
DATA_DIR=/var/lib/y8y-todo   # or your Y8Y_DATA_DIR

grep -rl 'owner: legacy-agent' "$DATA_DIR" || echo 'no legacy-owner files'
grep -rl 'owner: legacy-agent' "$DATA_DIR" | wc -l
```

### 2. Option A — keep the files (do nothing)

They are invisible and harmless. Backup includes them, so the history
is preserved for later decisions.

### 3. Option B — reassign to a real user

```bash
# Example: make the tasks owned by "alice" again
grep -rl 'owner: legacy-agent' "$DATA_DIR" | while read -r f; do
  sed -i '' 's/^owner: legacy-agent$/owner: alice/' "$f"
done
```

Then the tasks appear normally (subject to the usual owner rules).
Stop the server first if you want a clean view; otherwise just reload
the page — list reads hit the filesystem per request.

### 4. Option C — archive outside the data dir

```bash
mkdir -p ~/y8y-archive
grep -rl 'owner: legacy-agent' "$DATA_DIR" | while read -r f; do
  rel="${f#$DATA_DIR/}"
  mkdir -p "$HOME/y8y-archive/$(dirname "$rel")"
  mv "$f" "$HOME/y8y-archive/$rel"
done
```

### 5. The legacy-bridge token entry (machine auth) — still functional

`internal_tokens.json` is the machine-token map: **any** entry
`{"<identity>": "<hex>"}` is a working `X-Internal-Token` credential
for that identity. The current server does *not* filter entries
against the configured users (`Y8Y_USERS`) — the token itself is the
secret (see `_current_user()` in `src/serve.py`). A leftover `"legacy-bridge"`
entry therefore authenticates as the identity `legacy-bridge`, which:

- **can**: read and write `shared` tasks and lists; create and manage
  tasks owned by `legacy-bridge`.
- **cannot**: see, modify, or delete any user's private tasks (403),
  or act as any configured user.

Verified on a real deployment (fictional rehearsal in
`tests/test_legacy_compat.py::test_legacy_bridge_token_entry_is_a_live_machine_credential`):
legacy-bridge token → HTTP 200, `current_user = "legacy-bridge"`, shared tasks
readable *and* writable, legacy-bridge-owned task creation → 201, private
task access → 403. So the entry is **not** inert and does **not**
"grant nothing" — an earlier version of this document claimed it did;
that claim was wrong and has been corrected.

**Revoking a stale token entry (e.g. `legacy-bridge`) — the token map is
loaded once, at process start**

`src/serve.py` loads the token map exactly once, at service startup
(`INTERNAL_TOKENS = _load_internal_tokens()`, module level); every
authentication consults that in-memory copy. There is no hot reload —
**editing `internal_tokens.json` does not affect a running service**:
an entry you just "removed" keeps working until the process restarts.
The complete revocation procedure:

1. **Confirm no consumer still uses the token.** Check every internal
   consumer of the API (y8ysystems and anything else that holds the
   credential) and, where possible, the access logs — then confirm with
   the consumer owners. Never assume a credential is unused.
2. **Modify the token file in an approved maintenance window.** Back
   the file up first, then edit `internal_tokens.json` (remove the
   stale entry, or rotate its value). It is plain JSON — keep it valid.
3. **Restart the affected Todo service** (`systemctl restart <unit>`)
   so the new map is loaded. Until this step the old token is still
   accepted — this is the step people skip.
4. **Verify**: the old token is now rejected (**401** — it no longer
   resolves to any identity), while normal users (reverse-proxy
   identity header) and the remaining internal consumers still work
   (**200** with the expected `current_user`).

Leaving a stale entry in place — even "just for now" — keeps a valid
shared-read/write credential alive with no expiry.

## Do NOT

- Do not bulk-delete files matching `owner: legacy-agent` without reviewing
  them first — they may contain real task history.
- Do not hand-edit files while trusting them to stay valid: keep the
  frontmatter structure (`---` fences, `key: value` lines).

## Compatibility summary

| Concern | Status |
| --- | --- |
| Legacy files deleted by upgrade? | Never |
| Legacy files readable after upgrade? | Only if re-owned (Option B) |
| API contract changed for legacy data? | No — same fields, same shapes |
| New deployments affected? | No — the identities simply don't exist |
