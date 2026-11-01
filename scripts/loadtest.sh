#!/usr/bin/env bash
# Load test (#34): step the number of concurrent claims, once with 1 agent-worker and once
# with 3. Stub provider at 1.5 s per call (STUB_DELAY), result cache off, rate limit lifted.
#
#   scripts/loadtest.sh                 # both runs, steps 1 5 10 15 20 users, 90 s each
#   STEPS="10" DURATION=60s scripts/loadtest.sh 1
#
# Results: perf/results/w<workers>-u<users>_stats.csv, summarised by scripts/perf_summary.py.
set -euo pipefail
cd "$(dirname "$0")/.."

WORKER_COUNTS=("${@:-1 3}")
STEPS=${STEPS:-"1 5 10 15 20"}
DURATION=${DURATION:-90s}
export STUB_DELAY=${STUB_DELAY:-1.5}
COMPOSE=(docker compose -f docker-compose.yml -f deploy/locust/compose.loadtest.yml --profile loadtest)

for workers in ${WORKER_COUNTS[*]}; do
  echo "=== agent-worker replicas: $workers (STUB_DELAY=$STUB_DELAY)"
  "${COMPOSE[@]}" up -d --build --wait --scale worker="$workers" api worker verifier-worker llm-stub
  for users in $STEPS; do
    "${COMPOSE[@]}" exec -T redis redis-cli FLUSHDB >/dev/null
    echo "--- $users concurrent users for $DURATION"
    "${COMPOSE[@]}" run --rm --no-deps locust \
      -f /mnt/locust/locustfile.py --host http://api:8000 --headless \
      --users "$users" --spawn-rate "$users" --run-time "$DURATION" --stop-timeout 180 \
      --csv "/mnt/results/w${workers}-u${users}" --only-summary
  done
done
uv run python scripts/perf_summary.py
