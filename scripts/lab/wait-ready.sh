#!/usr/bin/env bash
# Wait until the lab's Kibana reports "available" (up to 10 minutes).
# `docker compose up --wait` can't be used: it treats the one-time setup
# container's normal exit as a failure.
set -euo pipefail
cd "$(dirname "$0")"
set -a; . ./.env; set +a
deadline=$(( $(date +%s) + 600 ))
printf "Waiting for Kibana"
until curl -s -u "elastic:${ELASTIC_PASSWORD}" "http://localhost:${KIBANA_PORT}/api/status" \
      | grep -q '"level":"available"'; do
  if [ "$(date +%s)" -ge "${deadline}" ]; then
    echo; echo "Kibana wasn't ready after 10 minutes. See: make lab-logs" >&2
    exit 1
  fi
  printf "."; sleep 5
done
echo " ready"
