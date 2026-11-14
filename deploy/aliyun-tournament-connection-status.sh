#!/usr/bin/env bash
set -eEuo pipefail

stage="${1:?provide the uploaded patch directory}"
expected_pvp="${2:?provide the installed PvP server hash}"
expected_tournament="${3:?provide the installed tournament server hash}"
expected_js="${4:?provide the installed tournament app.js hash}"
expected_css="${5:?provide the installed tournament app.css hash}"

test -f "$stage/pvp/server.py"
test -f "$stage/tournament/server.py"
test -f "$stage/tournament/static/app.js"
test -f "$stage/tournament/static/app.css"

stamp="$(date -u +%Y%m%dT%H%M%SZ)"
backup="/opt/ygo-evolution/backups/tournament-connection-status-$stamp"
pvp_state="$(sudo -n systemctl is-active ygo-pvp 2>/dev/null || true)"
tournament_state="$(
    sudo -n systemctl is-active ygo-tournament 2>/dev/null || true
)"
mutation_started=0
committed=0

case "$pvp_state:$tournament_state" in
    active:active|active:inactive|inactive:active|inactive:inactive) ;;
    *)
        echo "refusing deployment with unstable service state: pvp=$pvp_state tournament=$tournament_state" >&2
        exit 1
        ;;
esac

assert_idle() {
    if sudo -n ss -Hnt state established sport = :6009 | grep -q .; then
        sudo -n ss -Hnt state established sport = :6009 >&2
        echo "refusing deployment while native clients are connected" >&2
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
    connected = {
        side: bool((connections.get(side) or {}).get("connected"))
        for side in ("a", "b")
    }
    if any(connected.values()):
        active.append({
            "session_id": session.get("id"),
            "room_id": session.get("room_id"),
            "match_id": session.get("match_id"),
            "connected": connected,
        })
if active:
    print(json.dumps(active, ensure_ascii=False, indent=2))
    raise SystemExit("refusing deployment while tournament players are connected")
PY
}

assert_baseline() {
    test "$(
        sudo -n sha256sum /opt/ygo-pvp/app/pvp/server.py | cut -d' ' -f1
    )" = "$expected_pvp"
    test "$(
        sudo -n sha256sum /opt/ygo-tournament/app/tournament/server.py \
            | cut -d' ' -f1
    )" = "$expected_tournament"
    test "$(
        sudo -n sha256sum /opt/ygo-tournament/app/tournament/static/app.js \
            | cut -d' ' -f1
    )" = "$expected_js"
    test "$(
        sudo -n sha256sum /opt/ygo-tournament/app/tournament/static/app.css \
            | cut -d' ' -f1
    )" = "$expected_css"
}

restore_services() {
    if [[ "$pvp_state" = "active" ]]; then
        sudo -n systemctl start ygo-pvp
    else
        sudo -n systemctl stop ygo-pvp
    fi
    if [[ "$tournament_state" = "active" ]]; then
        sudo -n systemctl start ygo-tournament
    else
        sudo -n systemctl stop ygo-tournament
    fi
}

rollback() {
    local failed=0
    sudo -n systemctl stop ygo-tournament ygo-pvp || failed=1
    if sudo -n test -d "$backup"; then
        sudo -n cp -a "$backup/pvp-server.py" \
            /opt/ygo-pvp/app/pvp/server.py || failed=1
        sudo -n cp -a "$backup/tournament-server.py" \
            /opt/ygo-tournament/app/tournament/server.py || failed=1
        sudo -n cp -a "$backup/app.js" \
            /opt/ygo-tournament/app/tournament/static/app.js || failed=1
        sudo -n cp -a "$backup/app.css" \
            /opt/ygo-tournament/app/tournament/static/app.css || failed=1
    fi
    restore_services || failed=1
    return "$failed"
}

on_exit() {
    local status="$?"
    trap - EXIT
    if [[ "$status" != 0 && "$mutation_started" = 1 && "$committed" = 0 ]]; then
        rollback || true
    fi
    exit "$status"
}
trap on_exit EXIT

assert_idle
assert_baseline
sudo -n /opt/ygo-pvp/venv/bin/python -m py_compile \
    "$stage/pvp/server.py"
sudo -n /opt/ygo-tournament/venv/bin/python -m py_compile \
    "$stage/tournament/server.py"
sudo -n /opt/node-v24.14.0/bin/node --check \
    "$stage/tournament/static/app.js"
grep -q "PLAYER_STALL_WARNING_SECONDS" "$stage/pvp/server.py"
grep -q "connection_status" "$stage/tournament/server.py"
grep -q "playerConnectionState" "$stage/tournament/static/app.js"
grep -q ".player-connection-state.stalled" \
    "$stage/tournament/static/app.css"

sudo -n install -d -m 700 "$backup"
sudo -n cp -a /opt/ygo-pvp/app/pvp/server.py \
    "$backup/pvp-server.py"
sudo -n cp -a /opt/ygo-tournament/app/tournament/server.py \
    "$backup/tournament-server.py"
sudo -n cp -a /opt/ygo-tournament/app/tournament/static/app.js \
    "$backup/app.js"
sudo -n cp -a /opt/ygo-tournament/app/tournament/static/app.css \
    "$backup/app.css"

assert_idle
assert_baseline
mutation_started=1
sudo -n systemctl stop ygo-tournament ygo-pvp
assert_baseline
sudo -n install -m 640 -o ygo-pvp -g ygo-pvp \
    "$stage/pvp/server.py" \
    /opt/ygo-pvp/app/pvp/server.py
sudo -n install -m 644 -o ygo-tournament -g ygo-tournament \
    "$stage/tournament/server.py" \
    /opt/ygo-tournament/app/tournament/server.py
sudo -n install -m 644 -o ygo-tournament -g ygo-tournament \
    "$stage/tournament/static/app.js" \
    /opt/ygo-tournament/app/tournament/static/app.js
sudo -n install -m 644 -o ygo-tournament -g ygo-tournament \
    "$stage/tournament/static/app.css" \
    /opt/ygo-tournament/app/tournament/static/app.css

restore_services

if [[ "$pvp_state" = "active" ]]; then
    healthy=0
    for _ in $(seq 1 30); do
        if curl -fsS --max-time 2 \
            http://127.0.0.1:8790/health >/dev/null 2>&1; then
            healthy=1
            break
        fi
        sleep 1
    done
    test "$healthy" = 1
fi
if [[ "$tournament_state" = "active" ]]; then
    healthy=0
    for _ in $(seq 1 30); do
        if curl -fsS --max-time 2 \
            http://127.0.0.1:8787/health >/dev/null 2>&1; then
            healthy=1
            break
        fi
        sleep 1
    done
    test "$healthy" = 1
    curl -fsS http://127.0.0.1:8787/static/app.js \
        | grep -q "playerConnectionState"
    curl -fsS http://127.0.0.1:8787/static/app.css \
        | grep -q ".player-connection-state.stalled"
fi

committed=1
printf 'backup=%s\n' "$backup"
