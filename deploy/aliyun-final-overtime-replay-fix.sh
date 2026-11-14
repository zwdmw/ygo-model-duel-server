#!/usr/bin/env bash
set -eEuo pipefail

stage="${1:?provide the uploaded patch directory}"
target_room="${2:-r-c54c8de4}"
target_match="${3:-m-final}"
prefix_candidate="${4:-}"
prefix_identity="${5:-}"
allow_prefix_replace="${ALLOW_REPLACE_REPLAY_PREFIX:-0}"

if [[ -n "$prefix_candidate" ]]; then
    prefix_identity="${prefix_identity:-${prefix_candidate}.identity.json}"
    test -f "$prefix_candidate"
    test -f "$prefix_identity"
elif [[ -n "$prefix_identity" ]]; then
    echo "prefix identity requires a prefix EVRP" >&2
    exit 1
fi

tournament_source="$stage/tournament/server.py"
pvp_source="$stage/pvp/server.py"
tournament_target="/opt/ygo-tournament/app/tournament/server.py"
pvp_target="/opt/ygo-pvp/app/pvp/server.py"
expected_tournament="56e29fce7dd5762e12db96c61eb5d3b855b2789da29d1006ec1ec9558175c104"
expected_pvp="66132d151e6f60e2531e514401b6a5569b8e7caf1c3eaa23254921a793a36c8e"

test -f "$tournament_source"
test -f "$pvp_source"

preflight_json="$(mktemp)"
prefix_preflight_json="$(mktemp)"
session_index_json="$(mktemp)"
trap 'rm -f -- "$preflight_json" "$prefix_preflight_json" "$session_index_json"' EXIT
failure_status=0
maintenance_entered=0
backup_ready=0
mutation_started=0
committed=0
backup=""
tournament_state=""
pvp_state=""
evolution_state=""

validate_python_source() {
    local interpreter="$1"
    local source="$2"
    sudo -n env VALIDATE_SOURCE="$source" "$interpreter" - <<'PY'
import os
import py_compile
import tempfile

source = os.environ["VALIDATE_SOURCE"]
with tempfile.TemporaryDirectory(prefix="ygo-stage-compile-") as temporary:
    py_compile.compile(
        source,
        cfile=os.path.join(temporary, "validated.pyc"),
        doraise=True,
    )
PY
}

# Validate staged files before reading or changing any production state.
validate_python_source /opt/ygo-tournament/venv/bin/python "$tournament_source"
validate_python_source /opt/ygo-pvp/venv/bin/python "$pvp_source"
tournament_stage_sha="$(sha256sum "$tournament_source" | awk '{print $1}')"
pvp_stage_sha="$(sha256sum "$pvp_source" | awk '{print $1}')"

check_baseline() {
    local target="$1"
    local expected="$2"
    local staged="$3"
    local actual deployed
    actual="$(sudo -n sha256sum "$target" | awk '{print $1}')"
    deployed="$staged"
    if [[ "$actual" != "$expected" && "$actual" != "$deployed" ]]; then
        printf 'deployment baseline changed: %s\nexpected=%s\nactual=%s\n' \
            "$target" "$expected" "$actual" >&2
        exit 1
    fi
}

check_baseline "$tournament_target" "$expected_tournament" "$tournament_stage_sha"
check_baseline "$pvp_target" "$expected_pvp" "$pvp_stage_sha"

assert_no_live_connections() {
    local phase="$1"
    local tcp_connections
    tcp_connections="$(sudo -n ss -Hnt state established sport = :6009)"
    if [[ -n "$tcp_connections" ]]; then
        printf '%s\n' "$tcp_connections" >&2
        printf 'refusing deployment while port 6009 has established connections (%s)\n' \
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
            "status": session.get("status"),
            "game_number": session.get("game_number"),
            "connected": connected,
        })
if active:
    print(json.dumps(active, ensure_ascii=False, indent=2))
    raise SystemExit(
        "refusing deployment while tournament players are connected "
        f"({os.environ['CHECK_PHASE']})"
    )
PY
}

validate_target_state() {
    local require_native_session="$1"
    sudo -n env \
        TARGET_ROOM="$target_room" \
        TARGET_MATCH="$target_match" \
        REQUIRE_NATIVE_SESSION="$require_native_session" \
        python3 - <<'PY'
import json
import os
from pathlib import Path

room_id = os.environ["TARGET_ROOM"]
match_id = os.environ["TARGET_MATCH"]
require_native = os.environ["REQUIRE_NATIVE_SESSION"] == "1"
room_path = Path("/var/lib/ygo-tournament") / f"{room_id}.json"
if not room_path.is_file():
    raise SystemExit(f"target room is missing: {room_id}")
room = json.loads(room_path.read_text(encoding="utf-8"))
if room.get("id") not in (None, room_id):
    raise SystemExit("target room identity mismatch")
if room.get("engine") != "native-pvp":
    raise SystemExit("target room is not a native-pvp room")
if room.get("status") != "running":
    raise SystemExit("target room is not running")

match = next(
    (item for item in room.get("matches", []) if item.get("id") == match_id),
    None,
)
if match is None:
    raise SystemExit(f"target match is missing: {match_id}")
if match.get("stage") != "final":
    raise SystemExit("target match is not a final")
if match.get("status") != "tiebreak" or match.get("phase") != "tiebreak":
    raise SystemExit("target final is not waiting for a tiebreak game")
points = match.get("points") or {}
if points.get("a") != points.get("b"):
    raise SystemExit("target final is not tied")
session = match.get("session") or {}
if session.get("engine") != "native-pvp":
    raise SystemExit("target match session is not native-pvp")
ready = session.get("player_ready") or {}
if ready.get("a") is not True or ready.get("b") is not True:
    raise SystemExit("both players must be ready before deployment")

expected_game = len(match.get("games") or []) + 1
plans = [
    plan for plan in match.get("game_plans", [])
    if plan.get("number") == expected_game
]
if len(plans) > 1:
    raise SystemExit("multiple plans exist for the next game")
plan = plans[0] if plans else None
if plan is not None:
    seats = plan.get("seat_map")
    first = plan.get("required_first_player")
    if (
        not isinstance(seats, dict)
        or {seats.get("a"), seats.get("b")} != {0, 1}
        or first not in ("a", "b")
        or seats.get(first) != 0
    ):
        raise SystemExit("next game plan has an invalid seat assignment")

session_id = session.get("pvp_session_id")
native_ready = False
if isinstance(session_id, str) and session_id:
    session_path = Path("/var/lib/ygo-pvp") / f"{session_id}.json"
    if not session_path.is_file():
        raise SystemExit("target match references a missing native session")
    native = json.loads(session_path.read_text(encoding="utf-8"))
    if native.get("room_id") != room_id or native.get("match_id") != match_id:
        raise SystemExit("native session identity mismatch")
    if native.get("game_number") != expected_game:
        raise SystemExit("native session game number mismatch")
    if native.get("continuous_series") is not True:
        raise SystemExit("native session is not continuous")
    if plan is not None and (
        native.get("seat_plan") != plan.get("seat_map")
        or native.get("required_first_player")
        != plan.get("required_first_player")
    ):
        raise SystemExit("native session does not match the next game plan")
    native_ready = True
elif require_native:
    raise SystemExit("next native session was not provisioned")

print(json.dumps({
    "room_id": room_id,
    "match_id": match_id,
    "completed_games": expected_game - 1,
    "expected_next_game": expected_game,
    "planned_seat_map": plan.get("seat_map") if plan else None,
    "planned_first_player": (
        plan.get("required_first_player") if plan else None
    ),
    "native_session_id": session_id,
    "native_session_ready": native_ready,
}, ensure_ascii=False, indent=2))
PY
}

sync_target_pvp_sessions() {
    local mode="${1:-check}"
    if [[ "$mode" != "check" && "$mode" != "apply" ]]; then
        echo "invalid PvP session index mode: $mode" >&2
        return 1
    fi
    sudo -n env \
        TARGET_ROOM="$target_room" \
        TARGET_MATCH="$target_match" \
        SESSION_INDEX_MODE="$mode" \
        python3 - <<'PY'
import glob
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
import time

room_id = os.environ["TARGET_ROOM"]
match_id = os.environ["TARGET_MATCH"]
mode = os.environ["SESSION_INDEX_MODE"]
room_path = Path("/var/lib/ygo-tournament") / f"{room_id}.json"
room = json.loads(room_path.read_text(encoding="utf-8"))
match = next(
    (item for item in room.get("matches", []) if item.get("id") == match_id),
    None,
)
if match is None:
    raise SystemExit("target match is missing during PvP session backfill")
games = match.get("games") or []
current_session_id = (match.get("session") or {}).get("pvp_session_id")
if not isinstance(current_session_id, str) or not current_session_id:
    raise SystemExit("target match has no current PvP session")

def positive_int(value):
    return value if type(value) is int and value > 0 else None

sessions = {}
claims = {}
replay_root = Path("/var/lib/ygo-pvp/replays").resolve()

def verified_replay(metadata, label):
    if not isinstance(metadata, dict):
        return False
    path_value = metadata.get("path")
    byte_count = metadata.get("bytes")
    digest = metadata.get("sha256")
    if (
        not isinstance(path_value, str)
        or type(byte_count) is not int
        or byte_count <= 0
        or not isinstance(digest, str)
        or len(digest) != 64
    ):
        raise SystemExit(f"invalid replay metadata for {label}")
    replay_path = Path(path_value).resolve()
    if (
        replay_path.parent != replay_root
        or not replay_path.is_file()
        or replay_path.stat().st_size != byte_count
        or hashlib.sha256(replay_path.read_bytes()).hexdigest() != digest
    ):
        raise SystemExit(f"stored replay verification failed for {label}")
    return True

for raw_path in sorted(glob.glob("/var/lib/ygo-pvp/s-*.json")):
    path = Path(raw_path)
    session = json.loads(path.read_text(encoding="utf-8"))
    if (
        session.get("room_id") != room_id
        or session.get("match_id") != match_id
    ):
        continue
    session_id = session.get("id")
    if (
        not isinstance(session_id, str)
        or not session_id
        or path.stem != session_id
    ):
        raise SystemExit(f"invalid PvP session identity: {path}")
    replay_games = set()
    game_number = positive_int(session.get("game_number"))
    if (
        verified_replay(session.get("replay"), f"{session_id}:single")
        and game_number is not None
    ):
        replay_games.add(game_number)
    for index, replay in enumerate(session.get("series_replays") or []):
        ordinal = positive_int(
            replay.get("ordinal") if isinstance(replay, dict) else None
        )
        if ordinal is None:
            raise SystemExit(
                f"invalid series replay ordinal for {session_id}:{index}"
            )
        if verified_replay(replay, f"{session_id}:game-{ordinal}"):
            replay_games.add(ordinal)
    result_games = {
        number
        for item in session.get("results") or []
        if isinstance(item, dict)
        for number in [positive_int(item.get("game_number"))]
        if number is not None
    }
    initial_generation = positive_int(session.get("initial_generation"))
    start_candidates = (
        ({initial_generation} if initial_generation is not None else set())
        | replay_games
        | result_games
        | ({game_number} if game_number is not None else set())
    )
    if not start_candidates:
        raise SystemExit(f"PvP session has no game identity: {session_id}")
    if (
        session_id != current_session_id
        and not replay_games
        and not result_games
    ):
        continue
    sessions[session_id] = {
        "id": session_id,
        "start_game": min(start_candidates),
        "replay_games": replay_games,
    }
    for number in replay_games:
        claims.setdefault(number, []).append(session_id)

if current_session_id not in sessions:
    raise SystemExit("current PvP session was not found during backfill")

start_owners = {}
for session in sessions.values():
    start_game = session["start_game"]
    previous = start_owners.get(start_game)
    if previous is not None and previous != session["id"]:
        raise SystemExit(
            f"multiple PvP sessions start at game {start_game}: "
            f"{previous}, {session['id']}"
        )
    start_owners[start_game] = session["id"]

desired_entries = [
    {"id": item["id"], "start_game": item["start_game"]}
    for item in sorted(
        sessions.values(),
        key=lambda item: (item["start_game"], item["id"]),
    )
]
changed = match.get("pvp_sessions") != desired_entries
for expected_number, game in enumerate(games, 1):
    number = positive_int(game.get("number"))
    if number != expected_number:
        raise SystemExit("tournament game numbers are not contiguous")
    owners = sorted(set(claims.get(number, [])))
    if len(owners) != 1:
        raise SystemExit(
            f"completed game {number} must have exactly one replay session; "
            f"found {owners}"
        )
    session_id = owners[0]
    stored_session_id = game.get("session_id")
    if stored_session_id not in (None, session_id):
        raise SystemExit(
            f"completed game {number} has conflicting session ids: "
            f"{stored_session_id}, {session_id}"
        )
    if stored_session_id != session_id:
        changed = True
        game["session_id"] = session_id
match["pvp_sessions"] = desired_entries

if mode == "apply" and changed:
    room["updated_at"] = time.time()
    source_stat = room_path.stat()
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".tournament-pvp-index-",
        dir=room_path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(room, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, stat.S_IMODE(source_stat.st_mode))
        os.chown(temporary, source_stat.st_uid, source_stat.st_gid)
        os.replace(temporary, room_path)
        directory = os.open(room_path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)

print(json.dumps({
    "room_id": room_id,
    "match_id": match_id,
    "mode": mode,
    "changed": changed,
    "completed_games": len(games),
    "indexed_sessions": len(desired_entries),
    "current_session_id": current_session_id,
    "pvp_sessions": desired_entries,
    "game_session_ids": {
        str(game["number"]): game.get("session_id")
        for game in games
    },
}, ensure_ascii=False, indent=2))
PY
}

validate_prefix() {
    local candidate_path="${1:-$prefix_candidate}"
    local identity_path="${2:-$prefix_identity}"
    sudo -n env \
        PYTHONPATH=/opt/ygo-pvp/app \
        TARGET_ROOM="$target_room" \
        TARGET_MATCH="$target_match" \
        PREFIX_CANDIDATE="$candidate_path" \
        PREFIX_IDENTITY="$identity_path" \
        ALLOW_REPLACE_REPLAY_PREFIX="$allow_prefix_replace" \
        /opt/ygo-pvp/venv/bin/python - <<'PY'
import hashlib
import json
import os
from pathlib import Path

from pvp.server import validate_series_replay

room_id = os.environ["TARGET_ROOM"]
match_id = os.environ["TARGET_MATCH"]
candidate = Path(os.environ["PREFIX_CANDIDATE"])
identity_path = Path(os.environ["PREFIX_IDENTITY"])
allow_replace = os.environ.get("ALLOW_REPLACE_REPLAY_PREFIX") == "1"

room = json.loads(
    (Path("/var/lib/ygo-tournament") / f"{room_id}.json").read_text(
        encoding="utf-8"
    )
)
match = next(
    (item for item in room.get("matches", []) if item.get("id") == match_id),
    None,
)
if match is None:
    raise SystemExit("prefix target match is missing")
session_id = (match.get("session") or {}).get("pvp_session_id")
if not isinstance(session_id, str) or not session_id:
    raise SystemExit("prefix requires an existing native session")
session_path = Path("/var/lib/ygo-pvp") / f"{session_id}.json"
session = json.loads(session_path.read_text(encoding="utf-8"))
if session.get("room_id") != room_id or session.get("match_id") != match_id:
    raise SystemExit("prefix target session identity mismatch")
if session.get("continuous_series") is not True:
    raise SystemExit("prefix requires a continuous series")
if session.get("series_replay") is not None:
    raise SystemExit("prefix cannot replace a final series replay")

payload = candidate.read_bytes()
candidate_sha = hashlib.sha256(payload).hexdigest()
envelope = validate_series_replay(payload)
duels = len(envelope["duels"])
game_number = session.get("game_number")
initial_generation = session.get("initial_generation")
if initial_generation is None:
    initial_generation = duels + 1
game_generation = session.get("game_generation")
if (
    type(initial_generation) is not int
    or type(game_number) is not int
    or game_number < initial_generation
    or duels != initial_generation - 1
):
    raise SystemExit(
        "prefix duel count must equal the session initial generation minus one"
    )
if game_generation is not None and (
    type(game_generation) is not int or game_generation != game_number
):
    raise SystemExit("game_generation must equal the current game_number")

identity = json.loads(identity_path.read_text(encoding="utf-8"))
required_identity = {
    "session_id": session_id,
    "room_id": room_id,
    "match_id": match_id,
    "duels": duels,
    "sha256": candidate_sha,
}
for key, expected in required_identity.items():
    if identity.get(key) != expected:
        raise SystemExit(
            f"prefix identity mismatch for {key}: "
            f"expected={expected!r} actual={identity.get(key)!r}"
        )

target = (
    Path("/var/lib/ygo-pvp/replays")
    / f"{session_id}.prefix.evrp"
)
existing = session.get("series_replay_prefix")
existing_hashes = set()
if target.is_file():
    existing_hashes.add(hashlib.sha256(target.read_bytes()).hexdigest())
if isinstance(existing, dict):
    if isinstance(existing.get("sha256"), str):
        existing_hashes.add(existing["sha256"])
    old_path = existing.get("path")
    if isinstance(old_path, str):
        resolved = Path(old_path).resolve()
        replay_root = Path("/var/lib/ygo-pvp/replays").resolve()
        if resolved.parent == replay_root and resolved.is_file():
            existing_hashes.add(
                hashlib.sha256(resolved.read_bytes()).hexdigest()
            )
different = sorted(value for value in existing_hashes if value != candidate_sha)
if different and not allow_replace:
    raise SystemExit(
        "existing prefix has a different SHA-256; "
        "set ALLOW_REPLACE_REPLAY_PREFIX=1 for an audited replacement"
    )

metadata_matches = (
    isinstance(existing, dict)
    and existing.get("path") == str(target)
    and existing.get("bytes") == len(payload)
    and existing.get("sha256") == candidate_sha
    and existing.get("duels") == duels
)
noop = (
    target.is_file()
    and hashlib.sha256(target.read_bytes()).hexdigest() == candidate_sha
    and metadata_matches
)
print(json.dumps({
    "requested": True,
    "session_id": session_id,
    "room_id": room_id,
    "match_id": match_id,
    "duels": duels,
    "bytes": len(payload),
    "sha256": candidate_sha,
    "target": str(target),
    "initial_generation": initial_generation,
    "noop": noop,
    "replacement_authorized": bool(different and allow_replace),
}, ensure_ascii=False, indent=2))
PY
}

install_prefix() {
    local candidate_path="$1"
    local identity_path="$2"
    sudo -n env \
        PYTHONPATH=/opt/ygo-pvp/app \
        TARGET_ROOM="$target_room" \
        TARGET_MATCH="$target_match" \
        PREFIX_CANDIDATE="$candidate_path" \
        PREFIX_IDENTITY="$identity_path" \
        ALLOW_REPLACE_REPLAY_PREFIX="$allow_prefix_replace" \
        /opt/ygo-pvp/venv/bin/python - <<'PY'
import grp
import hashlib
import json
import os
import pwd
import time
from pathlib import Path

from pvp.server import atomic_bytes, atomic_json, validate_series_replay

room_id = os.environ["TARGET_ROOM"]
match_id = os.environ["TARGET_MATCH"]
candidate = Path(os.environ["PREFIX_CANDIDATE"])
identity = json.loads(
    Path(os.environ["PREFIX_IDENTITY"]).read_text(encoding="utf-8")
)
allow_replace = os.environ.get("ALLOW_REPLACE_REPLAY_PREFIX") == "1"
room = json.loads(
    (Path("/var/lib/ygo-tournament") / f"{room_id}.json").read_text(
        encoding="utf-8"
    )
)
match = next(
    item for item in room.get("matches", []) if item.get("id") == match_id
)
session_id = match["session"]["pvp_session_id"]
session_path = Path("/var/lib/ygo-pvp") / f"{session_id}.json"
session = json.loads(session_path.read_text(encoding="utf-8"))
payload = candidate.read_bytes()
envelope = validate_series_replay(payload)
sha256 = hashlib.sha256(payload).hexdigest()
duels = len(envelope["duels"])
if session.get("room_id") != room_id or session.get("match_id") != match_id:
    raise SystemExit("prefix target session identity mismatch")
if session.get("continuous_series") is not True:
    raise SystemExit("prefix requires a continuous series")
if session.get("series_replay") is not None:
    raise SystemExit("prefix cannot replace a final series replay")
game_number = session.get("game_number")
initial_generation = session.get("initial_generation")
if initial_generation is None:
    initial_generation = duels + 1
game_generation = session.get("game_generation")
if (
    type(initial_generation) is not int
    or type(game_number) is not int
    or game_number < initial_generation
    or duels != initial_generation - 1
):
    raise SystemExit("prefix duel count does not precede game_number")
if game_generation is not None and (
    type(game_generation) is not int or game_generation != game_number
):
    raise SystemExit("game_generation must equal the current game_number")
required = {
    "session_id": session_id,
    "room_id": room_id,
    "match_id": match_id,
    "duels": duels,
    "sha256": sha256,
}
if any(identity.get(key) != value for key, value in required.items()):
    raise SystemExit("prefix identity changed after validation")

target = (
    Path("/var/lib/ygo-pvp/replays")
    / f"{session_id}.prefix.evrp"
)
existing_hashes = set()
if target.is_file():
    existing_hashes.add(hashlib.sha256(target.read_bytes()).hexdigest())
existing = session.get("series_replay_prefix")
if isinstance(existing, dict) and isinstance(existing.get("sha256"), str):
    existing_hashes.add(existing["sha256"])
if any(value != sha256 for value in existing_hashes) and not allow_replace:
    raise SystemExit("refusing to overwrite a different replay prefix")

atomic_bytes(target, payload, mode=0o644)
session["initial_generation"] = initial_generation
session["game_generation"] = game_number
session["series_replay_prefix"] = {
    "path": str(target),
    "bytes": len(payload),
    "sha256": sha256,
    "duels": duels,
    "session_id": session_id,
    "room_id": room_id,
    "match_id": match_id,
}
session["updated_at"] = time.time()
atomic_json(session_path, session)

uid = pwd.getpwnam("ygo-pvp").pw_uid
gid = grp.getgrnam("ygo-pvp").gr_gid
for path in (target, session_path):
    os.chown(path, uid, gid)
os.chmod(target, 0o644)
os.chmod(session_path, 0o600)
PY
}

assert_no_live_connections "pre-maintenance"
validate_target_state 0 >"$preflight_json"
sync_target_pvp_sessions check >"$session_index_json"
if [[ -n "$prefix_candidate" ]]; then
    validate_target_state 1 >/dev/null
    validate_prefix >"$prefix_preflight_json"
else
    printf '{"requested":false,"noop":true}\n' >"$prefix_preflight_json"
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
            printf 'refusing deployment with unstable service state: %s\n' \
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
            sudo -n cp -a "$backup/code/tournament-server.py" \
                "$tournament_target" || rollback_failed=1
            sudo -n cp -a "$backup/code/pvp-server.py" \
                "$pvp_target" || rollback_failed=1
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
        printf 'deployment failed; restored backup=%s\n' "$backup" >&2
    else
        printf 'deployment failed and rollback needs manual attention; backup=%s\n' \
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
    rm -f -- \
        "$preflight_json" \
        "$prefix_preflight_json" \
        "$session_index_json"
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

tournament_changed=0
pvp_changed=0
[[ "$(sudo -n sha256sum "$tournament_target" | awk '{print $1}')" != \
   "$tournament_stage_sha" ]] \
    && tournament_changed=1
[[ "$(sudo -n sha256sum "$pvp_target" | awk '{print $1}')" != \
   "$pvp_stage_sha" ]] \
    && pvp_changed=1
native_ready="$(python3 -c \
    'import json,sys; print(int(json.load(open(sys.argv[1]))["native_session_ready"]))' \
    "$preflight_json")"
prefix_ready="$(python3 -c \
    'import json,sys; print(int(json.load(open(sys.argv[1]))["noop"]))' \
    "$prefix_preflight_json")"
session_index_ready="$(python3 -c \
    'import json,sys; print(int(not json.load(open(sys.argv[1]))["changed"]))' \
    "$session_index_json")"
if [[ "$tournament_changed" = 0 && "$pvp_changed" = 0 \
    && "$native_ready" = 1 && "$prefix_ready" = 1 \
    && "$session_index_ready" = 1 ]]; then
    committed=1
    echo "deployment already applied and next native session is valid"
    exit 0
fi

maintenance_entered=1
sudo -n systemctl stop ygo-tournament
sudo -n systemctl stop ygo-pvp
sudo -n systemctl stop ygo-evolution
assert_no_live_connections "maintenance-window"
validate_target_state 0 >/dev/null
if [[ -n "$prefix_candidate" ]]; then
    validate_prefix >/dev/null
fi

stamp="$(date -u +%Y%m%dT%H%M%SZ)"
backup="/opt/ygo-evolution/backups/final-overtime-replay-$stamp-$$"
sudo -n test ! -e "$backup"
sudo -n install -d -m 700 \
    "$backup/code" "$backup/data" "$backup/audit" "$backup/stage"
sudo -n cp -a "$tournament_target" "$backup/code/tournament-server.py"
sudo -n cp -a "$pvp_target" "$backup/code/pvp-server.py"
sudo -n cp -a "$preflight_json" "$backup/audit/preflight.json"
sudo -n cp -a \
    "$prefix_preflight_json" \
    "$backup/audit/prefix-preflight.json"
sudo -n cp -a \
    "$session_index_json" \
    "$backup/audit/session-index-preflight.json"
sudo -n cp -a "$tournament_source" "$backup/stage/tournament-server.py"
sudo -n cp -a "$pvp_source" "$backup/stage/pvp-server.py"
test "$(sudo -n sha256sum "$backup/stage/tournament-server.py" | awk '{print $1}')" \
    = "$tournament_stage_sha"
test "$(sudo -n sha256sum "$backup/stage/pvp-server.py" | awk '{print $1}')" \
    = "$pvp_stage_sha"
validate_python_source \
    /opt/ygo-tournament/venv/bin/python \
    "$backup/stage/tournament-server.py"
validate_python_source \
    /opt/ygo-pvp/venv/bin/python \
    "$backup/stage/pvp-server.py"
if [[ -n "$prefix_candidate" ]]; then
    sudo -n cp -a "$prefix_candidate" "$backup/stage/prefix.evrp"
    sudo -n cp -a \
        "$prefix_identity" \
        "$backup/stage/prefix.identity.json"
    validate_prefix \
        "$backup/stage/prefix.evrp" \
        "$backup/stage/prefix.identity.json" >/dev/null
fi
printf 'ygo-tournament=%s\nygo-pvp=%s\nygo-evolution=%s\n' \
    "$tournament_state" "$pvp_state" "$evolution_state" |
    sudo -n tee "$backup/service-states.env" >/dev/null
sudo -n tar --acls --xattrs --numeric-owner \
    -C /var/lib -cpf "$backup/data/ygo-tournament.tar" ygo-tournament
sudo -n tar --acls --xattrs --numeric-owner \
    -C /var/lib -cpf "$backup/data/ygo-pvp.tar" ygo-pvp
manifest_files=(
    code/tournament-server.py
    code/pvp-server.py
    stage/tournament-server.py
    stage/pvp-server.py
    data/ygo-tournament.tar
    data/ygo-pvp.tar
    audit/preflight.json
    audit/prefix-preflight.json
    audit/session-index-preflight.json
    service-states.env
)
if [[ -n "$prefix_candidate" ]]; then
    manifest_files+=(stage/prefix.evrp stage/prefix.identity.json)
fi
sudo -n bash -c '
    root="$1"
    shift
    cd "$root"
    sha256sum "$@" > SHA256SUMS
    sha256sum -c SHA256SUMS
' _ "$backup" "${manifest_files[@]}"
backup_ready=1

mutation_started=1
sudo -n install -m 640 -o ygo-tournament -g ygo-tournament \
    "$backup/stage/tournament-server.py" "$tournament_target"
sudo -n install -m 640 -o ygo-pvp -g ygo-pvp \
    "$backup/stage/pvp-server.py" "$pvp_target"
validate_python_source /opt/ygo-tournament/venv/bin/python "$tournament_target"
validate_python_source /opt/ygo-pvp/venv/bin/python "$pvp_target"
sync_target_pvp_sessions apply >"$session_index_json"
sync_target_pvp_sessions check >"$session_index_json"
test "$(python3 -c \
    'import json,sys; print(int(not json.load(open(sys.argv[1]))["changed"]))' \
    "$session_index_json")" = 1
if [[ -n "$prefix_candidate" ]]; then
    install_prefix \
        "$backup/stage/prefix.evrp" \
        "$backup/stage/prefix.identity.json"
    validate_prefix \
        "$backup/stage/prefix.evrp" \
        "$backup/stage/prefix.identity.json" >"$prefix_preflight_json"
    test "$(python3 -c \
        'import json,sys; print(int(json.load(open(sys.argv[1]))["noop"]))' \
        "$prefix_preflight_json")" = 1
fi

restore_service_states

if [[ "$pvp_state" = "active" ]]; then
    pvp_healthy=0
    for _ in $(seq 1 30); do
        if curl -fsS --max-time 2 http://127.0.0.1:8790/health >/dev/null 2>&1 \
            && sudo -n ss -ltn | grep -q ':6009 '; then
            pvp_healthy=1
            break
        fi
        sleep 1
    done
    test "$pvp_healthy" = 1
fi

if [[ "$tournament_state" = "active" ]]; then
    tournament_healthy=0
    for _ in $(seq 1 30); do
        if curl -fsS --max-time 2 \
            http://127.0.0.1:8787/health >/dev/null 2>&1; then
            tournament_healthy=1
            break
        fi
        sleep 1
    done
    test "$tournament_healthy" = 1
fi

if [[ "$pvp_state" = "active" && "$tournament_state" = "active" ]]; then
    postflight_ok=0
    for _ in $(seq 1 45); do
        if validate_target_state 1; then
            postflight_ok=1
            break
        fi
        sleep 1
    done
    test "$postflight_ok" = 1
fi

if [[ -n "$prefix_candidate" ]]; then
    validate_prefix \
        "$backup/stage/prefix.evrp" \
        "$backup/stage/prefix.identity.json" >"$prefix_preflight_json"
    test "$(python3 -c \
        'import json,sys; print(int(json.load(open(sys.argv[1]))["noop"]))' \
        "$prefix_preflight_json")" = 1
fi

verify_backup
committed=1
printf 'backup=%s\n' "$backup"
printf 'changed: tournament=%s pvp=%s\n' \
    "$tournament_changed" "$pvp_changed"
if [[ -n "$prefix_candidate" ]]; then
    cat "$prefix_preflight_json"
fi
