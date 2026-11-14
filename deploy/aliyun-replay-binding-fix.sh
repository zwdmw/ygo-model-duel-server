#!/usr/bin/env bash
set -eEuo pipefail

stage="${1:?provide the uploaded patch directory}"
session_id="${2:?provide the finished session id}"
expected_pvp="${3:?provide the installed PvP code hash}"
expected_tournament="${4:?provide the installed tournament code hash}"
allow_waiting="${5:-0}"
[[ "$allow_waiting" = 0 || "$allow_waiting" = 1 ]]
[[ "$session_id" =~ ^s-[a-zA-Z0-9]+$ ]]
backup="/opt/ygo-evolution/backups/replay-bindings-$(date -u +%Y%m%dT%H%M%SZ)"
mutated=0
installed=0
committed=0

assert_idle() {
    sudo -n env ALLOW_WAITING="$allow_waiting" python3 - <<'PY'
import json, os, subprocess
from pathlib import Path
allow_waiting = os.environ['ALLOW_WAITING'] == '1'
waiting = 0
for path in Path('/var/lib/ygo-pvp').glob('s-*.json'):
    session = json.loads(path.read_text())
    connected = sum(bool(connection.get('connected')) for connection in session.get('connections', {}).values())
    if connected:
        if not allow_waiting or connected != 1 or session.get('game_started') or session.get('status') not in ('waiting', 'connecting'):
            raise SystemExit('refusing deployment while an active duel or prepared pair exists: ' + session['id'])
        waiting += connected
connections = subprocess.check_output(['ss', '-Hnt', 'state', 'established', 'sport', '=', ':6009'], text=True).splitlines()
if len(connections) > waiting:
    raise SystemExit('refusing deployment while spectator or unidentified connections exist')
print('preflight_waiting_connections=' + str(waiting))
PY
}

assert_baseline() {
    test "$(sudo -n sha256sum /opt/ygo-pvp/app/pvp/server.py | cut -d' ' -f1)" = "$expected_pvp"
    test "$(sudo -n sha256sum /opt/ygo-tournament/app/tournament/server.py | cut -d' ' -f1)" = "$expected_tournament"
}

on_exit() {
    local status="$?"
    trap - EXIT
    if [[ "$status" != 0 && "$mutated" = 1 && "$committed" = 0 ]]; then
        sudo -n systemctl stop ygo-tournament ygo-pvp || true
        if [[ "$installed" = 1 ]]; then
            sudo -n cp -a "$backup/code/pvp-server.py" /opt/ygo-pvp/app/pvp/server.py
            sudo -n cp -a "$backup/code/tournament-server.py" /opt/ygo-tournament/app/tournament/server.py
            sudo -n cp -a "$backup/data/$session_id.json" "/var/lib/ygo-pvp/$session_id.json"
        fi
        sudo -n systemctl start ygo-pvp ygo-tournament
    fi
    exit "$status"
}
trap on_exit EXIT

assert_idle
assert_baseline
sudo -n systemctl is-active --quiet ygo-pvp
sudo -n systemctl is-active --quiet ygo-tournament
sudo -n /opt/ygo-pvp/venv/bin/python -m py_compile "$stage/pvp/server.py" "$stage/tools/repair_replay_bindings.py"
sudo -n /opt/ygo-tournament/venv/bin/python -m py_compile "$stage/tournament/server.py"
sudo -n env PYTHONPATH="$stage" /opt/ygo-pvp/venv/bin/python \
    "$stage/tools/repair_replay_bindings.py" "/var/lib/ygo-pvp/$session_id.json"
sudo -n install -d -m 700 "$backup/code" "$backup/data"
sudo -n cp -a /opt/ygo-pvp/app/pvp/server.py "$backup/code/pvp-server.py"
sudo -n cp -a /opt/ygo-tournament/app/tournament/server.py "$backup/code/tournament-server.py"
sudo -n cp -a "/var/lib/ygo-pvp/$session_id.json" "$backup/data/$session_id.json"
assert_idle
assert_baseline
mutated=1
sudo -n systemctl stop ygo-tournament ygo-pvp
assert_baseline
installed=1
sudo -n install -m 640 -o ygo-pvp -g ygo-pvp "$stage/pvp/server.py" /opt/ygo-pvp/app/pvp/server.py
sudo -n install -m 644 -o ygo-tournament -g ygo-tournament "$stage/tournament/server.py" /opt/ygo-tournament/app/tournament/server.py
sudo -n install -m 755 "$stage/tools/repair_replay_bindings.py" /opt/ygo-evolution/tools/repair_replay_bindings.py
sudo -n env PYTHONPATH=/opt/ygo-pvp/app /opt/ygo-pvp/venv/bin/python \
    /opt/ygo-evolution/tools/repair_replay_bindings.py "/var/lib/ygo-pvp/$session_id.json" \
    --apply --backup-dir "$backup/data"
sudo -n chown ygo-pvp:ygo-pvp "/var/lib/ygo-pvp/$session_id.json"
sudo -n chmod 600 "/var/lib/ygo-pvp/$session_id.json"
sudo -n systemctl start ygo-pvp ygo-tournament
for url in http://127.0.0.1:8790/health http://127.0.0.1:8787/health; do
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
sudo -n env PYTHONPATH=/opt/ygo-pvp/app /opt/ygo-pvp/venv/bin/python \
    /opt/ygo-evolution/tools/repair_replay_bindings.py "/var/lib/ygo-pvp/$session_id.json"
sudo -n systemctl is-active ygo-pvp ygo-tournament ygo-evolution
committed=1
printf 'backup=%s\n' "$backup"
