#!/usr/bin/env bash
set -eEuo pipefail

stage="${1:?provide the uploaded patch directory}"
expected_pvp="${2:?provide the installed PvP server hash}"
expected_tournament="${3:?provide the installed tournament server hash}"
expected_js="${4:?provide the installed tournament app.js hash}"
expected_css="${5:?provide the installed tournament app.css hash}"
expected_builder="${6:?provide the installed replay builder hash}"

pvp_target="/opt/ygo-pvp/app/pvp/server.py"
tournament_target="/opt/ygo-tournament/app/tournament/server.py"
js_target="/opt/ygo-tournament/app/tournament/static/app.js"
css_target="/opt/ygo-tournament/app/tournament/static/app.css"
builder_target="/opt/ygo-evolution/tools/build_partial_series_replay.py"

test -f "$stage/pvp/server.py"
test -f "$stage/tournament/server.py"
test -f "$stage/tournament/static/app.js"
test -f "$stage/tournament/static/app.css"
test -f "$stage/tools/build_partial_series_replay.py"

stamp="$(date -u +%Y%m%dT%H%M%SZ)"
backup="/opt/ygo-evolution/backups/admin-settlement-replay-$stamp"
pvp_state="$(sudo -n systemctl is-active ygo-pvp 2>/dev/null || true)"
tournament_state="$(
    sudo -n systemctl is-active ygo-tournament 2>/dev/null || true
)"
mutation_started=0
committed=0

case "$pvp_state:$tournament_state" in
    active:active|active:inactive|inactive:active|inactive:inactive) ;;
    *)
        echo "refusing deployment with unstable service state" >&2
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
    raise SystemExit(
        "refusing deployment while tournament players are connected"
    )
PY
}

assert_baseline() {
    test "$(
        sudo -n sha256sum "$pvp_target" | cut -d' ' -f1
    )" = "$expected_pvp"
    test "$(
        sudo -n sha256sum "$tournament_target" | cut -d' ' -f1
    )" = "$expected_tournament"
    test "$(
        sudo -n sha256sum "$js_target" | cut -d' ' -f1
    )" = "$expected_js"
    test "$(
        sudo -n sha256sum "$css_target" | cut -d' ' -f1
    )" = "$expected_css"
    test "$(
        sudo -n sha256sum "$builder_target" | cut -d' ' -f1
    )" = "$expected_builder"
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
    sudo -n systemctl stop ygo-pvp ygo-tournament || failed=1
    if sudo -n test -d "$backup"; then
        sudo -n cp -a "$backup/pvp-server.py" "$pvp_target" || failed=1
        sudo -n cp -a "$backup/tournament-server.py" \
            "$tournament_target" || failed=1
        sudo -n cp -a "$backup/app.js" "$js_target" || failed=1
        sudo -n cp -a "$backup/app.css" "$css_target" || failed=1
        sudo -n cp -a "$backup/build_partial_series_replay.py" \
            "$builder_target" || failed=1
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
    "$stage/pvp/server.py" \
    "$stage/tools/build_partial_series_replay.py"
sudo -n /opt/ygo-tournament/venv/bin/python -m py_compile \
    "$stage/tournament/server.py"
sudo -n /opt/node-v24.14.0/bin/node --check \
    "$stage/tournament/static/app.js"
grep -q '"/api/sessions/{session_id}/seal"' "$stage/pvp/server.py"
grep -q 'force-finish' "$stage/tournament/server.py"
grep -q 'data-force-finish-form' "$stage/tournament/static/app.js"
grep -q 'recovered series replay identity' "$stage/pvp/server.py"

sudo -n install -d -m 700 "$backup"
sudo -n cp -a "$pvp_target" "$backup/pvp-server.py"
sudo -n cp -a "$tournament_target" "$backup/tournament-server.py"
sudo -n cp -a "$js_target" "$backup/app.js"
sudo -n cp -a "$css_target" "$backup/app.css"
sudo -n cp -a "$builder_target" \
    "$backup/build_partial_series_replay.py"

assert_idle
assert_baseline
mutation_started=1
sudo -n systemctl stop ygo-pvp ygo-tournament
assert_idle
assert_baseline

sudo -n install -m 640 -o ygo-pvp -g ygo-pvp \
    "$stage/pvp/server.py" "$pvp_target"
sudo -n install -m 644 -o ygo-tournament -g ygo-tournament \
    "$stage/tournament/server.py" "$tournament_target"
sudo -n install -m 644 -o ygo-tournament -g ygo-tournament \
    "$stage/tournament/static/app.js" "$js_target"
sudo -n install -m 644 -o ygo-tournament -g ygo-tournament \
    "$stage/tournament/static/app.css" "$css_target"
sudo -n install -m 755 -o root -g root \
    "$stage/tools/build_partial_series_replay.py" "$builder_target"

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
        | grep -q 'data-force-finish-form'
fi

test "$(
    sudo -n sha256sum "$pvp_target" | cut -d' ' -f1
)" = "$(sha256sum "$stage/pvp/server.py" | cut -d' ' -f1)"
test "$(
    sudo -n sha256sum "$tournament_target" | cut -d' ' -f1
)" = "$(sha256sum "$stage/tournament/server.py" | cut -d' ' -f1)"
test "$(
    sudo -n sha256sum "$js_target" | cut -d' ' -f1
)" = "$(sha256sum "$stage/tournament/static/app.js" | cut -d' ' -f1)"
test "$(
    sudo -n sha256sum "$css_target" | cut -d' ' -f1
)" = "$(sha256sum "$stage/tournament/static/app.css" | cut -d' ' -f1)"
test "$(
    sudo -n sha256sum "$builder_target" | cut -d' ' -f1
)" = "$(
    sha256sum "$stage/tools/build_partial_series_replay.py" | cut -d' ' -f1
)"

sudo -n -u ygo-pvp test -r \
    /opt/ygo-evolution/tools/recover_evrp_from_yrp.js
sudo -n -u ygo-pvp test -x /opt/node-v24.14.0/bin/node
committed=1
printf 'backup=%s\n' "$backup"
