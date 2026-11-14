#!/usr/bin/env bash
set -eEuo pipefail

mode="final"
if [[ "${1:-}" == "--prefix" ]]; then
    mode="prefix"
    shift
fi

candidate="${1:?provide the candidate EVRP path}"
session_id="${2:?provide the PvP session id}"
identity="${3:-${candidate}.identity.json}"
if [[ "$mode" == "prefix" ]]; then
    allow_replace="${ALLOW_REPLACE_REPLAY_PREFIX:-0}"
else
    allow_replace="${ALLOW_REPLACE_REPLAY:-0}"
fi

test -f "$candidate"
test -f "$identity"
[[ "$session_id" == s-* ]]

preflight_json="$(mktemp)"
trap 'rm -f -- "$preflight_json"' EXIT
failure_status=0
maintenance_entered=0
backup_ready=0
mutation_started=0
committed=0
backup=""
tournament_state=""
pvp_state=""
evolution_state=""

assert_no_live_connections() {
    local phase="$1"
    local tcp_connections
    tcp_connections="$(sudo -n ss -Hnt state established sport = :6009)"
    if [[ -n "$tcp_connections" ]]; then
        printf '%s\n' "$tcp_connections" >&2
        printf 'refusing replay recovery while port 6009 has established connections (%s)\n' \
            "$phase" >&2
        return 1
    fi

    sudo -n env CHECK_PHASE="$phase" python3 - <<'PY'
import glob
import json
import os

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
            "match_id": session.get("match_id"),
            "connected": connected,
        })
if active:
    print(json.dumps(active, ensure_ascii=False, indent=2))
    raise SystemExit(
        "refusing replay recovery while tournament players are connected "
        f"({os.environ['CHECK_PHASE']})"
    )
PY
}

validate_recovery() {
    local candidate_path="${1:-$candidate}"
    local identity_path="${2:-$identity}"
    sudo -n env \
        PYTHONPATH=/opt/ygo-pvp/app \
        RECOVERY_CANDIDATE="$candidate_path" \
        RECOVERY_IDENTITY="$identity_path" \
        RECOVERY_SESSION_ID="$session_id" \
        RECOVERY_MODE="$mode" \
        ALLOW_REPLACE_REPLAY="$allow_replace" \
        /opt/ygo-pvp/venv/bin/python - <<'PY'
import hashlib
import json
import os
from pathlib import Path

from pvp.server import expected_series_duels, validate_series_replay

candidate = Path(os.environ["RECOVERY_CANDIDATE"])
identity_path = Path(os.environ["RECOVERY_IDENTITY"])
session_id = os.environ["RECOVERY_SESSION_ID"]
mode = os.environ["RECOVERY_MODE"]
allow_replace = os.environ.get("ALLOW_REPLACE_REPLAY") == "1"
session_path = Path("/var/lib/ygo-pvp") / f"{session_id}.json"
suffix = "prefix.evrp" if mode == "prefix" else "evrp"
target = Path("/var/lib/ygo-pvp/replays") / f"{session_id}.{suffix}"

payload = candidate.read_bytes()
candidate_sha = hashlib.sha256(payload).hexdigest()
envelope = validate_series_replay(payload)
identity = json.loads(identity_path.read_text(encoding="utf-8"))
session = json.loads(session_path.read_text(encoding="utf-8"))

required_identity = {
    "session_id": session_id,
    "room_id": session.get("room_id"),
    "match_id": session.get("match_id"),
    "duels": len(envelope["duels"]),
    "sha256": candidate_sha,
}
for key, expected in required_identity.items():
    if identity.get(key) != expected:
        raise SystemExit(
            f"recovery identity mismatch for {key}: "
            f"expected={expected!r} actual={identity.get(key)!r}"
        )
if session.get("id") != session_id:
    raise SystemExit("session id mismatch")
if mode == "prefix":
    if session.get("continuous_series") is not True:
        raise SystemExit("replay prefix requires a continuous series")
    if session.get("series_replay") is not None:
        raise SystemExit("replay prefix cannot replace a final replay")
    game_number = session.get("game_number")
    initial_generation = session.get("initial_generation")
    if initial_generation is None:
        initial_generation = len(envelope["duels"]) + 1
    game_generation = session.get("game_generation")
    if (
        type(initial_generation) is not int
        or type(game_number) is not int
        or game_number < initial_generation
        or len(envelope["duels"]) != initial_generation - 1
    ):
        raise SystemExit(
            "prefix duel count must equal the session initial generation "
            "minus one"
        )
    if game_generation is not None and (
        type(game_generation) is not int or game_generation != game_number
    ):
        raise SystemExit("game_generation must equal the current game_number")
    replay = session.get("series_replay_prefix")
else:
    if session.get("status") != "finished":
        raise SystemExit("replay recovery requires a finished session")
    expected_duels = expected_series_duels(session)
    if len(envelope["duels"]) != expected_duels:
        raise SystemExit(
            "EVRP duel count does not match the finished session expectation"
        )
    replay = session.get("series_replay")

meta = envelope.get("meta") or {}
for key in ("session_id", "room_id", "match_id"):
    if key in meta and meta[key] != required_identity[key]:
        raise SystemExit(f"EVRP metadata identity mismatch for {key}")
if "duels" in meta and meta["duels"] != required_identity["duels"]:
    raise SystemExit("EVRP metadata duel count mismatch")

existing_hashes = set()
if target.is_file():
    existing_hashes.add(hashlib.sha256(target.read_bytes()).hexdigest())
if isinstance(replay, dict) and isinstance(replay.get("sha256"), str):
    existing_hashes.add(replay["sha256"])
if mode == "prefix" and isinstance(replay, dict):
    old_path = replay.get("path")
    if isinstance(old_path, str):
        resolved = Path(old_path).resolve()
        replay_root = Path("/var/lib/ygo-pvp/replays").resolve()
        if resolved.parent == replay_root and resolved.is_file():
            existing_hashes.add(
                hashlib.sha256(resolved.read_bytes()).hexdigest()
            )
different = sorted(value for value in existing_hashes if value != candidate_sha)
if different and not allow_replace:
    variable = (
        "ALLOW_REPLACE_REPLAY_PREFIX"
        if mode == "prefix"
        else "ALLOW_REPLACE_REPLAY"
    )
    raise SystemExit(
        f"existing {mode} replay has a different SHA-256; "
        f"set {variable}=1 for an audited replacement"
    )

metadata_matches = (
    isinstance(replay, dict)
    and replay.get("path") == str(target)
    and replay.get("bytes") == len(payload)
    and replay.get("sha256") == candidate_sha
    and replay.get("duels") == len(envelope["duels"])
)
if mode == "final":
    metadata_matches = (
        session.get("series_finished") is True and metadata_matches
    )
noop = (
    target.is_file()
    and hashlib.sha256(target.read_bytes()).hexdigest() == candidate_sha
    and metadata_matches
)
print(json.dumps({
    "session_id": session_id,
    "mode": mode,
    "room_id": session.get("room_id"),
    "match_id": session.get("match_id"),
    "duels": len(envelope["duels"]),
    "bytes": len(payload),
    "sha256": candidate_sha,
    "target": str(target),
    "noop": noop,
    "replacement_authorized": bool(different and allow_replace),
}, ensure_ascii=False, indent=2))
PY
}

assert_no_live_connections "pre-maintenance"
validate_recovery >"$preflight_json"
noop="$(python3 -c \
    'import json,sys; print(int(json.load(open(sys.argv[1]))["noop"]))' \
    "$preflight_json")"
if [[ "$noop" = 1 ]]; then
    committed=1
    cat "$preflight_json"
    echo "replay recovery already applied"
    rm -f -- "$preflight_json"
    exit 0
fi

capture_service_state() {
    sudo -n systemctl is-active "$1" 2>/dev/null || true
}

tournament_state="$(capture_service_state ygo-tournament)"
pvp_state="$(capture_service_state ygo-pvp)"
evolution_state="$(capture_service_state ygo-evolution)"
for state in "$tournament_state" "$pvp_state" "$evolution_state"; do
    case "$state" in
        active|inactive) ;;
        *)
            printf 'refusing replay recovery with unstable service state: %s\n' \
                "$state" >&2
            exit 1
            ;;
    esac
done

restore_one_service() {
    local service="$1"
    local desired="$2"
    case "$desired" in
        active|activating|reloading)
            sudo -n systemctl start "$service"
            ;;
        *)
            sudo -n systemctl stop "$service"
            ;;
    esac
}

restore_service_states() {
    restore_one_service ygo-pvp "$pvp_state"
    restore_one_service ygo-evolution "$evolution_state"
    restore_one_service ygo-tournament "$tournament_state"
}

verify_backup() {
    sudo -n bash -c 'cd "$1" && sha256sum -c SHA256SUMS' _ "$backup"
}

restore_data_archive() {
    local archive="$1"
    local target="$2"
    case "$target" in
        /var/lib/ygo-tournament|/var/lib/ygo-pvp) ;;
        *)
            printf 'refusing unsafe rollback target: %s\n' "$target" >&2
            return 1
            ;;
    esac
    sudo -n rm -rf -- "$target"
    sudo -n tar --acls --xattrs --numeric-owner \
        -C /var/lib -xpf "$archive"
}

rollback() {
    local rollback_failed=0
    if [[ "$backup_ready" = 1 && "$mutation_started" = 1 ]]; then
        sudo -n systemctl stop ygo-tournament || rollback_failed=1
        sudo -n systemctl stop ygo-pvp || rollback_failed=1
        sudo -n systemctl stop ygo-evolution || rollback_failed=1
        verify_backup || rollback_failed=1
        if [[ "$rollback_failed" = 0 ]]; then
            restore_data_archive \
                "$backup/data/ygo-tournament.tar" \
                /var/lib/ygo-tournament || rollback_failed=1
            restore_data_archive \
                "$backup/data/ygo-pvp.tar" \
                /var/lib/ygo-pvp || rollback_failed=1
        fi
    fi
    if [[ "$maintenance_entered" = 1 ]]; then
        restore_service_states || rollback_failed=1
    fi
    if [[ "$rollback_failed" = 0 ]]; then
        printf 'replay recovery failed; restored backup=%s\n' "$backup" >&2
    else
        printf 'replay recovery failed and rollback needs manual attention; backup=%s\n' \
            "$backup" >&2
    fi
    return "$rollback_failed"
}

on_error() {
    failure_status="$1"
}

on_exit() {
    local status="$1"
    local effective="$status"
    trap - ERR EXIT
    rm -f -- "$preflight_json"
    if [[ "$failure_status" != 0 ]]; then
        effective="$failure_status"
    fi
    if [[ "$committed" = 0 && "$effective" != 0 ]]; then
        rollback || true
    fi
    exit "$effective"
}

trap 'on_error "$?"' ERR
trap 'on_exit "$?"' EXIT

maintenance_entered=1
sudo -n systemctl stop ygo-tournament
sudo -n systemctl stop ygo-pvp
sudo -n systemctl stop ygo-evolution
assert_no_live_connections "maintenance-window"
validate_recovery >/dev/null

stamp="$(date -u +%Y%m%dT%H%M%SZ)"
backup="/opt/ygo-evolution/backups/replay-${mode}-${session_id}-${stamp}-$$"
sudo -n test ! -e "$backup"
sudo -n install -d -m 700 "$backup/data" "$backup/audit"
sudo -n cp -a "$candidate" "$backup/audit/candidate.evrp"
sudo -n cp -a "$identity" "$backup/audit/candidate.identity.json"
sudo -n cp -a "$preflight_json" "$backup/audit/preflight.json"
printf 'ygo-tournament=%s\nygo-pvp=%s\nygo-evolution=%s\n' \
    "$tournament_state" "$pvp_state" "$evolution_state" |
    sudo -n tee "$backup/service-states.env" >/dev/null
sudo -n tar --acls --xattrs --numeric-owner \
    -C /var/lib -cpf "$backup/data/ygo-tournament.tar" ygo-tournament
sudo -n tar --acls --xattrs --numeric-owner \
    -C /var/lib -cpf "$backup/data/ygo-pvp.tar" ygo-pvp
sudo -n bash -c '
    cd "$1"
    sha256sum \
        data/ygo-tournament.tar \
        data/ygo-pvp.tar \
        audit/candidate.evrp \
        audit/candidate.identity.json \
        audit/preflight.json \
        service-states.env > SHA256SUMS
    sha256sum -c SHA256SUMS
' _ "$backup"
validate_recovery \
    "$backup/audit/candidate.evrp" \
    "$backup/audit/candidate.identity.json" >/dev/null
backup_ready=1

mutation_started=1
sudo -n env \
    PYTHONPATH=/opt/ygo-pvp/app \
    RECOVERY_CANDIDATE="$backup/audit/candidate.evrp" \
    RECOVERY_IDENTITY="$backup/audit/candidate.identity.json" \
    RECOVERY_SESSION_ID="$session_id" \
    RECOVERY_MODE="$mode" \
    ALLOW_REPLACE_REPLAY="$allow_replace" \
    /opt/ygo-pvp/venv/bin/python - <<'PY'
import hashlib
import json
import os
import grp
import pwd
import time
from pathlib import Path

from pvp.server import (
    atomic_bytes,
    atomic_json,
    expected_series_duels,
    validate_series_replay,
)

candidate = Path(os.environ["RECOVERY_CANDIDATE"])
identity_path = Path(os.environ["RECOVERY_IDENTITY"])
session_id = os.environ["RECOVERY_SESSION_ID"]
mode = os.environ["RECOVERY_MODE"]
allow_replace = os.environ.get("ALLOW_REPLACE_REPLAY") == "1"
session_path = Path("/var/lib/ygo-pvp") / f"{session_id}.json"
suffix = "prefix.evrp" if mode == "prefix" else "evrp"
target = Path("/var/lib/ygo-pvp/replays") / f"{session_id}.{suffix}"

payload = candidate.read_bytes()
envelope = validate_series_replay(payload)
identity = json.loads(identity_path.read_text(encoding="utf-8"))
session = json.loads(session_path.read_text(encoding="utf-8"))
sha256 = hashlib.sha256(payload).hexdigest()

# The full identity was checked immediately before the maintenance backup.
assert identity["session_id"] == session_id
assert identity["room_id"] == session["room_id"]
assert identity["match_id"] == session["match_id"]
assert identity["duels"] == len(envelope["duels"])
assert identity["sha256"] == sha256
if mode == "prefix":
    if session.get("continuous_series") is not True:
        raise SystemExit("replay prefix requires a continuous series")
    if session.get("series_replay") is not None:
        raise SystemExit("replay prefix cannot replace a final replay")
    game_number = session.get("game_number")
    initial_generation = session.get("initial_generation")
    if initial_generation is None:
        initial_generation = len(envelope["duels"]) + 1
    game_generation = session.get("game_generation")
    if (
        type(initial_generation) is not int
        or type(game_number) is not int
        or game_number < initial_generation
        or len(envelope["duels"]) != initial_generation - 1
    ):
        raise SystemExit("prefix duel count does not precede game_number")
    if game_generation is not None and (
        type(game_generation) is not int or game_generation != game_number
    ):
        raise SystemExit("game_generation must equal the current game_number")
    existing = session.get("series_replay_prefix")
else:
    if session.get("status") != "finished":
        raise SystemExit("replay recovery requires a finished session")
    if len(envelope["duels"]) != expected_series_duels(session):
        raise SystemExit("finished replay duel count changed")
    existing = session.get("series_replay")

existing_hashes = set()
if target.is_file():
    existing_hashes.add(hashlib.sha256(target.read_bytes()).hexdigest())
if isinstance(existing, dict) and isinstance(existing.get("sha256"), str):
    existing_hashes.add(existing["sha256"])
if any(value != sha256 for value in existing_hashes) and not allow_replace:
    raise SystemExit("refusing to overwrite a different replay archive")

atomic_bytes(target, payload, mode=0o644)
metadata = {
    "path": str(target),
    "bytes": len(payload),
    "sha256": sha256,
    "duels": len(envelope["duels"]),
}
if mode == "prefix":
    metadata.update({
        "session_id": session_id,
        "room_id": session["room_id"],
        "match_id": session["match_id"],
    })
    session["initial_generation"] = initial_generation
    session["game_generation"] = game_number
    session["series_replay_prefix"] = metadata
else:
    session["series_finished"] = True
    metadata["recovered_from_yrp"] = bool(
        identity.get("recovered_from_yrp", True)
    )
    session["series_replay"] = metadata
session["updated_at"] = time.time()
atomic_json(session_path, session)

uid = pwd.getpwnam("ygo-pvp").pw_uid
gid = grp.getgrnam("ygo-pvp").gr_gid
for path in (target, session_path):
    os.chown(path, uid, gid)
os.chmod(target, 0o644)
os.chmod(session_path, 0o600)
PY

restore_service_states

if [[ "$pvp_state" = "active" && "$mode" = "final" ]]; then
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

validate_recovery \
    "$backup/audit/candidate.evrp" \
    "$backup/audit/candidate.identity.json" >"$preflight_json"
test "$(python3 -c \
    'import json,sys; print(int(json.load(open(sys.argv[1]))["noop"]))' \
    "$preflight_json")" = 1

if [[ "$pvp_state" = "active" && "$mode" = "final" ]]; then
    registry="/run/ygo-provisioning/p${session_id#s-}.json"
    registry_ok=0
    for _ in $(seq 1 30); do
        if sudo -n test -f "$registry" \
            && sudo -n env \
                REGISTRY="$registry" \
                SESSION_ID="$session_id" \
                EXPECTED_SHA="$(python3 -c \
                    'import json,sys; print(json.load(open(sys.argv[1]))["sha256"])' \
                    "$preflight_json")" \
                python3 - <<'PY'
import json
import os
from pathlib import Path

registry = json.loads(
    Path(os.environ["REGISTRY"]).read_text(encoding="utf-8")
)
replay = registry.get("series_replay") or {}
if registry.get("id") not in (None, os.environ["SESSION_ID"]):
    raise SystemExit(1)
if registry.get("series_finished") is not True:
    raise SystemExit(1)
if replay.get("sha256") != os.environ["EXPECTED_SHA"]:
    raise SystemExit(1)
PY
        then
            registry_ok=1
            break
        fi
        sleep 1
    done
    test "$registry_ok" = 1
fi

verify_backup
committed=1
cat "$preflight_json"
printf 'backup=%s\n' "$backup"
