#!/usr/bin/env bash
set -euo pipefail

stage="${1:?provide the uploaded patch directory}"
test -f "$stage/ocgcore.js"
test -f "$stage/YGOProDuelingState.js"

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
            "status": session.get("status"),
            "connected": connected,
        })
if active:
    print(json.dumps(active, ensure_ascii=False, indent=2))
    raise SystemExit("refusing deployment while tournament players are connected")
PY

stamp="$(date -u +%Y%m%dT%H%M%SZ)"
backup="/opt/ygo-evolution/backups/natural-win-lifecycle-$stamp"
sudo -n install -d -m 755 "$backup"

core_target="/opt/ygo-evolution/dist/src/ygopro/ocgcore-worker/ocgcore.js"
state_target="/opt/ygo-evolution/dist/src/ygopro/room/domain/states/YGOProDuelingState.js"
sudo -n cp -a "$core_target" "$backup/ocgcore.js"
sudo -n cp -a "$state_target" "$backup/YGOProDuelingState.js"

sudo -n install -m 644 -o ygo-evolution -g ygo-evolution \
    "$stage/ocgcore.js" "$core_target"
sudo -n install -m 644 -o ygo-evolution -g ygo-evolution \
    "$stage/YGOProDuelingState.js" "$state_target"

sudo -n /opt/node-v24.14.0/bin/node --check "$core_target"
sudo -n /opt/node-v24.14.0/bin/node --check "$state_target"
sudo -n systemctl restart ygo-evolution
sudo -n systemctl is-active ygo-evolution
printf 'backup=%s\n' "$backup"
