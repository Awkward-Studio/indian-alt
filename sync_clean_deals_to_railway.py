from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path


def parse_args():
    parser = argparse.ArgumentParser(
        description=(
            "Sync the cleaned local deal database to Railway Postgres, including "
            "documents, chunks, embeddings, analyses, and retrieval profiles."
        )
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write to Railway. Default is dry-run.",
    )
    parser.add_argument(
        "--railway-project-dir",
        default=".",
        help="Directory where Railway CLI should run. Defaults to this repo.",
    )
    parser.add_argument(
        "--prod-database-url",
        default=None,
        help="Optional production DATABASE_URL. If omitted, the script uses Railway CLI.",
    )
    parser.add_argument(
        "--prod-db-ssl-require",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Whether the production DB connection should require SSL. Defaults to DB_SSL_REQUIRE/PROD_DB_SSL_REQUIRE or true.",
    )
    parser.add_argument(
        "--no-prune-production",
        action="store_true",
        help="Do not delete production deals absent from the cleaned local DB.",
    )
    parser.add_argument(
        "--deal",
        action="append",
        dest="deals",
        help="Optional local deal title or UUID to sync. Can be passed multiple times.",
    )
    return parser.parse_args()


def run_command(command: list[str]) -> int:
    print("+ " + " ".join(command), flush=True)
    completed = subprocess.run(command)
    return completed.returncode


def resolve_ssl_require(cli_value: bool | None) -> bool:
    if cli_value is not None:
        return cli_value

    raw_value = os.environ.get("PROD_DB_SSL_REQUIRE", os.environ.get("DB_SSL_REQUIRE", "true"))
    return raw_value.strip().lower() not in {"0", "false", "no", "off"}


def main() -> int:
    args = parse_args()
    repo_dir = Path(__file__).resolve().parent
    sync_script = repo_dir / "bulk_sync_local_db_to_prod.py"

    command = [sys.executable, str(sync_script)]
    if args.prod_database_url:
        command.extend(["--prod-database-url", args.prod_database_url])
    else:
        command.extend(["--railway-cli", "--railway-project-dir", args.railway_project_dir])

    if resolve_ssl_require(args.prod_db_ssl_require):
        command.append("--prod-db-ssl-require")
    else:
        command.append("--no-prod-db-ssl-require")

    if not args.apply:
        command.append("--dry-run")

    if not args.no_prune_production:
        command.append("--prune-production-deals")

    if args.deals:
        command.append("--deals")
        command.extend(args.deals)

    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f">>> CLEAN DEAL DB RAILWAY SYNC ({mode})", flush=True)
    print("This sync includes deal rows, analyses, phase logs, documents, chunks, embeddings, and retrieval profiles.", flush=True)
    if not args.no_prune_production:
        if args.deals:
            print("ERROR: production pruning is only allowed for full-dataset syncs. Use --no-prune-production with --deal.", flush=True)
            return 2
        print("Production deals not present in the selected local set will be pruned in apply mode.", flush=True)
    print("-" * 72, flush=True)

    return run_command(command)


if __name__ == "__main__":
    raise SystemExit(main())
