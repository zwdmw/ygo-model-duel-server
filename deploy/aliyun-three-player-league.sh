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
    connected = {
        side: bool((connections.get(side) or {}).get("connected"))
        for side in ("a", "b")
    }
    if any(connected.values()):
        active.append({
            "id": session.get("id"),
            "room_id": session.get("room_id"),
            "connected": connected,
        })
if active:
    print(json.dumps(active, ensure_ascii=False, indent=2))
    raise SystemExit("refusing deployment while tournament players are connected")
PY

stamp="$(date -u +%Y%m%dT%H%M%SZ)"
backup="/opt/ygo-tournament/backups/three-player-league-$stamp"
sudo -n install -d -m 755 "$backup/static"
sudo -n cp -a \
    /opt/ygo-tournament/app/tournament/server.py \
    "$backup/server.py"
sudo -n cp -a \
    /opt/ygo-tournament/app/tournament/static/app.js \
    "$backup/static/app.js"

sudo -n install -m 644 -o ygo-tournament -g ygo-tournament \
    "$stage/server.py" /opt/ygo-tournament/app/tournament/server.py
sudo -n install -m 644 -o ygo-tournament -g ygo-tournament \
    "$stage/app.js" /opt/ygo-tournament/app/tournament/static/app.js

sudo -n /opt/ygo-tournament/venv/bin/python -m py_compile \
    /opt/ygo-tournament/app/tournament/server.py
sudo -n /opt/node-v24.14.0/bin/node --check \
    /opt/ygo-tournament/app/tournament/static/app.js

sudo -n systemctl restart ygo-tournament
sudo -n systemctl is-active ygo-tournament

healthy=0
for _ in $(seq 1 30); do
    if curl -fsS --max-time 2 http://127.0.0.1:8787/health >/dev/null 2>&1; then
        healthy=1
        break
    fi
    sleep 1
done
test "$healthy" = 1
printf 'backup=%s\n' "$backup"
