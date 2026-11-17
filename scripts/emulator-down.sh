#!/usr/bin/env bash
# Stop the emulator container(s) started by emulator-up.sh. No other container is touched.
# Each emulator deletes ~/.aws/emulator/<port>/ and all in-memory state when it stops.
#
#   ./scripts/emulator-down.sh          # every emulator started by these scripts
#   EMULATOR_PORT=9399 ./scripts/emulator-down.sh --port-only   # just aws-emulator-9399
set -euo pipefail
PORT="${EMULATOR_PORT:-9297}"
LABEL="aws-emulator-poc"

if [[ "${1:-}" == "--port-only" ]]; then
  targets="aws-emulator-${PORT}"
else
  # Labelled emulators, plus the default name (covers containers from older versions of this script).
  targets="$(docker ps --filter "label=${LABEL}.role=emulator" --format '{{.Names}}') aws-emulator-${PORT}"
fi

stopped=0
for c in ${targets}; do
  if docker ps --format '{{.Names}}' | grep -qx "${c}"; then
    docker stop "${c}" >/dev/null
    echo "==> Stopped ${c}"
    stopped=1
  fi
done
[[ "${stopped}" == "1" ]] || echo "==> No emulator running"

if [[ "${1:-}" != "--port-only" && "${PORT}" == "9297" ]]; then
  : > "${HOME}/.aws/emulator/regions" 2>/dev/null || true
fi
