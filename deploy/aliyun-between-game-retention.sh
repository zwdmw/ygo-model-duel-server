#!/usr/bin/env bash
set -euo pipefail

stage="${1:?provide the uploaded patch directory}"
source_file="$stage/YGOProDuelingState.js"
target="/opt/ygo-evolution/dist/src/ygopro/room/domain/states/YGOProDuelingState.js"
node_bin="/opt/node-v24.14.0/bin/node"
expected="077768d72185754ad963910fd793ed7b962962128d7cbaac7bffc4e47136038e"
force_stale_deploy="${FORCE_STALE_DEPLOY:-0}"

test -f "$source_file"
"$node_bin" --check "$source_file"

actual="$(sudo -n sha256sum "$target" | awk '{print $1}')"
deployed="$(sha256sum "$source_file" | awk '{print $1}')"
if [[ "$actual" = "$deployed" ]]; then
    printf 'between-game retention patch already applied\n'
    exit 0
fi
if [[ "$actual" != "$expected" ]]; then
    printf 'deployment baseline changed: %s\nexpected=%s\nactual=%s\n' \
        "$target" "$expected" "$actual" >&2
    exit 1
fi

if [[ "$force_stale_deploy" != 1 ]] \
    && sudo -n ss -Hnt state established sport = :6009 | grep -q .; then
    sudo -n ss -Hnt state established sport = :6009
    printf 'refusing deployment while port 6009 has established connections\n' >&2
    exit 1
fi

FORCE_STALE_DEPLOY="$force_stale_deploy" sudo -n --preserve-env=FORCE_STALE_DEPLOY \
    python3 - <<'PY'
import glob
import json
import os
import time

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
            "last_activity_at": {
                side: (connections.get(side) or {}).get("last_activity_at")
                for side in ("a", "b")
            },
        })
if active:
    print(json.dumps(active, ensure_ascii=False, indent=2))
    if os.environ.get("FORCE_STALE_DEPLOY") != "1":
        raise SystemExit(
            "refusing deployment while tournament players are connected"
        )
    timestamp = time.time()
    for session in active:
        connected_sides = [
            side for side, value in session["connected"].items() if value
        ]
        if len(connected_sides) != 1:
            raise SystemExit(
                "forced deployment requires exactly one connected side per "
                "active session"
            )
        side = connected_sides[0]
        last_activity = session["last_activity_at"].get(side)
        if not isinstance(last_activity, (int, float)):
            raise SystemExit("connected side has no activity timestamp")
        if timestamp - last_activity < 300:
            raise SystemExit(
                "connected side is not stale enough for forced deployment"
            )
    print("forcing deployment across stale one-sided tournament connections")
PY

stamp="$(date -u +%Y%m%dT%H%M%SZ)"
backup="/opt/ygo-evolution/backups/between-game-retention-$stamp"
sudo -n install -d -m 755 "$backup"
sudo -n cp -a "$target" "$backup/YGOProDuelingState.js"

mutation_started=0
rollback() {
    if [[ "$mutation_started" = 1 ]]; then
        sudo -n install -m 644 -o ygo-evolution -g ygo-evolution \
            "$backup/YGOProDuelingState.js" "$target"
        sudo -n systemctl restart ygo-evolution
    fi
}
on_error() {
    status="$1"
    rollback
    exit "$status"
}
trap 'on_error "$?"' ERR

mutation_started=1
sudo -n install -m 644 -o ygo-evolution -g ygo-evolution \
    "$source_file" "$target"
sudo -n "$node_bin" --check "$target"
sudo -n systemctl restart ygo-evolution
sudo -n systemctl is-active --quiet ygo-evolution

healthy=0
for _ in $(seq 1 30); do
    if sudo -n ss -ltnp | grep -q ':7711 ' \
        && sudo -n ss -ltnp | grep -q ':7911 '; then
        healthy=1
        break
    fi
    sleep 1
done
test "$healthy" = 1

installed="$(sudo -n sha256sum "$target" | awk '{print $1}')"
test "$installed" = "$deployed"
mutation_started=0
trap - ERR

printf 'backup=%s\n' "$backup"
