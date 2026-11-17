#!/usr/bin/env bash
# Build (if needed) and start the AWS Emulator(s), then wait for health.
#
#   ./scripts/emulator-up.sh                                  # one emulator (all regions share it)
#   REGIONS=us-east-1,eu-west-2 ./scripts/emulator-up.sh      # one isolated emulator per region
#
# With REGIONS, region i gets port EMULATOR_PORT+i. Each emulator is a standalone container that
# publishes its own 127.0.0.1:<port> and joins the shared Docker network `aws-emulator`, where other
# containers can reach it as `aws-emulator-<port>`.
# These scripts only ever start/stop emulator containers.
set -euo pipefail

PORT="${EMULATOR_PORT:-9297}"
IMAGE="${EMULATOR_IMAGE:-aws-emulator}"
NETWORK="${EMULATOR_NETWORK:-aws-emulator}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
EMU_DIR="${HOME}/.aws/emulator"
LABEL="aws-emulator-poc"
TELEMETRY="${EMULATOR_TELEMETRY:-off}"           # off unless EMULATOR_TELEMETRY=on (image default is off too)

case "$(uname -m)" in
  arm64|aarch64) ARCH=aarch64 ;;
  *) ARCH=x86_64 ;;
esac

# --- regions -> ports -------------------------------------------------------------------------
IFS=',' read -r -a REGION_LIST <<< "${REGIONS:-}"
declare -a PORTS=()
REGION_PORTS=""
for i in "${!REGION_LIST[@]}"; do
  region="$(echo "${REGION_LIST[$i]}" | tr -d '[:space:]')"
  if [[ ! "${region}" =~ ^[a-z]{2}(-[a-z]+)+-[0-9]{1,2}$ ]]; then
    echo "!!  invalid region in REGIONS: '${region}'" >&2
    exit 1
  fi
  REGION_LIST[i]="${region}"
  PORTS+=("$((PORT + i))")
  REGION_PORTS+="${REGION_PORTS:+,}${region}=$((PORT + i))"
done
if [[ ${#PORTS[@]} -eq 0 ]]; then
  PORTS=("${PORT}")
fi

running() { docker ps --format '{{.Names}}' | grep -qx "$1"; }

wait_healthy() {  # $1=port $2=container
  local port="$1" name="$2" state="${EMU_DIR}/$1"
  echo -n "==> Waiting for ${name} (127.0.0.1:${port})"
  for _ in $(seq 1 60); do
    if [[ -f "${state}/ca.pem" ]] && \
       curl -fsS --cacert "${state}/ca.pem" "https://127.0.0.1:${port}/__admin/health" >/dev/null 2>&1; then
      echo " OK"
      return 0
    fi
    echo -n "."
    sleep 1
  done
  echo " FAILED"
  docker logs "${name}" 2>&1 | tail -20 || true
  return 1
}

# Must exist before `docker run`, otherwise Docker creates it owned by root.
mkdir -p "${EMU_DIR}"

if [[ -z "$(docker images -q "${IMAGE}" 2>/dev/null)" || "${REBUILD:-0}" == "1" ]]; then
  echo "==> Building ${IMAGE} (${ARCH})"
  docker build --build-arg "EMULATOR_ARCH=${ARCH}" -t "${IMAGE}" "${ROOT}/docker"
fi

# Shared network (a Docker network, not a container): created once, left in place for other users.
docker network inspect "${NETWORK}" >/dev/null 2>&1 || docker network create "${NETWORK}" >/dev/null

for i in "${!PORTS[@]}"; do
  p="${PORTS[$i]}"
  emulator="aws-emulator-${p}"
  if running "${emulator}"; then
    echo "==> ${emulator} already running"
  else
    echo "==> Starting ${emulator} on 127.0.0.1:${p}${REGION_LIST[$i]:+ (${REGION_LIST[$i]})}"
    docker run -d --rm --name "${emulator}" --label "${LABEL}.role=emulator" \
      --user "$(id -u):$(id -g)" \
      --memory=2g --memory-swap=2g \
      --network "${NETWORK}" \
      -p "127.0.0.1:${p}:${p}" \
      -e HOME="${HOME}" \
      -e EMULATOR_TELEMETRY="${TELEMETRY}" \
      -v "${EMU_DIR}:${EMU_DIR}" \
      "${IMAGE}" -p "${p}" --telemetry "${TELEMETRY}" >/dev/null
  fi
  wait_healthy "${p}" "${emulator}"
done

# region -> port map for `source scripts/emulator-env.sh <region>` (and read live by the web console).
# Only the main group (default port) owns it, so a throwaway emulator on another port can't clobber it.
if [[ "${PORT}" == "9297" ]]; then
  if [[ -n "${REGION_PORTS}" ]]; then
    tr ',' '\n' <<< "${REGION_PORTS}" > "${EMU_DIR}/regions"
  else
    : > "${EMU_DIR}/regions"
  fi
fi

if [[ -n "${REGION_PORTS}" ]]; then
  echo "==> Regions: ${REGION_PORTS//,/  }"
  echo "==> Ready. Run: source scripts/emulator-env.sh [region]"
else
  echo "==> Ready. Run: source scripts/emulator-env.sh"
fi
