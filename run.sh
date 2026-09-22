#!/usr/bin/env bash
# Bring the whole console up: database, API, front end.
#
#   ./run.sh            start everything (TigerGraph must already be up)
#   ./run.sh --graph    bring TigerGraph Community Edition up first
set -euo pipefail

[[ "${1:-}" == "--graph" ]] && ./graph/local_tigergraph.sh up

echo "-- api   : http://localhost:8000"
uv run uvicorn server:app --port 8000 &
API=$!
trap 'kill $API 2>/dev/null || true' EXIT

until curl -sf http://localhost:8000/api/cases >/dev/null; do sleep 1; done
echo "-- console: http://localhost:5180"
cd dashboard-app && npm run dev
