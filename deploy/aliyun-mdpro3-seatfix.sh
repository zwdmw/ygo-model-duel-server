#!/usr/bin/env bash
set -euo pipefail

stage="${1:?provide the uploaded patch directory}"
test -f "$stage/pvp/server.py"
test -f "$stage/pvp/protocol.py"

stamp="$(date -u +%Y%m%dT%H%M%SZ)"
backup="/opt/ygo-pvp/backups/mdpro3-seatfix-$stamp"
sudo -n install -d -m 755 "$backup"
sudo -n cp -a /opt/ygo-pvp/app/pvp/server.py "$backup/server.py"
sudo -n cp -a /opt/ygo-pvp/app/pvp/protocol.py "$backup/protocol.py"

sudo -n /opt/ygo-pvp/venv/bin/python -m py_compile \
    "$stage/pvp/server.py" "$stage/pvp/protocol.py"

sudo -n install -m 644 -o ygo-pvp -g ygo-pvp \
    "$stage/pvp/server.py" /opt/ygo-pvp/app/pvp/server.py
sudo -n install -m 644 -o ygo-pvp -g ygo-pvp \
    "$stage/pvp/protocol.py" /opt/ygo-pvp/app/pvp/protocol.py
sudo -n /opt/ygo-pvp/venv/bin/python -m py_compile \
    /opt/ygo-pvp/app/pvp/server.py /opt/ygo-pvp/app/pvp/protocol.py

sudo -n systemctl restart ygo-pvp
healthy=0
for _ in $(seq 1 30); do
    if curl -fsS --max-time 2 http://127.0.0.1:8790/health >/tmp/ygo-pvp-health.json; then
        healthy=1
        break
    fi
    sleep 1
done
test "$healthy" = 1
cat /tmp/ygo-pvp-health.json
printf '\nbackup=%s\n' "$backup"
printf 'protocol_sha256='
sudo -n sha256sum /opt/ygo-pvp/app/pvp/protocol.py | awk '{print $1}'
printf 'server_sha256='
sudo -n sha256sum /opt/ygo-pvp/app/pvp/server.py | awk '{print $1}'
sudo -n systemctl is-active ygo-pvp
