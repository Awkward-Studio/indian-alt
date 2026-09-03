#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
T4_INFERENCE_ENV_FILE="${T4_INFERENCE_ENV_FILE:-$SCRIPT_DIR/.env.inference.t4}"
START_SERVICES=true

usage() {
  printf '%s\n' \
    "Usage: ./bootstrap_inference_t4.sh [--no-start]" \
    "" \
    "Creates the T4 environment file when missing, validates Compose, then" \
    "pulls and starts text generation, embedding, and reranking." \
    "" \
    "  --no-start  Prepare and validate only." \
    "  -h, --help   Show this help."
}

while (($#)); do
  case "$1" in
    --no-start) START_SERVICES=false ;;
    -h|--help) usage; exit 0 ;;
    *) printf 'Unknown option: %s\n' "$1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

if ! command -v docker >/dev/null 2>&1; then
  printf 'Required command not found: docker\n' >&2
  exit 1
fi

if [[ ! -f "$T4_INFERENCE_ENV_FILE" ]]; then
  cp -- "$SCRIPT_DIR/.env.inference.t4.example" "$T4_INFERENCE_ENV_FILE"
  chmod 600 "$T4_INFERENCE_ENV_FILE"
  printf 'Created %s. Review its API key and cache path.\n' "$T4_INFERENCE_ENV_FILE"
fi

compose=(docker compose --env-file "$T4_INFERENCE_ENV_FILE" -f "$SCRIPT_DIR/docker-compose.inference.t4.yml")
"${compose[@]}" config --quiet
printf 'T4 application inference configuration is valid.\n'

if [[ "$START_SERVICES" == true ]]; then
  "${compose[@]}" pull vllm-text tei-embedding tei-reranker
  "${compose[@]}" up -d --remove-orphans vllm-text tei-embedding tei-reranker
  "${compose[@]}" ps
else
  printf 'T4 setup complete. Containers were not started.\n'
fi
