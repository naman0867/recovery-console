#!/usr/bin/env bash
# Recovery Console runner.
#
#   ./run.sh eval      full 50k evaluation + ablation study
#   ./run.sh quick     5k smoke run, ~2 seconds
#   ./run.sh console   start the live console on :8000
#
# Set ANTHROPIC_API_KEY to activate the tier-2 model classifier. Without it the
# long-tail path runs on keyword fallback and every report says which mode it used.

set -euo pipefail
cd "$(dirname "$0")"

case "${1:-console}" in
  eval)
    python3 -m recovery.evaluate --n 50000 --days 14
    ;;
  quick)
    python3 -m recovery.evaluate --n 5000 --days 7 --no-ablate
    ;;
  console)
    echo "Generating the session, this takes a few seconds..."
    echo "Console will be at http://127.0.0.1:8000"
    SESSION_N="${SESSION_N:-50000}" \
    SESSION_DAYS="${SESSION_DAYS:-14}" \
      python3 -m uvicorn api.main:app --host 127.0.0.1 --port 8000
    ;;
  *)
    echo "usage: ./run.sh [eval|quick|console]" >&2
    exit 1
    ;;
esac
