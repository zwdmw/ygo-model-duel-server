#!/usr/bin/env bash
set -euo pipefail

stage="${1:?provide the uploaded patch directory}"
test -f "$stage/tournament/server.py"
test -f "$stage/pvp/server.py"
test -f "$stage/pvp/protocol.py"
test -f "$stage/pvp/deck.py"
test -f "$stage/pvp/__init__.py"
test -f "$stage/YGOProRoom.js"
test -f "$stage/SpectatorView.js"
test -f "$stage/ocgcore.js"
test -f "$stage/YGOProDuelingState.js"

stamp="$(date -u +%Y%m%dT%H%M%SZ)"
backup="/opt/ygo-evolution/backups/continuous-series-$stamp"
sudo -n install -d -m 755 "$backup"
sudo -n install -d -m 755 "$backup/tournament" "$backup/pvp" "$backup/evolution"
for mapping in \
    "tournament/server.py:/opt/ygo-tournament/app/tournament/server.py" \
    "pvp/server.py:/opt/ygo-pvp/app/pvp/server.py" \
    "pvp/protocol.py:/opt/ygo-pvp/app/pvp/protocol.py" \
    "pvp/deck.py:/opt/ygo-pvp/app/pvp/deck.py" \
    "pvp/__init__.py:/opt/ygo-pvp/app/pvp/__init__.py" \
    "YGOProRoom.js:/opt/ygo-evolution/dist/src/ygopro/room/domain/YGOProRoom.js" \
    "SpectatorView.js:/opt/ygo-evolution/dist/src/ygopro/room/domain/SpectatorView.js" \
    "ocgcore.js:/opt/ygo-evolution/dist/src/ygopro/ocgcore-worker/ocgcore.js" \
    "YGOProDuelingState.js:/opt/ygo-evolution/dist/src/ygopro/room/domain/states/YGOProDuelingState.js"; do
    source="${mapping%%:*}"
    target="${mapping#*:}"
    case "$source" in
        tournament/*) backup_target="$backup/$source" ;;
        pvp/*) backup_target="$backup/$source" ;;
        *) backup_target="$backup/evolution/$(basename "$source")" ;;
    esac
    if sudo -n test -e "$target"; then
        sudo -n cp -a "$target" "$backup_target"
    elif [[ "$source" != "SpectatorView.js" ]]; then
        printf 'missing deployment target: %s\n' "$target" >&2
        exit 1
    fi
done

sudo -n install -m 644 -o ygo-tournament -g ygo-tournament \
    "$stage/tournament/server.py" /opt/ygo-tournament/app/tournament/server.py
sudo -n install -m 644 -o ygo-pvp -g ygo-pvp \
    "$stage/pvp/server.py" /opt/ygo-pvp/app/pvp/server.py
sudo -n install -m 644 -o ygo-pvp -g ygo-pvp \
    "$stage/pvp/protocol.py" /opt/ygo-pvp/app/pvp/protocol.py
sudo -n install -m 644 -o ygo-pvp -g ygo-pvp \
    "$stage/pvp/deck.py" /opt/ygo-pvp/app/pvp/deck.py
sudo -n install -m 644 -o ygo-pvp -g ygo-pvp \
    "$stage/pvp/__init__.py" /opt/ygo-pvp/app/pvp/__init__.py
sudo -n install -m 644 -o ygo-evolution -g ygo-evolution \
    "$stage/YGOProRoom.js" /opt/ygo-evolution/dist/src/ygopro/room/domain/YGOProRoom.js
sudo -n install -m 644 -o ygo-evolution -g ygo-evolution \
    "$stage/SpectatorView.js" /opt/ygo-evolution/dist/src/ygopro/room/domain/SpectatorView.js
sudo -n install -m 644 -o ygo-evolution -g ygo-evolution \
    "$stage/ocgcore.js" /opt/ygo-evolution/dist/src/ygopro/ocgcore-worker/ocgcore.js
sudo -n install -m 644 -o ygo-evolution -g ygo-evolution \
    "$stage/YGOProDuelingState.js" \
    /opt/ygo-evolution/dist/src/ygopro/room/domain/states/YGOProDuelingState.js

sudo -n /opt/node-v24.14.0/bin/node --check \
    /opt/ygo-evolution/dist/src/ygopro/room/domain/YGOProRoom.js
sudo -n /opt/node-v24.14.0/bin/node --check \
    /opt/ygo-evolution/dist/src/ygopro/room/domain/SpectatorView.js
sudo -n /opt/node-v24.14.0/bin/node --check \
    /opt/ygo-evolution/dist/src/ygopro/ocgcore-worker/ocgcore.js
sudo -n /opt/node-v24.14.0/bin/node --check \
    /opt/ygo-evolution/dist/src/ygopro/room/domain/states/YGOProDuelingState.js
sudo -n /opt/ygo-tournament/venv/bin/python -m py_compile \
    /opt/ygo-tournament/app/tournament/server.py
sudo -n /opt/ygo-pvp/venv/bin/python -m py_compile \
    /opt/ygo-pvp/app/pvp/server.py
sudo -n /opt/ygo-pvp/venv/bin/python -m py_compile \
    /opt/ygo-pvp/app/pvp/protocol.py \
    /opt/ygo-pvp/app/pvp/deck.py

sudo -n systemctl restart ygo-evolution ygo-pvp ygo-tournament
sudo -n systemctl is-active ygo-evolution ygo-pvp ygo-tournament
for url in http://127.0.0.1:8787/health http://127.0.0.1:8790/health; do
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
