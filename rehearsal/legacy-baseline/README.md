# Legacy baseline — what this is and why it is vendored

This directory contains an **old version** of y8y Todo, vendored so the
upgrade/rollback rehearsal
(`.github/workflows/p4-upgrade-rehearsal.yml`) does **not** depend on
git history.

## Provenance

- Source: the pre-refactor production line of y8y Todo, preserved
  privately by the project maintainer. **This copy is ANONYMIZED**: all
  personal identity names that the original code hardcoded have been
  replaced with generic fictional identities, and the original
  deployment paths have been replaced with generic paths. It is a
  behavior-equivalent test baseline, **not** a byte-identical copy of
  any historical source tree.
- Anonymization mapping (kept in the private release workspace, never
  shipped): the two historical human users became `alice` / `bob`, the
  removed hardware-calendar integration owner became `legacy-agent`,
  and its machine relay identity became `legacy-bridge`. Permission
  semantics are unchanged — see "Behavior contract" below.
- Because the public repository starts from a clean snapshot (no
  private history), the baseline is kept here as plain files.
- The rehearsal treats this code as **read-only**: it is copied
  unmodified into an isolated runner location and exercised against
  fictional legacy-format data. This copy hardcodes
  `/var/lib/todolist-legacy` and
  `/var/lib/todolist-legacy/shared/chrome`; the CI job stages exactly
  those paths inside the ephemeral runner (never on a real host).

## Behavior contract (what anonymization must preserve)

The old version encodes a small permission model around the removed
integration. The anonymized copy keeps the structure exactly, with
generic names:

| Historical identity | Anonymized identity | Permission semantics (unchanged) |
|---|---|---|
| primary user | `alice` | own + `shared` tasks visible; default owner for legacy data without an `owner` field |
| second user | `bob` | own + `shared` tasks visible; cannot see `alice`'s private tasks |
| removed calendar integration owner | `legacy-agent` | tasks soft-hidden from everyone; only `alice` could see them historically |
| machine relay identity | `legacy-bridge` | may only touch `legacy-agent`-owned content; sees all lists; forced `owner: legacy-agent` on writes; cannot create lists |
| `shared` | `shared` | visible to all configured users |

Anyone modifying this baseline must keep these semantics intact; the
test suite (`tests/test_legacy_compat.py`,
`tests/test_production_data_shapes.py`) pins them.

## The two deliberate edits (sanitization record)

Two real internal domains appeared in the original baseline. Both were
replaced with reserved-domain placeholders:

| File | Original value (redacted) | Replacement | Exercised by the rehearsal? |
|---|---|---|---|
| `src/index.html` (brand header link) | `https://dash.<org-domain>/` | `https://example.com/` | No — the link is rendered, never followed |
| `src/serve.py` (Google OAuth callback URI) | `https://vps.<org-domain>/todo/api/google-calendar/oauth/callback` | `https://todo.example.com/todo/api/google-calendar/oauth/callback` | No — the removed Google feature is never invoked (no SDK, no network); the constant is only read at import |

Both values were internal hostnames of the original deployment. The
rehearsal never contacts either address, and the Google integration
itself is inert in this vendored copy (no SDK is installed anywhere in
CI, which the workflow asserts structurally).

## Allowed production-path references (itemized register)

The old version **hardcodes** the legacy deployment layout — this is
load-bearing for the rehearsal: the CI job stages exactly these paths
inside the ephemeral runner so the old code can run. These references
are intentional and must not be "cleaned" without also changing the
rehearsal's staging step:

| Where | Value | Kind |
|---|---|---|
| `src/serve.py` (`TODOLIST_DIR`) | `/var/lib/todolist-legacy` | functional hardcode (data dir) |
| `src/serve.py` (`sys.path` + comment) | `/var/lib/todolist-legacy/shared/chrome` | functional hardcode (shared chrome) |
| `src/serve.py` (comment) | `/var/lib/todolist` | descriptive (legacy symlink note) |
| `src/google_calendar.py` (docstring) | `/var/lib/todolist/google_calendar` | descriptive |
| `.github/workflows/p4-upgrade-rehearsal.yml` (`OLD_DATA`, staging steps) | `/var/lib/todolist-legacy`, `/var/lib/todolist-legacy/shared/chrome`, `/var/lib/todolist-legacy/src` | CI runner staging paths (ephemeral, never a real host) |

Everything else in this directory was scanned clean: no real
credentials (the Google credential files are never present — the
fixture's are fictional), no real email addresses, no IP addresses, no
personal identity names, no other domains (`googleapis.com/auth/calendar`
is Google's public API scope constant).

## Integrity

`MANIFEST.sha256` records the SHA-256 of every file here (paths
relative to this directory, `sha256sum -c` format). The CI job
verifies it before using the baseline, so accidental drift or
tampering fails the build. The manifest was regenerated after the
anonymization pass.

## Files

```
.gitignore                              legacy import-bundle ignore rules
src/serve.py                            old backend (hardcoded paths, anonymized identities)
src/google_calendar.py                  old Google Calendar sync client (inert; no SDK)
src/index.html                          old single-file SPA (domain sanitized, identities anonymized)
src/todolist-web.service                old systemd unit (User=root, reference only)
src/.gitignore                          legacy src-level ignore rules
vendor/y8y-shared-chrome/*.py           shared static-asset helpers (serve_asset, static_guard)
vendor/y8y-shared-chrome/*.css|*.js     shared UI shell assets
```

`EXPERIMENT_BRIEF.md` from the original bundle is intentionally **not**
included: it is internal handoff material, not needed by the rehearsal.
