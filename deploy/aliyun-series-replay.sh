#!/usr/bin/env bash
set -euo pipefail

stage="${1:?provide the uploaded patch directory}"
test -f "$stage/pvp/server.py"
test -f "$stage/pvp/protocol.py"
test -f "$stage/YGOProRoom.js"
test -f "$stage/SeriesReplayView.js"
test -f "$stage/YGOProWaitingState.js"
test -f "$stage/EvrpSerializer.js"
test -f "$stage/DisconnectHandler.js"

sudo -n python3 - <<'PY'
import glob
import json

active = []
for path in sorted(glob.glob("/var/lib/ygo-pvp/s-*.json")):
    with open(path, encoding="utf-8") as stream:
        session = json.load(stream)
    connected = session.get("connections") or {}
    live = {
        side: bool((connected.get(side) or {}).get("connected"))
        for side in ("a", "b")
    }
    if any(live.values()):
        active.append({
            "id": session.get("id"),
            "room_id": session.get("room_id"),
            "status": session.get("status"),
            "connected": live,
        })
if active:
    print(json.dumps(active, ensure_ascii=False, indent=2))
    raise SystemExit("refusing deployment while tournament players are connected")
PY

stamp="$(date -u +%Y%m%dT%H%M%SZ)"
backup="/opt/ygo-evolution/backups/series-replay-$stamp"
sudo -n install -d -m 755 "$backup/pvp" "$backup/evolution"

for mapping in \
    "pvp/server.py:/opt/ygo-pvp/app/pvp/server.py" \
    "pvp/protocol.py:/opt/ygo-pvp/app/pvp/protocol.py" \
    "SeriesReplayView.js:/opt/ygo-evolution/dist/src/ygopro/room/domain/SeriesReplayView.js" \
    "YGOProRoom.js:/opt/ygo-evolution/dist/src/ygopro/room/domain/YGOProRoom.js" \
    "YGOProWaitingState.js:/opt/ygo-evolution/dist/src/ygopro/room/domain/states/YGOProWaitingState.js" \
    "EvrpSerializer.js:/opt/ygo-evolution/dist/src/ygopro/room/domain/replay/EvrpSerializer.js" \
    "DisconnectHandler.js:/opt/ygo-evolution/dist/src/shared/room/application/DisconnectHandler.js"; do
    source="${mapping%%:*}"
    target="${mapping#*:}"
    if [[ "$source" == pvp/* ]]; then
        backup_target="$backup/$source"
    else
        backup_target="$backup/evolution/$source"
    fi
    if sudo -n test -f "$target"; then
        sudo -n cp -a "$target" "$backup_target"
    elif [[ "$source" != "SeriesReplayView.js" ]]; then
        printf 'missing deployment target: %s\n' "$target" >&2
        exit 1
    fi
done

sudo -n install -m 644 -o ygo-pvp -g ygo-pvp \
    "$stage/pvp/server.py" /opt/ygo-pvp/app/pvp/server.py
sudo -n install -m 644 -o ygo-pvp -g ygo-pvp \
    "$stage/pvp/protocol.py" /opt/ygo-pvp/app/pvp/protocol.py
sudo -n install -m 644 -o ygo-evolution -g ygo-evolution \
    "$stage/SeriesReplayView.js" \
    /opt/ygo-evolution/dist/src/ygopro/room/domain/SeriesReplayView.js
sudo -n install -m 644 -o ygo-evolution -g ygo-evolution \
    "$stage/YGOProRoom.js" \
    /opt/ygo-evolution/dist/src/ygopro/room/domain/YGOProRoom.js
sudo -n install -m 644 -o ygo-evolution -g ygo-evolution \
    "$stage/YGOProWaitingState.js" \
    /opt/ygo-evolution/dist/src/ygopro/room/domain/states/YGOProWaitingState.js
sudo -n install -m 644 -o ygo-evolution -g ygo-evolution \
    "$stage/EvrpSerializer.js" \
    /opt/ygo-evolution/dist/src/ygopro/room/domain/replay/EvrpSerializer.js
sudo -n install -m 644 -o ygo-evolution -g ygo-evolution \
    "$stage/DisconnectHandler.js" \
    /opt/ygo-evolution/dist/src/shared/room/application/DisconnectHandler.js

sudo -n install -d -m 755 -o ygo-pvp -g ygo-pvp /var/lib/ygo-pvp/replays

sudo -n /opt/ygo-pvp/venv/bin/python -m py_compile \
    /opt/ygo-pvp/app/pvp/server.py \
    /opt/ygo-pvp/app/pvp/protocol.py
sudo -n /opt/node-v24.14.0/bin/node --check \
    /opt/ygo-evolution/dist/src/ygopro/room/domain/SeriesReplayView.js
sudo -n /opt/node-v24.14.0/bin/node --check \
    /opt/ygo-evolution/dist/src/ygopro/room/domain/YGOProRoom.js
sudo -n /opt/node-v24.14.0/bin/node --check \
    /opt/ygo-evolution/dist/src/ygopro/room/domain/states/YGOProWaitingState.js
sudo -n /opt/node-v24.14.0/bin/node --check \
    /opt/ygo-evolution/dist/src/ygopro/room/domain/replay/EvrpSerializer.js
sudo -n /opt/node-v24.14.0/bin/node --check \
    /opt/ygo-evolution/dist/src/shared/room/application/DisconnectHandler.js

sudo -n systemctl restart ygo-pvp
sudo -n systemctl restart ygo-evolution
sudo -n systemctl is-active ygo-pvp ygo-evolution

healthy=0
for _ in $(seq 1 30); do
    if curl -fsS --max-time 2 http://127.0.0.1:8790/health >/dev/null 2>&1; then
        healthy=1
        break
    fi
    sleep 1
done
test "$healthy" = 1
printf 'backup=%s\n' "$backup"
