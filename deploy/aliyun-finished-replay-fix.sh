#!/usr/bin/env bash
set -euo pipefail

stage="${1:?provide the uploaded patch directory}"
test -f "$stage/server.py"
test -f "$stage/app.js"

sudo -n python3 - <<'PY'
import glob
import json

active = []
for path in sorted(glob.glob("/var/lib/ygo-pvp/s-*.json")):
    with open(path, encoding="utf-8") as stream:
        session = json.load(stream)
    connections = session.get("connections") or {}
    live = {
        side: bool((connections.get(side) or {}).get("connected"))
        for side in ("a", "b")
    }
    if any(live.values()):
        active.append({
            "id": session.get("id"),
            "room_id": session.get("room_id"),
            "connected": live,
        })
if active:
    print(json.dumps(active, ensure_ascii=False, indent=2))
    raise SystemExit("refusing deployment while tournament players are connected")
PY

stamp="$(date -u +%Y%m%dT%H%M%SZ)"
pvp_backup="/opt/ygo-pvp/backups/finished-replay-$stamp"
tournament_backup="/opt/ygo-tournament/backups/finished-replay-$stamp"
sudo -n install -d -m 755 "$pvp_backup" "$tournament_backup/static"
sudo -n cp -a /opt/ygo-pvp/app/pvp/server.py "$pvp_backup/server.py"
sudo -n cp -a \
    /opt/ygo-tournament/app/tournament/static/app.js \
    "$tournament_backup/static/app.js"

sudo -n install -m 644 -o ygo-pvp -g ygo-pvp \
    "$stage/server.py" /opt/ygo-pvp/app/pvp/server.py
sudo -n install -m 644 -o ygo-tournament -g ygo-tournament \
    "$stage/app.js" /opt/ygo-tournament/app/tournament/static/app.js

sudo -n /opt/ygo-pvp/venv/bin/python -m py_compile \
    /opt/ygo-pvp/app/pvp/server.py
sudo -n /opt/node-v24.14.0/bin/node --check \
    /opt/ygo-tournament/app/tournament/static/app.js

sudo -n systemctl restart ygo-pvp
sudo -n systemctl restart ygo-tournament
sudo -n systemctl is-active ygo-pvp ygo-tournament

for url in \
    http://127.0.0.1:8790/health \
    http://127.0.0.1:8787/health; do
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

printf 'pvp_backup=%s\n' "$pvp_backup"
printf 'tournament_backup=%s\n' "$tournament_backup"
