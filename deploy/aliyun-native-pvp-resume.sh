#!/usr/bin/env bash
set -euo pipefail

# Run after SSH/ECS recovery. This script only prepares the Evolution
# dependency tree; service installation and secret rotation remain explicit.
APP_ROOT="${APP_ROOT:-/opt/ygo-evolution}"
NODE_BIN="${NODE_BIN:-/opt/node-v24.14.0/bin}"
PNPM_INSTALL_TIMEOUT_SECONDS="${PNPM_INSTALL_TIMEOUT_SECONDS:-900}"
PNPM_REBUILD_TIMEOUT_SECONDS="${PNPM_REBUILD_TIMEOUT_SECONDS:-600}"
export COREPACK_ENABLE_DOWNLOAD_PROMPT=0
export NPM_CONFIG_AUDIT=false
export NPM_CONFIG_FUND=false
export NPM_CONFIG_REGISTRY="${NPM_CONFIG_REGISTRY:-https://registry.npmmirror.com}"

test -x "$NODE_BIN/node"
test -f "$APP_ROOT/package.json"
test -f "$APP_ROOT/pnpm-lock.yaml"

runuser -u ygo-evolution -- env \
  APP_ROOT="$APP_ROOT" \
  NODE_BIN="$NODE_BIN" \
  PNPM_INSTALL_TIMEOUT_SECONDS="$PNPM_INSTALL_TIMEOUT_SECONDS" \
  PNPM_REBUILD_TIMEOUT_SECONDS="$PNPM_REBUILD_TIMEOUT_SECONDS" \
  bash -c '
    set -euo pipefail
    cd "$APP_ROOT"
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
    "$NODE_BIN/node" -e "for (const name of ['better-sqlite3', 'lzma-native', 'koishipro-core.js']) require(name)"
  '

test -f "$APP_ROOT/node_modules/better-sqlite3/build/Release/better_sqlite3.node"
test -f "$APP_ROOT/node_modules/koishipro-core.js/package.json"
echo "Evolution production dependencies are ready."
