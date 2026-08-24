#!/usr/bin/env bash
set -euo pipefail

SERVICE_NAME="india-alt-inference-t4.service"
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
COMPOSE_FILE="${REPO_DIR}/docker-compose.inference.t4.yml"
ENV_FILE="${T4_INFERENCE_ENV_FILE:-${REPO_DIR}/.env.inference.t4}"
TEMPLATE_FILE="${REPO_DIR}/deploy/systemd/${SERVICE_NAME}.template"
DOCKER_BIN="$(command -v docker || true)"

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run this installer as root: sudo $0" >&2
  exit 1
fi

if [[ -z "${DOCKER_BIN}" ]]; then
  echo "Docker is not installed or is not on PATH." >&2
  exit 1
fi

if ! "${DOCKER_BIN}" compose version >/dev/null 2>&1; then
  echo "The Docker Compose plugin is not available." >&2
  exit 1
fi

for required_file in "${COMPOSE_FILE}" "${ENV_FILE}" "${TEMPLATE_FILE}"; do
  if [[ ! -f "${required_file}" ]]; then
    echo "Required file not found: ${required_file}" >&2
    exit 1
  fi
done

if [[ ! -d "${REPO_DIR}/../indian-alt-docproc" ]]; then
  echo "Expected sibling repository not found: ${REPO_DIR}/../indian-alt-docproc" >&2
  exit 1
fi

escape_sed_replacement() {
  printf '%s' "$1" | sed 's/[&|]/\\&/g'
}

repo_dir_escaped="$(escape_sed_replacement "${REPO_DIR}")"
compose_file_escaped="$(escape_sed_replacement "${COMPOSE_FILE}")"
env_file_escaped="$(escape_sed_replacement "${ENV_FILE}")"
docker_bin_escaped="$(escape_sed_replacement "${DOCKER_BIN}")"

sed \
  -e "s|__REPO_DIR__|${repo_dir_escaped}|g" \
  -e "s|__COMPOSE_FILE__|${compose_file_escaped}|g" \
  -e "s|__ENV_FILE__|${env_file_escaped}|g" \
  -e "s|__DOCKER_BIN__|${docker_bin_escaped}|g" \
  "${TEMPLATE_FILE}" > "/etc/systemd/system/${SERVICE_NAME}"

systemctl daemon-reload
systemctl enable --now docker.service
systemctl enable --now "${SERVICE_NAME}"

echo
echo "Installed and started ${SERVICE_NAME}."
echo "Check boot configuration: systemctl is-enabled ${SERVICE_NAME}"
echo "Check service state:      systemctl status ${SERVICE_NAME} --no-pager"
echo "Check containers:         ${DOCKER_BIN} compose --env-file ${ENV_FILE} -f ${COMPOSE_FILE} ps"
