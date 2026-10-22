#!/usr/bin/env bash
# Start the simulated tools, the FabLink host and (in dev mode) the Vite server.
# Ctrl-C stops everything.
set -euo pipefail
cd "$(dirname "$0")/.."

SPEED="${SIM_SPEED:-3}"
PY=backend/.venv/bin/python
[ -x "$PY" ] || { echo "Run 'make setup' first."; exit 1; }

pids=()
cleanup() { kill "${pids[@]}" 2>/dev/null || true; }
trap cleanup EXIT INT TERM

(cd backend && ../$PY -m simulator --speed "$SPEED") &
pids+=($!)
sleep 1

if [ "${1:-}" = "--demo" ]; then
  (cd backend && ../$PY -m uvicorn app.main:app --port 8000) &
  pids+=($!)
  echo; echo "  FabLink console: http://localhost:8000"; echo
else
  (cd backend && ../$PY -m uvicorn app.main:app --port 8000 --reload) &
  pids+=($!)
  (cd frontend && npm run dev -- --port 5173) &
  pids+=($!)
  echo; echo "  FabLink console: http://localhost:5173  (API on :8000, docs at :8000/docs)"; echo
fi
wait
