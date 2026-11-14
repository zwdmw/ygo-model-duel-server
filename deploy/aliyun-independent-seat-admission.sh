#!/usr/bin/env bash
set -euo pipefail

stage="${1:?provide the uploaded patch directory}"
test -f "$stage/server.py"
test -f "$stage/YGOProRoom.js"
test -f "$stage/YGOProWaitingState.js"

active_connections="$(
    sudo -n python3 - <<'PY'
import json
from pathlib import Path

count = 0
for path in Path("/var/lib/ygo-pvp").glob("s-*.json"):
    try:
        session = json.loads(path.read_text())
    except Exception:
        continue
    connections = session.get("connections") or {}
    count += sum(
        bool((connections.get(side) or {}).get("connected"))
        for side in ("a", "b")
    )
print(count)
PY
)"
if [[ "$active_connections" != "0" ]]; then
    printf 'refusing to restart with %s active player connections\n' \
        "$active_connections" >&2
    exit 1
fi

stamp="$(date -u +%Y%m%dT%H%M%SZ)"
backup="/opt/ygo-evolution/backups/independent-seat-admission-$stamp"
sudo -n install -d -m 755 "$backup/pvp" "$backup/evolution" \
    "$backup/provisioning"
sudo -n cp -a /opt/ygo-pvp/app/pvp/server.py "$backup/pvp/server.py"
sudo -n cp -a \
    /opt/ygo-evolution/dist/src/ygopro/room/domain/YGOProRoom.js \
    "$backup/evolution/YGOProRoom.js"
sudo -n cp -a \
    /opt/ygo-evolution/dist/src/ygopro/room/domain/states/YGOProWaitingState.js \
    "$backup/evolution/YGOProWaitingState.js"
if sudo -n test -d /run/ygo-provisioning; then
    sudo -n cp -a /run/ygo-provisioning/. "$backup/provisioning/"
fi

sudo -n /opt/ygo-pvp/venv/bin/python -m py_compile "$stage/server.py"
sudo -n /opt/node-v24.14.0/bin/node --check "$stage/YGOProRoom.js"
sudo -n /opt/node-v24.14.0/bin/node --check "$stage/YGOProWaitingState.js"

sudo -n install -m 644 -o ygo-pvp -g ygo-pvp \
    "$stage/server.py" /opt/ygo-pvp/app/pvp/server.py
sudo -n install -m 644 -o ygo-evolution -g ygo-evolution \
    "$stage/YGOProRoom.js" \
    /opt/ygo-evolution/dist/src/ygopro/room/domain/YGOProRoom.js
sudo -n install -m 644 -o ygo-evolution -g ygo-evolution \
    "$stage/YGOProWaitingState.js" \
    /opt/ygo-evolution/dist/src/ygopro/room/domain/states/YGOProWaitingState.js

# Existing unfinished rooms were provisioned before canonical player
# identities were part of the registry schema. Upgrade them in place before
# Evolution restarts so they remain joinable.
sudo -n env PYTHONPATH=/opt/ygo-pvp/app \
    /opt/ygo-pvp/venv/bin/python - <<'PY'
import json
import os
from pathlib import Path
import tempfile

from pvp.server import native_player_names

registry = Path("/run/ygo-provisioning")
sessions = Path("/var/lib/ygo-pvp")
updated = 0
for path in registry.glob("*.json"):
    provision = json.loads(path.read_text(encoding="utf-8"))
    session_id = provision.get("session_id")
    session_path = sessions / f"{session_id}.json"
    if not session_path.is_file():
        raise SystemExit(f"missing session for provisioning file {path.name}")
    session = json.loads(session_path.read_text(encoding="utf-8"))
    provision["players"] = native_player_names(session)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=registry,
        prefix=".seat-admission-",
        delete=False,
    ) as stream:
        temporary = Path(stream.name)
        json.dump(provision, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.chmod(temporary, 0o644)
    temporary.replace(path)
    updated += 1
print(f"provisioning_updated={updated}")
PY

sudo -n /opt/ygo-pvp/venv/bin/python -m py_compile \
    /opt/ygo-pvp/app/pvp/server.py
sudo -n /opt/node-v24.14.0/bin/node --check \
    /opt/ygo-evolution/dist/src/ygopro/room/domain/YGOProRoom.js
sudo -n /opt/node-v24.14.0/bin/node --check \
    /opt/ygo-evolution/dist/src/ygopro/room/domain/states/YGOProWaitingState.js

sudo -n systemctl restart ygo-pvp ygo-evolution
sudo -n systemctl is-active ygo-pvp ygo-evolution

healthy=0
for _ in $(seq 1 30); do
    if curl -fsS --max-time 2 http://127.0.0.1:8790/health >/dev/null 2>&1; then
        healthy=1
        break
    fi
    sleep 1
done
test "$healthy" = 1

printf 'backup=%s\n' "$backup"
sudo -n sha256sum \
    /opt/ygo-pvp/app/pvp/server.py \
    /opt/ygo-evolution/dist/src/ygopro/room/domain/YGOProRoom.js \
    /opt/ygo-evolution/dist/src/ygopro/room/domain/states/YGOProWaitingState.js
