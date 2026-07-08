#!/usr/bin/env bash
set -euo pipefail

if [[ -z "${PROD_DATABASE_URL:-}" ]]; then
  echo "ERROR: PROD_DATABASE_URL is not set." >&2
  echo "Usage:" >&2
  echo "  export PROD_DATABASE_URL='postgres://user:password@host:port/dbname'" >&2
  echo "  $0" >&2
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
BACKUP_DIR="${BACKUP_DIR:-${REPO_DIR}/data/backups}"
POSTGRES_IMAGE="${POSTGRES_IMAGE:-postgres:18}"
PGSSLMODE_VALUE="${PGSSLMODE:-disable}"
PROGRESS_INTERVAL="${PROGRESS_INTERVAL:-2}"
TIMESTAMP="$(date +%Y%m%d_%H%M%S)"
BACKUP_FILE="${BACKUP_DIR}/hosted_${TIMESTAMP}.dump"
LOG_FILE="${BACKUP_DIR}/hosted_${TIMESTAMP}.log"

mkdir -p "${BACKUP_DIR}"

log() {
  local message="$1"
  printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "${message}" | tee -a "${LOG_FILE}"
}

human_size() {
  local path="$1"
  if [[ ! -f "${path}" ]]; then
    echo "0B"
    return
  fi
  du -h "${path}" | awk '{print $1}'
}

format_elapsed() {
  local total="$1"
  local hours minutes seconds
  hours=$((total / 3600))
  minutes=$(((total % 3600) / 60))
  seconds=$((total % 60))
  printf '%02d:%02d:%02d' "${hours}" "${minutes}" "${seconds}"
}

progress_bar() {
  local tick="$1"
  local width=24
  local block=8
  local start=$((tick % (width - block + 1)))
  local bar=""
  local i

  for ((i = 0; i < width; i++)); do
    if ((i >= start && i < start + block)); then
      bar+="#"
    else
      bar+="."
    fi
  done

  printf '%s' "${bar}"
}

monitor_dump() {
  local pid="$1"
  local started_at="$2"
  local index=0
  while kill -0 "${pid}" 2>/dev/null; do
    local now elapsed size bar
    now="$(date +%s)"
    elapsed="$(format_elapsed "$((now - started_at))")"
    size="$(human_size "${BACKUP_FILE}")"
    bar="$(progress_bar "${index}")"
    printf '\r[%s] pg_dump running [%s] elapsed=%s dump_size=%s log=%s' "$(date '+%H:%M:%S')" "${bar}" "${elapsed}" "${size}" "$(basename "${LOG_FILE}")"
    index=$((index + 1))
    sleep "${PROGRESS_INTERVAL}"
  done
  printf '\r%*s\r' 140 ''
}

parse_url() {
  "${REPO_DIR}/venv/bin/python" - "$1" <<'PY'
import os
import sys
import urllib.parse

field = sys.argv[1]
url = urllib.parse.urlparse(os.environ["PROD_DATABASE_URL"])

if field == "host":
    print(url.hostname or "")
elif field == "port":
    print(url.port or 5432)
elif field == "user":
    print(urllib.parse.unquote(url.username or "postgres"))
elif field == "password":
    print(urllib.parse.unquote(url.password or ""))
elif field == "database":
    print((url.path or "/").lstrip("/"))
else:
    raise SystemExit(f"unknown field: {field}")
PY
}

DB_HOST="$(parse_url host)"
DB_PORT="$(parse_url port)"
DB_USER="$(parse_url user)"
DB_PASSWORD="$(parse_url password)"
DB_NAME="$(parse_url database)"

if [[ -z "${DB_HOST}" || -z "${DB_USER}" || -z "${DB_NAME}" ]]; then
  echo "ERROR: PROD_DATABASE_URL is missing host, user, or database name." >&2
  exit 1
fi

log "Creating hosted DB backup."
log "Host: ${DB_HOST}"
log "Port: ${DB_PORT}"
log "Database: ${DB_NAME}"
log "Docker image: ${POSTGRES_IMAGE}"
log "SSL mode: ${PGSSLMODE_VALUE}"
log "Output: ${BACKUP_FILE}"
log "Log: ${LOG_FILE}"

STARTED_AT="$(date +%s)"
DUMP_STDERR="${BACKUP_DIR}/hosted_${TIMESTAMP}.pg_dump.stderr"

docker run --rm \
  -e "PGPASSWORD=${DB_PASSWORD}" \
  -e "PGSSLMODE=${PGSSLMODE_VALUE}" \
  -v "${BACKUP_DIR}:/backup" \
  "${POSTGRES_IMAGE}" \
  pg_dump \
    -h "${DB_HOST}" \
    -p "${DB_PORT}" \
    -U "${DB_USER}" \
    -d "${DB_NAME}" \
    -Fc \
    --no-owner \
    --no-acl \
    -f "/backup/$(basename "${BACKUP_FILE}")" \
    2> "${DUMP_STDERR}" &

DUMP_PID="$!"
monitor_dump "${DUMP_PID}" "${STARTED_AT}"

if ! wait "${DUMP_PID}"; then
  log "ERROR: pg_dump failed."
  if [[ -s "${DUMP_STDERR}" ]]; then
    sed "s/${DB_PASSWORD}/***/g" "${DUMP_STDERR}" | tee -a "${LOG_FILE}" >&2
  fi
  exit 1
fi

if [[ -s "${DUMP_STDERR}" ]]; then
  log "pg_dump messages:"
  sed "s/${DB_PASSWORD}/***/g" "${DUMP_STDERR}" | tee -a "${LOG_FILE}"
fi
rm -f "${DUMP_STDERR}"

log "pg_dump completed. Verifying backup archive with pg_restore -l..."
TOC_LINES="$(
  docker run --rm \
    -v "${BACKUP_DIR}:/backup" \
    "${POSTGRES_IMAGE}" \
    pg_restore -l "/backup/$(basename "${BACKUP_FILE}")" | wc -l
)"

BYTES="$(stat -c%s "${BACKUP_FILE}")"
MB="$("${REPO_DIR}/venv/bin/python" - <<PY
print(round(${BYTES} / 1024 / 1024, 2))
PY
)"

log "Backup complete."
log "File: ${BACKUP_FILE}"
log "Bytes: ${BYTES}"
log "MB: ${MB}"
log "Archive TOC lines: ${TOC_LINES}"
