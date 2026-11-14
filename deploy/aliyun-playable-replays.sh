#!/usr/bin/env bash
set -euo pipefail

stage="${1:?provide the uploaded patch directory}"
pvp_source="$stage/pvp/server.py"
replay_source="$stage/pvp/replay.py"
tournament_source="$stage/tournament/server.py"
pvp_target="/opt/ygo-pvp/app/pvp/server.py"
replay_target="/opt/ygo-pvp/app/pvp/replay.py"
tournament_target="/opt/ygo-tournament/app/tournament/server.py"

for source in "$pvp_source" "$replay_source" "$tournament_source"; do
    test -f "$source"
done

check_baseline() {
    local target="$1"
    local expected="$2"
    local source="$3"
    local actual deployed
    actual="$(sudo -n sha256sum "$target" | awk '{print $1}')"
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
check_baseline "$tournament_target" \
    "56e29fce7dd5762e12db96c61eb5d3b855b2789da29d1006ec1ec9558175c104" \
    "$tournament_source"

sudo -n python3 - <<'PY'
import glob
import json

active = []
for path in sorted(glob.glob("/var/lib/ygo-pvp/s-*.json")):
    try:
        with open(path, encoding="utf-8") as stream:
            session = json.load(stream)
    except (OSError, ValueError):
        continue
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

sudo -n /opt/ygo-pvp/venv/bin/python -m py_compile \
    "$pvp_source" "$replay_source"
sudo -n /opt/ygo-tournament/venv/bin/python -m py_compile \
    "$tournament_source"

stamp="$(date -u +%Y%m%dT%H%M%SZ)"
backup="/opt/ygo-tournament/backups/playable-replays-$stamp"
sudo -n install -d -m 755 "$backup/pvp" "$backup/tournament"
sudo -n cp -a "$pvp_target" "$backup/pvp/server.py"
sudo -n cp -a "$tournament_target" "$backup/tournament/server.py"
replay_existed=0
if sudo -n test -f "$replay_target"; then
    replay_existed=1
    sudo -n cp -a "$replay_target" "$backup/pvp/replay.py"
fi

rollback() {
    status=$?
    trap - ERR
    sudo -n install -m 640 -o ygo-pvp -g ygo-pvp \
        "$backup/pvp/server.py" "$pvp_target"
    sudo -n install -m 644 -o ygo-tournament -g ygo-tournament \
        "$backup/tournament/server.py" "$tournament_target"
    if [[ "$replay_existed" = 1 ]]; then
        sudo -n install -m 640 -o ygo-pvp -g ygo-pvp \
            "$backup/pvp/replay.py" "$replay_target"
    else
        sudo -n rm -f "$replay_target"
    fi
    sudo -n systemctl restart ygo-pvp ygo-tournament || true
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

sudo -n /opt/ygo-pvp/venv/bin/python -m py_compile \
    "$pvp_target" "$replay_target"
sudo -n /opt/ygo-tournament/venv/bin/python -m py_compile \
    "$tournament_target"

sudo -n systemctl restart ygo-pvp
sudo -n systemctl restart ygo-tournament
sudo -n systemctl is-active ygo-pvp ygo-tournament ygo-evolution

healthy=0
for _ in $(seq 1 30); do
    if curl -fsS --max-time 2 http://127.0.0.1:8790/health >/dev/null 2>&1 \
        && curl -fsS --max-time 2 http://127.0.0.1:8787/health >/dev/null 2>&1 \
        && sudo -n ss -ltn | grep -q ':6008 '; then
        healthy=1
        break
    fi
    sleep 1
done
test "$healthy" = 1

trap - ERR
printf 'backup=%s\n' "$backup"
sudo -n sha256sum "$pvp_target" "$replay_target" "$tournament_target"
