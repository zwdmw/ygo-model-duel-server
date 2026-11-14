#!/usr/bin/env bash
set -euo pipefail

# Complete the Aliyun installation after the host is reachable again.
# The script keeps the existing tournament data and model bridge intact.
ARCHIVE="${1:-/tmp/ygo-gateway-source.tar.gz}"
EVOLUTION_ROOT="/opt/ygo-evolution"
TOURNAMENT_ROOT="/opt/ygo-tournament"
PVP_ROOT="/opt/ygo-pvp"
NODE_BIN="/opt/node-v24.14.0/bin"
PUBLIC_HOST="${PVP_PUBLIC_HOST:-47.97.218.63}"
PNPM_INSTALL_TIMEOUT_SECONDS="${PNPM_INSTALL_TIMEOUT_SECONDS:-900}"
PNPM_REBUILD_TIMEOUT_SECONDS="${PNPM_REBUILD_TIMEOUT_SECONDS:-600}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
STAGE="$(mktemp -d "/tmp/ygo-gateway-activate.XXXXXX")"
EVOLUTION_PATCH_ROOT="$STAGE/deploy/evolution-patch"

cleanup() {
    rm -rf "$STAGE"
}
trap cleanup EXIT

test -f "$ARCHIVE"
if [[ -n "${GATEWAY_SHA256:-}" ]]; then
    printf '%s  %s\n' "$GATEWAY_SHA256" "$ARCHIVE" | sha256sum -c -
fi

tar -xzf "$ARCHIVE" -C "$STAGE"
test -f "$STAGE/pvp/server.py"
test -f "$STAGE/tournament/server.py"
test -f "$STAGE/decks/SkyStriker.ydk"
test -f "$STAGE/deploy/ygo-evolution.service.example"
test -f "$EVOLUTION_PATCH_ROOT/YGOProRoom.js"
test -f "$EVOLUTION_PATCH_ROOT/SeriesReplayView.js"
test -f "$EVOLUTION_PATCH_ROOT/ocgcore.js"
test -f "$EVOLUTION_PATCH_ROOT/YGOProWaitingState.js"

id ygo-pvp >/dev/null 2>&1 || useradd --system --home-dir "$PVP_ROOT" \
    --create-home --shell /usr/sbin/nologin ygo-pvp
id ygo-evolution >/dev/null 2>&1 || useradd --system --home-dir "$EVOLUTION_ROOT" \
    --create-home --shell /usr/sbin/nologin ygo-evolution

install -d -m 755 -o ygo-tournament -g ygo-tournament "$TOURNAMENT_ROOT/app/tournament"
install -d -m 755 -o ygo-pvp -g ygo-pvp "$PVP_ROOT/app/pvp" "$PVP_ROOT/backups"
install -d -m 755 -o ygo-evolution -g ygo-evolution "$EVOLUTION_ROOT/backups"
install -d -m 755 -o root -g root /etc/ygo-pvp/fixed-decks

if [[ -d "$TOURNAMENT_ROOT/app/tournament" ]]; then
    cp -a "$TOURNAMENT_ROOT/app/tournament" \
        "$TOURNAMENT_ROOT/backups/tournament-$STAMP"
fi
if [[ -d "$PVP_ROOT/app/pvp" ]]; then
    cp -a "$PVP_ROOT/app/pvp" "$PVP_ROOT/backups/pvp-$STAMP"
fi

cp -a "$STAGE/tournament/." "$TOURNAMENT_ROOT/app/tournament/"
cp -a "$STAGE/pvp/." "$PVP_ROOT/app/pvp/"
chown -R ygo-tournament:ygo-tournament "$TOURNAMENT_ROOT/app/tournament"
chown -R ygo-pvp:ygo-pvp "$PVP_ROOT/app/pvp"
install -m 644 -o root -g root "$STAGE/decks/SkyStriker.ydk" \
    /etc/ygo-pvp/fixed-decks/SkyStriker.ydk
rm -f /etc/ygo-pvp/fixed-decks/MirrorForce_SkyStriker.ydk

# Evolution is an installed native runtime rather than part of this Python
# source tree. Keep a dated backup of the exact compiled files before applying
# the deterministic tournament-room adapter.
EVOLUTION_PATCH_BACKUP="$EVOLUTION_ROOT/backups/deterministic-pvp-$STAMP"
install -d -m 755 -o ygo-evolution -g ygo-evolution "$EVOLUTION_PATCH_BACKUP"
for relative in \
    dist/src/ygopro/room/domain/SeriesReplayView.js \
    dist/src/ygopro/room/domain/YGOProRoom.js \
    dist/src/ygopro/ocgcore-worker/ocgcore.js \
    dist/src/ygopro/room/domain/states/YGOProWaitingState.js; do
    if [[ -f "$EVOLUTION_ROOT/$relative" ]]; then
        cp -a "$EVOLUTION_ROOT/$relative" "$EVOLUTION_PATCH_BACKUP/$(basename "$relative")"
    fi
done
install -m 644 -o ygo-evolution -g ygo-evolution \
    "$EVOLUTION_PATCH_ROOT/SeriesReplayView.js" \
    "$EVOLUTION_ROOT/dist/src/ygopro/room/domain/SeriesReplayView.js"
install -m 644 -o ygo-evolution -g ygo-evolution \
    "$EVOLUTION_PATCH_ROOT/YGOProRoom.js" \
    "$EVOLUTION_ROOT/dist/src/ygopro/room/domain/YGOProRoom.js"
install -m 644 -o ygo-evolution -g ygo-evolution \
    "$EVOLUTION_PATCH_ROOT/ocgcore.js" \
    "$EVOLUTION_ROOT/dist/src/ygopro/ocgcore-worker/ocgcore.js"
install -m 644 -o ygo-evolution -g ygo-evolution \
    "$EVOLUTION_PATCH_ROOT/YGOProWaitingState.js" \
    "$EVOLUTION_ROOT/dist/src/ygopro/room/domain/states/YGOProWaitingState.js"

if ! swapon --show=NAME --noheadings | grep -q .; then
    if [[ ! -f /swapfile ]]; then
        fallocate -l 1G /swapfile
        chmod 600 /swapfile
        mkswap /swapfile >/dev/null
    fi
    swapon /swapfile
fi
grep -qE '^/swapfile[[:space:]]' /etc/fstab || \
    printf '/swapfile none swap sw 0 0\n' >> /etc/fstab

if [[ ! -x "$PVP_ROOT/venv/bin/python" ]]; then
    python3 -m venv "$PVP_ROOT/venv"
fi
"$PVP_ROOT/venv/bin/python" -m pip install --disable-pip-version-check \
    --no-cache-dir --index-url https://mirrors.aliyun.com/pypi/simple \
    -r "$PVP_ROOT/app/pvp/requirements.txt"

test -x "$NODE_BIN/node"
test -x "$NODE_BIN/corepack"
chown -R ygo-evolution:ygo-evolution "$EVOLUTION_ROOT"

if [[ ! -f "$EVOLUTION_ROOT/node_modules/better-sqlite3/build/Release/better_sqlite3.node" ]] ||
   [[ ! -f "$EVOLUTION_ROOT/node_modules/koishipro-core.js/package.json" ]]; then
    install -d -o ygo-evolution -g ygo-evolution "$EVOLUTION_ROOT/node_modules"
    runuser -u ygo-evolution -- env \
        EVOLUTION_ROOT="$EVOLUTION_ROOT" \
        NODE_BIN="$NODE_BIN" \
        PNPM_INSTALL_TIMEOUT_SECONDS="$PNPM_INSTALL_TIMEOUT_SECONDS" \
        PNPM_REBUILD_TIMEOUT_SECONDS="$PNPM_REBUILD_TIMEOUT_SECONDS" \
        COREPACK_ENABLE_DOWNLOAD_PROMPT=0 \
        NPM_CONFIG_AUDIT=false \
        NPM_CONFIG_FUND=false \
        NPM_CONFIG_REGISTRY="${NPM_CONFIG_REGISTRY:-https://registry.npmmirror.com}" \
        bash -c '
            set -euo pipefail
            cd "$EVOLUTION_ROOT"
            timeout "${PNPM_INSTALL_TIMEOUT_SECONDS}s" "$NODE_BIN/corepack" pnpm install \
                --prod \
                --frozen-lockfile \
                --ignore-scripts \
                --child-concurrency=1 \
                --network-concurrency=1 \
                --reporter=append-only
            timeout "${PNPM_REBUILD_TIMEOUT_SECONDS}s" "$NODE_BIN/corepack" pnpm rebuild \
                better-sqlite3
            timeout "${PNPM_REBUILD_TIMEOUT_SECONDS}s" "$NODE_BIN/corepack" pnpm rebuild \
                lzma-native
            "$NODE_BIN/node" -e "for (const name of ['\''better-sqlite3'\'', '\''lzma-native'\'', '\''koishipro-core.js'\'']) require(name)"
        '
fi
test -f "$EVOLUTION_ROOT/node_modules/better-sqlite3/build/Release/better_sqlite3.node"
test -f "$EVOLUTION_ROOT/node_modules/koishipro-core.js/package.json"

if [[ -f /etc/ygo-tournament.env ]]; then
    admin_key="$(sed -n 's/^TOURNAMENT_ADMIN_KEY=//p' /etc/ygo-tournament.env | head -n 1)"
else
    admin_key=""
fi
if [[ -z "$admin_key" ]]; then
    admin_key="$(openssl rand -base64 36 | tr -d '\n')"
fi

if [[ -f /etc/ygo-pvp.env ]]; then
    pvp_key="$(sed -n 's/^PVP_INTERNAL_KEY=//p' /etc/ygo-pvp.env | head -n 1)"
    callback_key="$(sed -n 's/^TOURNAMENT_INTERNAL_KEY=//p' /etc/ygo-pvp.env | head -n 1)"
else
    pvp_key=""
    callback_key=""
fi
[[ -n "$pvp_key" ]] || pvp_key="$(openssl rand -base64 36 | tr -d '\n')"
[[ -n "$callback_key" ]] || callback_key="$(openssl rand -base64 36 | tr -d '\n')"

write_env() {
    local target="$1"
    shift
    local temporary="${target}.tmp"
    umask 077
    printf '%s\n' "$@" > "$temporary"
    chown root:root "$temporary"
    chmod 600 "$temporary"
    mv "$temporary" "$target"
}

write_env /etc/ygo-tournament.env \
    "TOURNAMENT_ADMIN_KEY=$admin_key" \
    "PVP_URL=http://127.0.0.1:8790" \
    "PVP_INTERNAL_KEY=$pvp_key" \
    "TOURNAMENT_INTERNAL_KEY=$callback_key" \
    "PVP_PUBLIC_HOST=$PUBLIC_HOST"

write_env /etc/ygo-pvp.env \
    "PVP_INTERNAL_KEY=$pvp_key" \
    "PVP_TOURNAMENT_URL=http://127.0.0.1:8787" \
    "TOURNAMENT_INTERNAL_KEY=$callback_key" \
    "PVP_FIXED_DECK=/etc/ygo-pvp/fixed-decks/SkyStriker.ydk"

if [[ ! -f "$EVOLUTION_ROOT/.env" ]]; then
    write_env "$EVOLUTION_ROOT/.env" \
        "HOST_PORT=7911" \
        "YGOPRO_PORT=7711" \
        "HTTP_PORT=7922" \
        "WEBSOCKET_PORT=4000" \
        "YGOPRO_WEBSOCKET_PORT=4002" \
        "WEBSOCKET_DUEL_PORT=4001" \
        "RESOURCES_DIR=./resources/current" \
        "MANIFEST_PATH=./resources.manifest.json" \
        "USE_REDIS=false" \
        "RANK_ENABLED=false" \
        "RATE_LIMIT_ENABLED=true" \
        "RATE_LIMIT=60" \
        "RATE_LIMIT_WINDOW=60" \
        "RATE_LIMIT_JOIN=60" \
        "RATE_LIMIT_JOIN_WINDOW=60" \
        "NODE_ENV=production" \
        "ADMIN_API_KEY=$(openssl rand -base64 36 | tr -d '\n')" \
        "SIDE_TIMEOUT_MINUTES=3"
    chown ygo-evolution:ygo-evolution "$EVOLUTION_ROOT/.env"
fi

install -m 644 "$STAGE/deploy/ygo-evolution.service.example" \
    /etc/systemd/system/ygo-evolution.service
install -m 644 "$STAGE/deploy/ygo-pvp.service.example" \
    /etc/systemd/system/ygo-pvp.service
install -m 644 "$STAGE/deploy/ygo-tournament.service.example" \
    /etc/systemd/system/ygo-tournament.service

systemctl daemon-reload
systemctl enable ygo-evolution ygo-pvp ygo-tournament >/dev/null
systemctl restart ygo-evolution

for _ in $(seq 1 60); do
    if curl -fsS --max-time 2 http://127.0.0.1:7922/ >/dev/null 2>&1; then
        break
    fi
    sleep 2
done
curl -fsS --max-time 5 http://127.0.0.1:7922/ >/dev/null

systemctl restart ygo-pvp
for _ in $(seq 1 20); do
    if curl -fsS --max-time 2 http://127.0.0.1:8790/health >/dev/null 2>&1; then
        break
    fi
    sleep 1
done
curl -fsS --max-time 5 http://127.0.0.1:8790/health

systemctl restart ygo-tournament
for _ in $(seq 1 20); do
    if curl -fsS --max-time 2 http://127.0.0.1:8787/health >/dev/null 2>&1; then
        break
    fi
    sleep 1
done
curl -fsS --max-time 5 http://127.0.0.1:8787/health

systemctl --no-pager --full status ygo-evolution ygo-pvp ygo-tournament \
    | sed -n '1,100p'
ss -ltnp | grep -E ':(6008|6009|7711|7922|8787|8790)\b'
