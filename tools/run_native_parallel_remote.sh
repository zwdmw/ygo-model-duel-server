#!/usr/bin/env bash
set -euo pipefail

stage="${1:?provide an extracted test bundle directory}"
source /etc/ygo-tournament.env
export TOURNAMENT_ADMIN_KEY
export TOURNAMENT_URL=http://127.0.0.1:8787
export PVP_HOST=127.0.0.1
export CARDS_CDB=/opt/ygo-evolution/resources/current/ygopro/base/cards.cdb
export PYTHONPATH="$stage"
cd "$stage"
exec /opt/ygo-pvp/venv/bin/python -m tools.native_parallel_series_e2e
