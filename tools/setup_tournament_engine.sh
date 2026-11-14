#!/usr/bin/env bash
set -euo pipefail
root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
engine="${1:-$root/runtime/evolution}"
test "$(node --version)" = v24.14.0 || {
    echo 'Install Node.js 24.14.0 (including corepack) and put its bin directory on PATH.' >&2
    exit 1
}
test -f "$engine/pnpm-lock.yaml"
test -f "$engine/dist/src/index.js"
cd -- "$engine"
export COREPACK_ENABLE_DOWNLOAD_PROMPT=0
export npm_config_python="${npm_config_python:-python3}"
# Skip the upstream development-only husky prepare hook, then explicitly build
# the production bindings. package.json and the lock pin the package manager.
corepack pnpm install --prod --frozen-lockfile --ignore-scripts
corepack pnpm rebuild better-sqlite3 lzma-native bcrypt
node -e 'for (const name of ["better-sqlite3", "lzma-native", "koishipro-core.js"]) require(name)'
echo 'Tournament engine dependencies are ready.'
