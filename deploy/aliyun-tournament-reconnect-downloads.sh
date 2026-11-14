#!/usr/bin/env bash
set -euo pipefail

stage="${1:?provide the uploaded patch directory}"
allow_recent_disconnected="${ALLOW_RECENT_DISCONNECTED:-0}"
force_live_deploy="${FORCE_LIVE_DEPLOY:-0}"

pvp_source="$stage/pvp/server.py"
replay_source="$stage/pvp/replay.py"
tournament_source="$stage/tournament/server.py"
app_source="$stage/tournament/static/app.js"
css_source="$stage/tournament/static/app.css"
waiting_source="$stage/YGOProWaitingState.js"
dueling_source="$stage/YGOProDuelingState.js"
evrp_source="$stage/EvrpSerializer.js"

pvp_target="/opt/ygo-pvp/app/pvp/server.py"
replay_target="/opt/ygo-pvp/app/pvp/replay.py"
tournament_target="/opt/ygo-tournament/app/tournament/server.py"
app_target="/opt/ygo-tournament/app/tournament/static/app.js"
css_target="/opt/ygo-tournament/app/tournament/static/app.css"
waiting_target="/opt/ygo-evolution/dist/src/ygopro/room/domain/states/YGOProWaitingState.js"
dueling_target="/opt/ygo-evolution/dist/src/ygopro/room/domain/states/YGOProDuelingState.js"
evrp_target="/opt/ygo-evolution/dist/src/ygopro/room/domain/replay/EvrpSerializer.js"

for source in \
    "$pvp_source" \
    "$replay_source" \
    "$tournament_source" \
    "$app_source" \
    "$css_source" \
    "$waiting_source" \
    "$dueling_source" \
    "$evrp_source"; do
    test -f "$source"
done

target_hash() {
    local target="$1"
    if sudo -n test -f "$target"; then
        sudo -n sha256sum "$target" | awk '{print $1}'
    else
        printf '__missing__\n'
    fi
}

check_baseline() {
    local target="$1"
    local expected="$2"
    local source="$3"
    local actual deployed
    actual="$(target_hash "$target")"
    deployed="$(sha256sum "$source" | awk '{print $1}')"
    if [[ "$actual" != "$expected" && "$actual" != "$deployed" ]]; then
        printf 'deployment baseline changed: %s\nexpected=%s\nactual=%s\n' \
            "$target" "$expected" "$actual" >&2
        exit 1
    fi
}

check_baseline "$pvp_target" \
    "66132d151e6f60e2531e514401b6a5569b8e7caf1c3eaa23254921a793a36c8e" \
    "$pvp_source"
check_baseline "$replay_target" \
    "__missing__" \
    "$replay_source"
check_baseline "$tournament_target" \
    "56e29fce7dd5762e12db96c61eb5d3b855b2789da29d1006ec1ec9558175c104" \
    "$tournament_source"
check_baseline "$app_target" \
    "5189883acdbc75b0b5ffc7007eb205ff33ae5c017661eea100c1ff91005c7ee4" \
    "$app_source"
check_baseline "$css_target" \
    "137810d0c69fe6ee5f2a96d443d7ce8d30f6a343ebb4c194e9c0bf52837fcdb6" \
    "$css_source"
check_baseline "$waiting_target" \
    "5b2bbde760db6586669d90ae8f7939120b70b057fcbb33ab7659f3847fa5bab3" \
    "$waiting_source"
check_baseline "$dueling_target" \
    "a33fa601eb0e41a8c166dfa2c3b4f1cb2b1047260fa3fd890e3b2cea4241f6ce" \
    "$dueling_source"
check_baseline "$evrp_target" \
    "9d2fada02ee597ba919e042f604da25bb711eed71e2cbf4227641d31e1eec74d" \
    "$evrp_source"

needs_update() {
    local target="$1"
    local source="$2"
    [[ "$(target_hash "$target")" != \
       "$(sha256sum "$source" | awk '{print $1}')" ]]
}

pvp_changed=0
replay_changed=0
tournament_changed=0
tournament_static_changed=0
evolution_changed=0
needs_update "$pvp_target" "$pvp_source" && pvp_changed=1
needs_update "$replay_target" "$replay_source" && replay_changed=1
needs_update "$tournament_target" "$tournament_source" && tournament_changed=1
if needs_update "$app_target" "$app_source" \
    || needs_update "$css_target" "$css_source"; then
    tournament_static_changed=1
fi
if needs_update "$waiting_target" "$waiting_source" \
    || needs_update "$dueling_target" "$dueling_source" \
    || needs_update "$evrp_target" "$evrp_source"; then
    evolution_changed=1
fi

sudo -n env \
    ALLOW_RECENT_DISCONNECTED="$allow_recent_disconnected" \
    FORCE_LIVE_DEPLOY="$force_live_deploy" \
    python3 - <<'PY'
import glob
import json
import os
import time

active = []
now = time.time()
allow_recent_disconnected = os.environ.get("ALLOW_RECENT_DISCONNECTED") == "1"
force_live_deploy = os.environ.get("FORCE_LIVE_DEPLOY") == "1"
for path in sorted(glob.glob("/var/lib/ygo-pvp/s-*.json")):
    try:
        with open(path, encoding="utf-8") as stream:
            session = json.load(stream)
    except (OSError, ValueError):
        continue
    if session.get("status") not in {"waiting", "connecting", "ready", "dueling"}:
        continue
    connections = session.get("connections") or {}
    connected = {
        side: bool((connections.get(side) or {}).get("connected"))
        for side in ("a", "b")
    }
    updated_at = session.get("updated_at")
    recently_active = (
        isinstance(updated_at, (int, float))
        and now - updated_at < 15 * 60
    )
    if any(connected.values()) or (
        recently_active and not allow_recent_disconnected
    ):
        active.append({
            "id": session.get("id"),
            "room_id": session.get("room_id"),
            "status": session.get("status"),
            "game_number": session.get("game_number"),
            "connected": connected,
        })
if active:
    print(json.dumps(active, ensure_ascii=False, indent=2))
    if not force_live_deploy:
        raise SystemExit(
            "refusing deployment while a tournament session is connected or recently active"
        )
    print(
        "warning: FORCE_LIVE_DEPLOY=1; connected tournament clients will be disconnected"
    )
PY

sudo -n /opt/ygo-pvp/venv/bin/python -m py_compile \
    "$pvp_source" "$replay_source"
sudo -n /opt/ygo-tournament/venv/bin/python -m py_compile "$tournament_source"
sudo -n /opt/node-v24.14.0/bin/node --check "$app_source"
sudo -n /opt/node-v24.14.0/bin/node --check "$waiting_source"
sudo -n /opt/node-v24.14.0/bin/node --check "$dueling_source"
sudo -n /opt/node-v24.14.0/bin/node --check "$evrp_source"

stamp="$(date -u +%Y%m%dT%H%M%SZ)"
backup="/opt/ygo-tournament/backups/reconnect-downloads-$stamp"
sudo -n install -d -m 755 \
    "$backup/pvp" \
    "$backup/tournament/static" \
    "$backup/evolution"
sudo -n cp -a "$pvp_target" "$backup/pvp/server.py"
replay_existed=0
if sudo -n test -f "$replay_target"; then
    replay_existed=1
    sudo -n cp -a "$replay_target" "$backup/pvp/replay.py"
fi
sudo -n cp -a "$tournament_target" "$backup/tournament/server.py"
sudo -n cp -a "$app_target" "$backup/tournament/static/app.js"
sudo -n cp -a "$css_target" "$backup/tournament/static/app.css"
sudo -n cp -a "$waiting_target" "$backup/evolution/YGOProWaitingState.js"
sudo -n cp -a "$dueling_target" "$backup/evolution/YGOProDuelingState.js"
sudo -n cp -a "$evrp_target" "$backup/evolution/EvrpSerializer.js"

pvp_restarted=0
tournament_restarted=0
evolution_restarted=0

rollback() {
    status=$?
    trap - ERR
    sudo -n install -m 640 -o ygo-pvp -g ygo-pvp \
        "$backup/pvp/server.py" "$pvp_target"
    if [[ "$replay_existed" = 1 ]]; then
        sudo -n install -m 640 -o ygo-pvp -g ygo-pvp \
            "$backup/pvp/replay.py" "$replay_target"
    else
        sudo -n rm -f "$replay_target"
    fi
    sudo -n install -m 644 -o ygo-tournament -g ygo-tournament \
        "$backup/tournament/server.py" "$tournament_target"
    sudo -n install -m 644 -o ygo-tournament -g ygo-tournament \
        "$backup/tournament/static/app.js" "$app_target"
    sudo -n install -m 644 -o ygo-tournament -g ygo-tournament \
        "$backup/tournament/static/app.css" "$css_target"
    sudo -n install -m 644 -o ygo-evolution -g ygo-evolution \
        "$backup/evolution/YGOProWaitingState.js" "$waiting_target"
    sudo -n install -m 644 -o ygo-evolution -g ygo-evolution \
        "$backup/evolution/YGOProDuelingState.js" "$dueling_target"
    sudo -n install -m 644 -o ygo-evolution -g ygo-evolution \
        "$backup/evolution/EvrpSerializer.js" "$evrp_target"
    if [[ "$evolution_restarted" = 1 ]]; then
        sudo -n systemctl restart ygo-evolution
    fi
    if [[ "$pvp_restarted" = 1 ]]; then
        sudo -n systemctl restart ygo-pvp
    fi
    if [[ "$tournament_restarted" = 1 ]]; then
        sudo -n systemctl restart ygo-tournament
    fi
    printf 'deployment failed; restored backup=%s\n' "$backup" >&2
    exit "$status"
}
trap rollback ERR

sudo -n install -m 640 -o ygo-pvp -g ygo-pvp \
    "$pvp_source" "$pvp_target"
sudo -n install -m 640 -o ygo-pvp -g ygo-pvp \
    "$replay_source" "$replay_target"
sudo -n install -m 644 -o ygo-tournament -g ygo-tournament \
    "$tournament_source" "$tournament_target"
sudo -n install -m 644 -o ygo-tournament -g ygo-tournament \
    "$app_source" "$app_target"
sudo -n install -m 644 -o ygo-tournament -g ygo-tournament \
    "$css_source" "$css_target"
sudo -n install -m 644 -o ygo-evolution -g ygo-evolution \
    "$waiting_source" "$waiting_target"
sudo -n install -m 644 -o ygo-evolution -g ygo-evolution \
    "$dueling_source" "$dueling_target"
sudo -n install -m 644 -o ygo-evolution -g ygo-evolution \
    "$evrp_source" "$evrp_target"

sudo -n /opt/ygo-pvp/venv/bin/python -m py_compile \
    "$pvp_target" "$replay_target"
sudo -n /opt/ygo-tournament/venv/bin/python -m py_compile "$tournament_target"
sudo -n /opt/node-v24.14.0/bin/node --check "$app_target"
sudo -n /opt/node-v24.14.0/bin/node --check "$waiting_target"
sudo -n /opt/node-v24.14.0/bin/node --check "$dueling_target"
sudo -n /opt/node-v24.14.0/bin/node --check "$evrp_target"

if [[ "$evolution_changed" = 1 ]]; then
    evolution_restarted=1
    sudo -n systemctl restart ygo-evolution
fi
if [[ "$pvp_changed" = 1 || "$replay_changed" = 1 ]]; then
    pvp_restarted=1
    sudo -n systemctl restart ygo-pvp
fi
if [[ "$tournament_changed" = 1 ]]; then
    tournament_restarted=1
    sudo -n systemctl restart ygo-tournament
fi
sudo -n systemctl is-active ygo-evolution ygo-pvp ygo-tournament

healthy=0
for _ in $(seq 1 30); do
    if curl -fsS --max-time 2 http://127.0.0.1:8790/health >/dev/null 2>&1 \
        && curl -fsS --max-time 2 http://127.0.0.1:8787/health >/dev/null 2>&1 \
        && sudo -n ss -ltn | grep -q ':7911 '; then
        healthy=1
        break
    fi
    sleep 1
done
test "$healthy" = 1

trap - ERR
printf 'backup=%s\n' "$backup"
printf 'changed: pvp=%s replay=%s tournament=%s tournament_static=%s evolution=%s\n' \
    "$pvp_changed" \
    "$replay_changed" \
    "$tournament_changed" \
    "$tournament_static_changed" \
    "$evolution_changed"
