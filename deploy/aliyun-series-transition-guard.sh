#!/usr/bin/env bash
set -euo pipefail

stage="${1:?provide the uploaded patch directory}"
test -f "$stage/pvp/server.py"
test -f "$stage/YGOProRoom.js"
test -f "$stage/YGOProDuelingState.js"

pvp_target="/opt/ygo-pvp/app/pvp/server.py"
room_target="/opt/ygo-evolution/dist/src/ygopro/room/domain/YGOProRoom.js"
dueling_target="/opt/ygo-evolution/dist/src/ygopro/room/domain/states/YGOProDuelingState.js"

expected_pvp="d8bd66010a0d1a2176c1b671ac04ec5949a5788666c2c787f6d3a3e2b09c8cb4"
expected_room="e4f6ec0fcdcdbe67c747b24217415a0124151dc97794b74335ffdc29db3de4fd"
expected_dueling="ae12e7e9e059be21a1f54070ef11c284c79b5b482fca77cfd7aa35116049c972"

for item in \
    "$pvp_target:$expected_pvp:$stage/pvp/server.py" \
    "$room_target:$expected_room:$stage/YGOProRoom.js" \
    "$dueling_target:$expected_dueling:$stage/YGOProDuelingState.js"; do
    target="${item%%:*}"
    remainder="${item#*:}"
    expected="${remainder%%:*}"
    source="${remainder#*:}"
    actual="$(sudo -n sha256sum "$target" | awk '{print $1}')"
    deployed="$(sha256sum "$source" | awk '{print $1}')"
    if [[ "$actual" != "$expected" && "$actual" != "$deployed" ]]; then
        printf 'deployment baseline changed: %s\nexpected=%s\nactual=%s\n' \
            "$target" "$expected" "$actual" >&2
        exit 1
    fi
done

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
            "game_number": session.get("game_number"),
            "connected": connected,
        })
if active:
    print(json.dumps(active, ensure_ascii=False, indent=2))
    raise SystemExit("refusing deployment while tournament players are connected")
PY

stamp="$(date -u +%Y%m%dT%H%M%SZ)"
backup="/opt/ygo-evolution/backups/series-transition-$stamp"
sudo -n install -d -m 755 "$backup/pvp" "$backup/evolution"
sudo -n cp -a "$pvp_target" "$backup/pvp/server.py"
sudo -n cp -a "$room_target" "$backup/evolution/YGOProRoom.js"
sudo -n cp -a "$dueling_target" "$backup/evolution/YGOProDuelingState.js"

sudo -n install -m 640 -o ygo-pvp -g ygo-pvp \
    "$stage/pvp/server.py" "$pvp_target"
sudo -n install -m 644 -o ygo-evolution -g ygo-evolution \
    "$stage/YGOProRoom.js" "$room_target"
sudo -n install -m 644 -o ygo-evolution -g ygo-evolution \
    "$stage/YGOProDuelingState.js" "$dueling_target"

sudo -n /opt/ygo-pvp/venv/bin/python -m py_compile "$pvp_target"
sudo -n /opt/node-v24.14.0/bin/node --check "$room_target"
sudo -n /opt/node-v24.14.0/bin/node --check "$dueling_target"

sudo -n systemctl restart ygo-pvp ygo-evolution
sudo -n systemctl is-active ygo-pvp ygo-evolution

healthy=0
for _ in $(seq 1 30); do
    if curl -fsS --max-time 2 http://127.0.0.1:8790/health >/dev/null 2>&1 \
        && sudo -n ss -ltnp | grep -q ':7911 '; then
        healthy=1
        break
    fi
    sleep 1
done
test "$healthy" = 1

printf 'backup=%s\n' "$backup"
