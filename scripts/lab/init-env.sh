#!/usr/bin/env bash
# Create scripts/lab/.env from .env.example with random secrets, if it doesn't exist.
set -euo pipefail
cd "$(dirname "$0")"
if [ -f .env ]; then exit 0; fi
rand() { LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom | head -c "$1"; }
sed -e "s/^ELASTIC_PASSWORD=.*/ELASTIC_PASSWORD=$(rand 20)/" \
    -e "s/^KIBANA_PASSWORD=.*/KIBANA_PASSWORD=$(rand 20)/" \
    -e "s/^KIBANA_ENCRYPTION_KEY=.*/KIBANA_ENCRYPTION_KEY=$(rand 40)/" \
    .env.example > .env
chmod 600 .env
echo "Created scripts/lab/.env with random passwords"
