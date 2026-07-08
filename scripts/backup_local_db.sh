#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
BACKUP_DIR="${BACKUP_DIR:-${REPO_DIR}/data/backups}"
DB_CONTAINER="${DB_CONTAINER:-indian-alt-db-1}"
DB_NAME="${DB_NAME:-indian_alt}"
DB_USER="${DB_USER:-postgres}"
DB_PASSWORD="${DB_PASSWORD:-postgres}"
POSTGRES_IMAGE="${POSTGRES_IMAGE:-postgres:16}"
PROGRESS_INTERVAL="${PROGRESS_INTERVAL:-2}"
FULL_VERIFY="${FULL_VERIFY:-0}"
TIMESTAMP="$(date +%Y%m%d_%H%M%S)"
BACKUP_FILE="${BACKUP_DIR}/local_${TIMESTAMP}.dump"
LOG_FILE="${BACKUP_DIR}/local_${TIMESTAMP}.log"
DUMP_STDERR="${BACKUP_DIR}/local_${TIMESTAMP}.pg_dump.stderr"

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
    printf '\r[%s] local pg_dump running [%s] elapsed=%s dump_size=%s log=%s' "$(date '+%H:%M:%S')" "${bar}" "${elapsed}" "${size}" "$(basename "${LOG_FILE}")"
    index=$((index + 1))
    sleep "${PROGRESS_INTERVAL}"
  done
  printf '\r%*s\r' 150 ''
}

log "Creating local DB backup."
log "Container: ${DB_CONTAINER}"
log "Database: ${DB_NAME}"
log "User: ${DB_USER}"
log "Verification image: ${POSTGRES_IMAGE}"
log "Output: ${BACKUP_FILE}"
log "Log: ${LOG_FILE}"

if ! docker inspect "${DB_CONTAINER}" >/dev/null 2>&1; then
  log "ERROR: Docker container '${DB_CONTAINER}' was not found."
  exit 1
fi

log "Checking database connectivity..."
docker exec \
  -e "PGPASSWORD=${DB_PASSWORD}" \
  "${DB_CONTAINER}" \
  pg_isready -U "${DB_USER}" -d "${DB_NAME}" >/dev/null

DB_SIZE="$(
  docker exec \
    -e "PGPASSWORD=${DB_PASSWORD}" \
    "${DB_CONTAINER}" \
    psql -U "${DB_USER}" -d "${DB_NAME}" -Atc "select pg_size_pretty(pg_database_size(current_database()))"
)"
log "Database size: ${DB_SIZE}"

STARTED_AT="$(date +%s)"

(
  docker exec \
    -e "PGPASSWORD=${DB_PASSWORD}" \
    "${DB_CONTAINER}" \
    pg_dump \
      -U "${DB_USER}" \
      -d "${DB_NAME}" \
      -Fc \
      --no-owner \
      --no-acl \
      > "${BACKUP_FILE}" \
      2> "${DUMP_STDERR}"
) &

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

if [[ "${FULL_VERIFY}" == "1" ]]; then
  log "Running full archive read/decompression check..."
  docker run --rm \
    -v "${BACKUP_DIR}:/backup" \
    "${POSTGRES_IMAGE}" \
    pg_restore -f /dev/null "/backup/$(basename "${BACKUP_FILE}")"
fi

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
