#!/usr/bin/env bash
set -eEuo pipefail

stage="${1:?provide the uploaded patch directory}"
candidate="${2:?provide the partial EVRP path}"
session_id="${3:?provide the PvP session id}"
identity="${4:-${candidate}.identity.json}"

test -f "$stage/pvp/server.py"
test -f "$stage/tournament/server.py"
test -f "$stage/tournament/static/app.js"
test -f "$stage/tools/build_partial_series_replay.py"
test -f "$stage/tools/recover_evrp_from_yrp.js"
test -f "$candidate"
test -f "$identity"
[[ "$session_id" == s-* ]]

stamp="$(date -u +%Y%m%dT%H%M%SZ)"
backup="/opt/ygo-evolution/backups/replay-status-maintenance-$stamp"
target="/var/lib/ygo-pvp/replays/${session_id}.evrp"
had_target=0
mutation_started=0
committed=0
pvp_state="$(sudo -n systemctl is-active ygo-pvp 2>/dev/null || true)"
tournament_state="$(
    sudo -n systemctl is-active ygo-tournament 2>/dev/null || true
)"

for state in "$pvp_state" "$tournament_state"; do
    case "$state" in
        active|inactive) ;;
        *)
            echo "refusing maintenance with unstable service state: $state" >&2
            exit 1
            ;;
    esac
done

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
        sudo -n cp -a \
            "$backup/code/pvp-server.py" \
            /opt/ygo-pvp/app/pvp/server.py || failed=1
        sudo -n cp -a \
            "$backup/code/tournament-server.py" \
            /opt/ygo-tournament/app/tournament/server.py || failed=1
        sudo -n cp -a \
            "$backup/code/tournament-app.js" \
            /opt/ygo-tournament/app/tournament/static/app.js || failed=1
        sudo -n cp -a \
            "$backup/data/session.json" \
            "/var/lib/ygo-pvp/${session_id}.json" || failed=1
        if [[ "$had_target" = 1 ]]; then
            sudo -n cp -a "$backup/data/existing.evrp" "$target" || failed=1
        else
            sudo -n rm -f -- "$target" || failed=1
        fi
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

sudo -n /opt/ygo-pvp/venv/bin/python -m py_compile \
    "$stage/pvp/server.py" \
    "$stage/tools/build_partial_series_replay.py"
sudo -n /opt/ygo-tournament/venv/bin/python -m py_compile \
    "$stage/tournament/server.py"
sudo -n /opt/node-v24.14.0/bin/node --check \
    "$stage/tournament/static/app.js"
sudo -n /opt/node-v24.14.0/bin/node --check \
    "$stage/tools/recover_evrp_from_yrp.js"

sudo -n env \
    STAGE_SERVER="$stage/pvp/server.py" \
    CANDIDATE="$candidate" \
    IDENTITY="$identity" \
    SESSION_ID="$session_id" \
    /opt/ygo-pvp/venv/bin/python - <<'PY'
import hashlib
import importlib.util
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, "/opt/ygo-pvp/app")
import pvp

spec = importlib.util.spec_from_file_location(
    "pvp.server_candidate", os.environ["STAGE_SERVER"]
)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)

session_id = os.environ["SESSION_ID"]
session = json.loads(
    (Path("/var/lib/ygo-pvp") / f"{session_id}.json").read_text()
)
candidate = Path(os.environ["CANDIDATE"])
identity = json.loads(Path(os.environ["IDENTITY"]).read_text())
payload = candidate.read_bytes()
envelope = module.validate_series_replay(payload)
expected = module.expected_series_duels(session)
required = {
    "session_id": session_id,
    "room_id": session.get("room_id"),
    "match_id": session.get("match_id"),
    "duels": len(envelope["duels"]),
    "expected_duels": expected,
    "sha256": hashlib.sha256(payload).hexdigest(),
}
for key, value in required.items():
    if identity.get(key) != value:
        raise SystemExit(f"candidate identity mismatch: {key}")
ordinals = identity.get("ordinals")
missing = identity.get("missing_ordinals")
if (
    session.get("status") != "finished"
    or session.get("series_finished") is not True
    or not isinstance(ordinals, list)
    or not isinstance(missing, list)
    or ordinals != sorted(set(ordinals))
    or missing != sorted(set(missing))
    or len(ordinals) != len(envelope["duels"])
    or sorted(ordinals + missing) != list(range(1, expected + 1))
    or set(ordinals).intersection(missing)
):
    raise SystemExit("candidate coverage is invalid")
plans = {
    item.get("game_number"): item
    for item in session.get("results", [])
    if isinstance(item, dict)
}
for ordinal, duel in zip(ordinals, envelope["duels"]):
    plan = plans.get(ordinal)
    if (
        not isinstance(plan, dict)
        or duel.get("gameNumber") != ordinal
        or duel.get("seed") != plan.get("seed")
    ):
        raise SystemExit(f"candidate game identity mismatch: {ordinal}")
PY

sudo -n install -d -m 700 "$backup/code" "$backup/data"
sudo -n cp -a \
    /opt/ygo-pvp/app/pvp/server.py \
    "$backup/code/pvp-server.py"
sudo -n cp -a \
    /opt/ygo-tournament/app/tournament/server.py \
    "$backup/code/tournament-server.py"
sudo -n cp -a \
    /opt/ygo-tournament/app/tournament/static/app.js \
    "$backup/code/tournament-app.js"
sudo -n cp -a \
    "/var/lib/ygo-pvp/${session_id}.json" \
    "$backup/data/session.json"
sudo -n cp -a "$candidate" "$backup/data/candidate.evrp"
sudo -n cp -a "$identity" "$backup/data/candidate.identity.json"
if sudo -n test -f "$target"; then
    had_target=1
    sudo -n cp -a "$target" "$backup/data/existing.evrp"
fi

mutation_started=1
sudo -n systemctl stop ygo-tournament ygo-pvp

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

sudo -n env \
    PYTHONPATH=/opt/ygo-pvp/app \
    CANDIDATE="$candidate" \
    IDENTITY="$identity" \
    SESSION_ID="$session_id" \
    /opt/ygo-pvp/venv/bin/python - <<'PY'
import hashlib
import json
import os
import time
from pathlib import Path

from pvp.server import atomic_bytes, atomic_json

session_id = os.environ["SESSION_ID"]
session_path = Path("/var/lib/ygo-pvp") / f"{session_id}.json"
target = Path("/var/lib/ygo-pvp/replays") / f"{session_id}.evrp"
candidate = Path(os.environ["CANDIDATE"])
identity = json.loads(Path(os.environ["IDENTITY"]).read_text())
session = json.loads(session_path.read_text())
payload = candidate.read_bytes()
atomic_bytes(target, payload, 0o644)
session["series_replay"] = {
    "path": str(target),
    "bytes": len(payload),
    "sha256": hashlib.sha256(payload).hexdigest(),
    "duels": identity["duels"],
    "partial": True,
    "ordinals": identity["ordinals"],
    "missing_ordinals": identity["missing_ordinals"],
    "recovered_from_yrp": True,
}
session["updated_at"] = time.time()
atomic_json(session_path, session)
PY
sudo -n chown \
    ygo-pvp:ygo-pvp \
    "$target" "/var/lib/ygo-pvp/${session_id}.json"
sudo -n chmod 644 "$target"
sudo -n chmod 600 "/var/lib/ygo-pvp/${session_id}.json"

restore_services

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

sudo -n env \
    PYTHONPATH=/opt/ygo-pvp/app \
    SESSION_ID="$session_id" \
    /opt/ygo-pvp/venv/bin/python - <<'PY'
import json
import os
from pathlib import Path

from pvp.server import (
    partial_series_replay_stored,
    series_replay_available,
    series_replay_status,
)

session_id = os.environ["SESSION_ID"]
session = json.loads(
    (Path("/var/lib/ygo-pvp") / f"{session_id}.json").read_text()
)
if not partial_series_replay_stored(session):
    raise SystemExit("partial replay storage verification failed")
if not series_replay_available(session):
    raise SystemExit("partial replay availability verification failed")
status = series_replay_status(session)
if (
    status.get("state") != "partial"
    or status.get("replay_count") != 19
    or status.get("expected_replay_count") != 20
    or status.get("missing_game_numbers") != [1]
):
    raise SystemExit("partial replay status verification failed")
print(json.dumps(status, ensure_ascii=False, indent=2))
PY

committed=1
printf 'backup=%s\n' "$backup"
