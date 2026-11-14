#!/usr/bin/env bash
set -eEuo pipefail

candidate="${1:?provide the partial EVRP path}"
session_id="${2:?provide the PvP session id}"
identity="${3:-${candidate}.identity.json}"

test -f "$candidate"
test -f "$identity"
[[ "$session_id" == s-* ]]

pvp_state=""
stopped=0
committed=0
backup=""
target="/var/lib/ygo-pvp/replays/${session_id}.evrp"
had_existing=0

restore_service() {
    if [[ "$pvp_state" = "active" ]]; then
        sudo -n systemctl start ygo-pvp
    else
        sudo -n systemctl stop ygo-pvp
    fi
}

rollback() {
    if [[ -n "$backup" ]] && sudo -n test -d "$backup"; then
        sudo -n cp -a \
            "$backup/session.json" \
            "/var/lib/ygo-pvp/${session_id}.json"
        if [[ "$had_existing" = 1 ]]; then
            sudo -n cp -a "$backup/existing.evrp" "$target"
        else
            sudo -n rm -f -- "$target"
        fi
    fi
    if [[ "$stopped" = 1 ]]; then
        restore_service
    fi
}

on_exit() {
    local status="$?"
    trap - EXIT
    if [[ "$status" != 0 && "$committed" = 0 ]]; then
        rollback || true
    fi
    exit "$status"
}

trap on_exit EXIT

assert_idle() {
    local connections
    connections="$(sudo -n ss -Hnt state established sport = :6009)"
    if [[ -n "$connections" ]]; then
        printf '%s\n' "$connections" >&2
        echo "refusing partial replay recovery while port 6009 is active" >&2
        return 1
    fi
}

validate() {
    sudo -n env \
        PYTHONPATH=/opt/ygo-pvp/app \
        CANDIDATE="$candidate" \
        IDENTITY="$identity" \
        SESSION_ID="$session_id" \
        /opt/ygo-pvp/venv/bin/python - <<'PY'
import gzip
import hashlib
import json
import os
from pathlib import Path

from pvp.server import expected_series_duels, validate_series_replay

session_id = os.environ["SESSION_ID"]
session_path = Path("/var/lib/ygo-pvp") / f"{session_id}.json"
candidate = Path(os.environ["CANDIDATE"])
identity_path = Path(os.environ["IDENTITY"])
session = json.loads(session_path.read_text(encoding="utf-8"))
identity = json.loads(identity_path.read_text(encoding="utf-8"))
payload = candidate.read_bytes()
envelope = validate_series_replay(payload)
expected = expected_series_duels(session)
ordinals = identity.get("ordinals")
missing = identity.get("missing_ordinals")
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
        raise SystemExit(f"partial replay identity mismatch: {key}")
if (
    session.get("status") != "finished"
    or session.get("series_finished") is not True
    or not isinstance(ordinals, list)
    or not isinstance(missing, list)
    or ordinals != sorted(set(ordinals))
    or missing != sorted(set(missing))
    or len(ordinals) != len(envelope["duels"])
    or not ordinals
    or not missing
    or sorted(ordinals + missing) != list(range(1, expected + 1))
    or set(ordinals).intersection(missing)
):
    raise SystemExit("partial replay coverage is invalid")
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
        raise SystemExit(f"partial replay game identity mismatch: {ordinal}")
print(json.dumps({
    **required,
    "ordinals": ordinals,
    "missing_ordinals": missing,
}, ensure_ascii=False, indent=2))
PY
}

assert_idle
validate
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
backup="/opt/ygo-evolution/backups/partial-replay-${session_id}-${stamp}"
sudo -n install -d -m 700 "$backup"
sudo -n cp -a \
    "/var/lib/ygo-pvp/${session_id}.json" \
    "$backup/session.json"
sudo -n cp -a "$candidate" "$backup/candidate.evrp"
sudo -n cp -a "$identity" "$backup/candidate.identity.json"
if sudo -n test -f "$target"; then
    had_existing=1
    sudo -n cp -a "$target" "$backup/existing.evrp"
fi

pvp_state="$(sudo -n systemctl is-active ygo-pvp 2>/dev/null || true)"
case "$pvp_state" in
    active|inactive) ;;
    *)
        echo "refusing recovery with unstable ygo-pvp state: $pvp_state" >&2
        exit 1
        ;;
esac
sudo -n systemctl stop ygo-pvp
stopped=1
assert_idle
validate
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
candidate = Path(os.environ["CANDIDATE"])
identity = json.loads(Path(os.environ["IDENTITY"]).read_text(encoding="utf-8"))
session_path = Path("/var/lib/ygo-pvp") / f"{session_id}.json"
target = Path("/var/lib/ygo-pvp/replays") / f"{session_id}.evrp"
session = json.loads(session_path.read_text(encoding="utf-8"))
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
restore_service
stopped=0

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
    raise SystemExit("partial replay was not stored")
if not series_replay_available(session):
    raise SystemExit("partial replay is not available")
print(json.dumps(series_replay_status(session), indent=2))
PY
committed=1
printf 'backup=%s\n' "$backup"
