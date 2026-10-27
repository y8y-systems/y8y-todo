#!/usr/bin/env bash
# Root-only CI fixture: disposable users, directories and fictional data.
set -euo pipefail
repo=$(cd "$(dirname "$0")/.." && pwd)
fixture=$(mktemp -d /tmp/todo-notify-acl.XXXXXX)
trap 'rm -rf "$fixture"' EXIT
web=y8y
notify=y8y-notify
install -d -o "$web" -g "$web" -m 0700 "$fixture/data"
runuser -u "$web" -- mkdir -p "$fixture/data/todo/old" "$fixture/data/recurring"
runuser -u "$web" -- sh -c 'printf "fictional\n" > "$1/todo/old/task.md"; printf "fictional\n" > "$1/recurring/task.md"; printf "fixture\n" > "$1/internal_tokens.json"; chmod 600 "$1/internal_tokens.json"' sh "$fixture/data"
install -d -o "$notify" -g "$notify" -m 2770 "$fixture/data/notify"
# Traverse the fixture and private data root, without allowing root listing.
chmod 0711 "$fixture"
setfacl -m u:y8y-notify:--x "$fixture/data"
find -P "$fixture/data/todo" "$fixture/data/recurring" -type d -exec setfacl -m u:y8y-notify:r-x,d:u:y8y-notify:r-x {} +
find -P "$fixture/data/todo" "$fixture/data/recurring" -type f -name '*.md' -exec setfacl -m u:y8y-notify:r-- {} +
runuser -u "$web" -- mkdir -p "$fixture/data/todo/new/deep"
runuser -u "$web" -- python3 - "$repo/src" "$fixture/data/todo/new/deep/task.md" <<'PY'
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
