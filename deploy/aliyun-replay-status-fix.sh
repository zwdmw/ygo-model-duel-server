#!/usr/bin/env bash
set -euo pipefail

stage="${1:?provide the uploaded patch directory}"
test -f "$stage/pvp/server.py"
test -f "$stage/tournament/server.py"
test -f "$stage/tournament/static/app.js"
test -f "$stage/tools/build_partial_series_replay.py"
test -f "$stage/tools/recover_evrp_from_yrp.js"

assert_idle() {
    local connections
    connections="$(sudo -n ss -Hnt state established sport = :6009)"
    if [[ -n "$connections" ]]; then
        printf '%s\n' "$connections" >&2
        echo "refusing deployment while port 6009 has established connections" >&2
        return 1
    fi
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
            "match_id": session.get("match_id"),
            "connected": live,
        })
if active:
    print(json.dumps(active, ensure_ascii=False, indent=2))
    raise SystemExit("refusing deployment while tournament players are connected")
PY
}

assert_idle
sudo -n /opt/ygo-pvp/venv/bin/python -m py_compile "$stage/pvp/server.py"
sudo -n /opt/ygo-tournament/venv/bin/python -m py_compile \
    "$stage/tournament/server.py"
sudo -n /opt/node-v24.14.0/bin/node --check \
    "$stage/tournament/static/app.js"
sudo -n /opt/node-v24.14.0/bin/node --check \
    "$stage/tools/recover_evrp_from_yrp.js"
sudo -n /opt/ygo-pvp/venv/bin/python -m py_compile \
    "$stage/tools/build_partial_series_replay.py"

stamp="$(date -u +%Y%m%dT%H%M%SZ)"
backup="/opt/ygo-evolution/backups/replay-status-$stamp"
sudo -n install -d -m 700 \
    "$backup/pvp" "$backup/tournament/static" "$backup/tools"
sudo -n cp -a /opt/ygo-pvp/app/pvp/server.py "$backup/pvp/server.py"
sudo -n cp -a \
    /opt/ygo-tournament/app/tournament/server.py \
    "$backup/tournament/server.py"
sudo -n cp -a \
    /opt/ygo-tournament/app/tournament/static/app.js \
    "$backup/tournament/static/app.js"
if sudo -n test -f /opt/ygo-evolution/tools/recover_evrp_from_yrp.js; then
    sudo -n cp -a \
        /opt/ygo-evolution/tools/recover_evrp_from_yrp.js \
        "$backup/tools/recover_evrp_from_yrp.js"
fi
if sudo -n test -f /opt/ygo-evolution/tools/build_partial_series_replay.py; then
    sudo -n cp -a \
        /opt/ygo-evolution/tools/build_partial_series_replay.py \
        "$backup/tools/build_partial_series_replay.py"
fi

assert_idle
sudo -n install -m 640 -o ygo-pvp -g ygo-pvp \
    "$stage/pvp/server.py" /opt/ygo-pvp/app/pvp/server.py
sudo -n install -m 644 -o ygo-tournament -g ygo-tournament \
    "$stage/tournament/server.py" \
    /opt/ygo-tournament/app/tournament/server.py
sudo -n install -m 644 -o ygo-tournament -g ygo-tournament \
    "$stage/tournament/static/app.js" \
    /opt/ygo-tournament/app/tournament/static/app.js
sudo -n install -d -m 755 /opt/ygo-evolution/tools
sudo -n install -m 755 -o root -g root \
    "$stage/tools/recover_evrp_from_yrp.js" \
    /opt/ygo-evolution/tools/recover_evrp_from_yrp.js
sudo -n install -m 755 -o root -g root \
    "$stage/tools/build_partial_series_replay.py" \
    /opt/ygo-evolution/tools/build_partial_series_replay.py

sudo -n systemctl restart ygo-pvp ygo-tournament
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
printf 'backup=%s\n' "$backup"
