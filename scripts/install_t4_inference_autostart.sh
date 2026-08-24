#!/usr/bin/env bash
set -euo pipefail

SERVICE_NAME="india-alt-inference-t4.service"
INVOKING_USER="${SUDO_USER:-$(id -un)}"
INVOKING_HOME="$(getent passwd "${INVOKING_USER}" | cut -d: -f6 || true)"

if [[ -z "${INVOKING_HOME}" ]]; then
  echo "Could not resolve the home directory for ${INVOKING_USER}." >&2
  exit 1
fi

PROJECT_DIR="${T4_INFERENCE_DIR:-${INVOKING_HOME}}"
COMPOSE_FILE="${PROJECT_DIR}/docker-compose.inference.t4.yml"
ENV_FILE="${T4_INFERENCE_ENV_FILE:-${PROJECT_DIR}/.env.inference.t4}"
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

for required_file in "${COMPOSE_FILE}" "${ENV_FILE}"; do
  if [[ ! -f "${required_file}" ]]; then
    echo "Required file not found: ${required_file}" >&2
    exit 1
  fi
done

if [[ "${PROJECT_DIR}${COMPOSE_FILE}${ENV_FILE}${DOCKER_BIN}" =~ [[:space:]] ]]; then
  echo "Paths containing whitespace are not supported by this installer." >&2
  exit 1
fi

install -m 0644 /dev/stdin "/etc/systemd/system/${SERVICE_NAME}" <<EOF
[Unit]
Description=India Alternatives T4 inference services
Wants=network-online.target
Requires=docker.service
After=network-online.target docker.service nvidia-persistenced.service

[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory=${PROJECT_DIR}
ExecStartPre=${DOCKER_BIN} info
ExecStart=${DOCKER_BIN} compose --env-file ${ENV_FILE} -f ${COMPOSE_FILE} up -d --remove-orphans
ExecReload=${DOCKER_BIN} compose --env-file ${ENV_FILE} -f ${COMPOSE_FILE} up -d --remove-orphans
ExecStop=${DOCKER_BIN} compose --env-file ${ENV_FILE} -f ${COMPOSE_FILE} stop
TimeoutStartSec=0
TimeoutStopSec=300
Restart=on-failure
RestartSec=15

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now docker.service
systemctl enable --now "${SERVICE_NAME}"

echo
echo "Installed and started ${SERVICE_NAME}."
echo "Invoking user:           ${INVOKING_USER}"
echo "Project directory:       ${PROJECT_DIR}"
echo "Check boot configuration: systemctl is-enabled ${SERVICE_NAME}"
echo "Check service state:      systemctl status ${SERVICE_NAME} --no-pager"
echo "Check containers:         ${DOCKER_BIN} compose --env-file ${ENV_FILE} -f ${COMPOSE_FILE} ps"
