#!/usr/bin/env bash
set -euo pipefail

stage="${1:?provide the uploaded patch directory}"
test -f "$stage/server.py"
test -f "$stage/app.js"

active_connections="$(
    sudo -n python3 - <<'PY'
import json
from pathlib import Path

count = 0
for path in Path("/var/lib/ygo-pvp").glob("*.json"):
    try:
        session = json.loads(path.read_text())
    except Exception:
        continue
    connections = session.get("connections") or {}
    count += sum(
        bool((connections.get(side) or {}).get("connected"))
        for side in ("a", "b")
    )
print(count)
PY
)"
if [[ "$active_connections" != "0" ]]; then
    printf 'refusing to restart with %s active player connections\n' \
        "$active_connections" >&2
    exit 1
fi

stamp="$(date -u +%Y%m%dT%H%M%SZ)"
pvp_backup="/opt/ygo-pvp/backups/seat-any-order-$stamp"
tournament_backup="/opt/ygo-tournament/backups/seat-any-order-$stamp"
sudo -n install -d -m 755 "$pvp_backup" "$tournament_backup"
sudo -n cp -a /opt/ygo-pvp/app/pvp/server.py "$pvp_backup/server.py"
sudo -n cp -a \
    /opt/ygo-tournament/app/tournament/static/app.js \
    "$tournament_backup/app.js"

sudo -n /opt/ygo-pvp/venv/bin/python -m py_compile "$stage/server.py"
sudo -n /opt/node-v24.14.0/bin/node --check "$stage/app.js"

sudo -n install -m 644 -o ygo-pvp -g ygo-pvp \
    "$stage/server.py" /opt/ygo-pvp/app/pvp/server.py
sudo -n install -m 644 -o ygo-tournament -g ygo-tournament \
    "$stage/app.js" /opt/ygo-tournament/app/tournament/static/app.js

sudo -n /opt/ygo-pvp/venv/bin/python -m py_compile \
    /opt/ygo-pvp/app/pvp/server.py
sudo -n /opt/node-v24.14.0/bin/node --check \
    /opt/ygo-tournament/app/tournament/static/app.js

sudo -n systemctl restart ygo-pvp ygo-tournament
for url in http://127.0.0.1:8790/health http://127.0.0.1:8787/health; do
    healthy=0
    for _ in $(seq 1 30); do
        if curl -fsS --max-time 2 "$url" >/dev/null 2>&1; then
            healthy=1
            break
        fi
        sleep 1
    done
    test "$healthy" = 1
done

sudo -n systemctl is-active ygo-pvp ygo-tournament
printf 'pvp_backup=%s\n' "$pvp_backup"
printf 'tournament_backup=%s\n' "$tournament_backup"
sudo -n sha256sum \
    /opt/ygo-pvp/app/pvp/server.py \
    /opt/ygo-tournament/app/tournament/static/app.js
