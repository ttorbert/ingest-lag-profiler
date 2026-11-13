#!/usr/bin/env bash
# Prepare Rancher Desktop for the Elasticsearch lab.
#
#   rancher-setup.sh            check the engine and memory, and raise vm.max_map_count
#                               until the VM next restarts
#   rancher-setup.sh --persist  also write a Rancher Desktop provisioning override so the
#                               setting survives VM restarts
set -euo pipefail

RD="${HOME}/.rd/bin"
DOCKER="$(command -v docker || echo "${RD}/docker")"
RDCTL="$(command -v rdctl || echo "${RD}/rdctl")"

if [ ! -x "${RDCTL}" ]; then
  echo "Rancher Desktop not found (no rdctl). Install it from https://rancherdesktop.io" >&2
  exit 1
fi

settings="$("${RDCTL}" list-settings)"
engine="$(printf '%s' "${settings}" | python3 -c 'import json,sys;print(json.load(sys.stdin)["containerEngine"]["name"])')"
mem="$(printf '%s' "${settings}" | python3 -c 'import json,sys;print(json.load(sys.stdin)["virtualMachine"]["memoryInGB"])')"

if [ "${engine}" != "moby" ]; then
  echo "Rancher Desktop uses the ${engine} engine. Switch to dockerd (moby) in Preferences > Container Engine," >&2
  echo "or run: ${RDCTL} set --container-engine.name moby" >&2
  exit 1
fi
if [ "${mem}" -lt 6 ]; then
  echo "Rancher Desktop VM has ${mem} GB; the lab needs at least 6 (8 recommended)." >&2
  echo "Run: ${RDCTL} set --virtual-machine.memory-in-gb 8" >&2
  exit 1
fi

current="$("${RDCTL}" shell sysctl -n vm.max_map_count)"
if [ "${current}" -lt 262144 ]; then
  "${RDCTL}" shell sudo sysctl -q -w vm.max_map_count=262144
  echo "Raised vm.max_map_count from ${current} to 262144 (until the VM restarts)"
fi

if [ "${1:-}" = "--persist" ]; then
  override="${HOME}/Library/Application Support/rancher-desktop/lima/_config/override.yaml"
  if [ -f "${override}" ] && grep -q max_map_count "${override}"; then
    echo "Provisioning override already sets vm.max_map_count"
  elif [ -f "${override}" ]; then
    echo "An override file already exists at:" >&2
    echo "  ${override}" >&2
    echo "Add a provision step with 'sysctl -w vm.max_map_count=262144' to it by hand." >&2
    exit 1
  else
    mkdir -p "$(dirname "${override}")"
    cat > "${override}" <<'YAML'
provision:
  - mode: system
    script: |
      #!/bin/sh
      sysctl -w vm.max_map_count=262144
YAML
    echo "Wrote ${override}"
  fi
fi

"${DOCKER}" compose version >/dev/null
echo "Rancher Desktop ready: engine ${engine}, ${mem} GB"
