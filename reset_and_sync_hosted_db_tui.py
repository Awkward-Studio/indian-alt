#!/usr/bin/env python3
import os
import sys
import time
import json
import logging
import argparse
import subprocess
from copy import deepcopy

import dj_database_url
import django

# Rich TUI components
from rich.console import Console
from rich.panel import Panel
from rich.progress import (
    Progress, SpinnerColumn, TextColumn, BarColumn, 
    TaskProgressColumn, TimeElapsedColumn, TimeRemainingColumn, MofNCompleteColumn
)
from rich.live import Live

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.base")
django.setup()

from django.conf import settings
from django.db import connections, transaction

from deals.models import Deal, DealAnalysis, DealDocument
from banks.models import Bank
from contacts.models import Contact
from ai_orchestrator.models import DocumentChunk, AIAuditLog, DealRetrievalProfile
from work_items.models import Task, TaskSuggestion
from accounts.models import Profile

SOURCE_DB = "default"
TARGET_DB = "production"

PROD_DATABASE_URL = os.environ.get(
    "PROD_DATABASE_URL",
    "postgres://postgres:.9uAmOaNMwx2MuLqhC9ln5Xrw-fCwVM5@crossover.proxy.rlwy.net:42815/railway"
)

# Logging configuration
LOG_FILE = "hosted_db_reset_and_sync.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, mode="a", encoding="utf-8"),
    ]
)
logger = logging.getLogger("db_sync_tui")
console = Console()

def parse_args():
    parser = argparse.ArgumentParser(description="Reset/Sync Hosted Database with Local Analyzed Deals")
    parser.add_argument(
        "--skip-wipe", "--resume", 
        action="store_true", 
        help="Skip database wipe and perform incremental sync checking existing records."
    )
    return parser.parse_args()

def setup_target_database():
    source_url = settings.DATABASES[SOURCE_DB]
    parsed = dj_database_url.parse(PROD_DATABASE_URL, conn_max_age=600, ssl_require=False)
    target_config = deepcopy(source_url)
    target_config.update(parsed)
    settings.DATABASES[TARGET_DB] = target_config
    connections.databases[TARGET_DB] = target_config
    connections[TARGET_DB].close()

def log_msg(msg: str):
    logger.info(msg)

def wipe_hosted_database(progress, task_id, skip_wipe=False):
    if skip_wipe:
        log_msg("--- STAGE 1: Skipping DB Wipe (Resume / Incremental Mode) ---")
        progress.update(task_id, description="[bold yellow]Skipped DB Wipe (Incremental Resume Mode)", completed=100)
        setup_target_database()
        return

    log_msg("--- STAGE 1: Clearing Hosted Database ---")
    progress.update(task_id, description="[bold red]Connecting to hosted PostgreSQL...", completed=10)
    setup_target_database()
    
    with connections[TARGET_DB].cursor() as cursor:
        progress.update(task_id, description="[bold red]Dropping schema public cascade...", completed=30)
        cursor.execute("DROP SCHEMA public CASCADE;")
        log_msg("Dropped schema public CASCADE")
        
        progress.update(task_id, description="[bold yellow]Recreating clean schema public...", completed=60)
        cursor.execute("CREATE SCHEMA public;")
        cursor.execute("GRANT ALL ON SCHEMA public TO postgres;")
        cursor.execute("GRANT ALL ON SCHEMA public TO public;")
        log_msg("Re-created schema public")
        
        progress.update(task_id, description="[bold green]Creating vector extension...", completed=90)
        cursor.execute("CREATE EXTENSION IF NOT EXISTS vector;")
        log_msg("Created vector extension")

    progress.update(task_id, description="[bold green]Stage 1 Complete: Hosted DB Cleared!", completed=100)

def run_fresh_migrations_streaming(progress, task_id):
    log_msg("--- STAGE 2: Running Fresh Migrations ---")
    progress.update(task_id, description="[bold cyan]Starting manage.py migrate...", completed=5)
    
    env = os.environ.copy()
    env["DATABASE_URL"] = PROD_DATABASE_URL + "?sslmode=disable"
    
    process = subprocess.Popen(
        [sys.executable, "manage.py", "migrate"],
        cwd="/home/omi/Omi_Home_NAS/Code/Work/India-Alternatives/indian-alt",
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1
    )

    applied_count = 0
    total_estimated = 118

    for line in iter(process.stdout.readline, ''):
        line_str = line.strip()
        if not line_str:
            continue
        log_msg(line_str)
        if "Applying " in line_str:
            applied_count += 1
            mig_name = line_str.replace("Applying ", "").replace("...", "").strip()
            pct = min(95, int(10 + (applied_count / total_estimated) * 85))
            progress.update(
                task_id, 
                completed=pct, 
                description=f"[bold cyan]Migrating ({applied_count}/{total_estimated}): {mig_name[:35]}"
            )

    process.wait()
    if process.returncode != 0:
        log_msg(f"Migration error exit code: {process.returncode}")
        raise RuntimeError("Migration failed. Check hosted_db_reset_and_sync.log for details.")

    progress.update(task_id, description="[bold green]Stage 2 Complete: All Migrations Applied!", completed=100)

def sync_reference_data_tui(progress, user_task, bank_task, contact_task):
    log_msg("--- STAGE 3: Syncing Reference Data (Users, Banks & Contacts) ---")
    from bulk_sync_local_db_to_prod import upsert_bank, upsert_contact, upsert_user_and_profile

    local_profiles = list(Profile.objects.using(SOURCE_DB).select_related("user").all())
    progress.update(user_task, total=len(local_profiles), completed=0, description="[bold yellow]Syncing Users & Profiles...")
    with transaction.atomic(using=TARGET_DB):
        for idx, prof in enumerate(local_profiles, start=1):
            upsert_user_and_profile(prof, dry_run=False)
            progress.update(user_task, completed=idx, description=f"[bold yellow]User ({idx}/{len(local_profiles)}): {prof.email[:30]}")
    log_msg(f"Synced {len(local_profiles)} Users & Profiles")

    local_banks = list(Bank.objects.using(SOURCE_DB).all().order_by("name", "id"))
    local_contacts = list(Contact.objects.using(SOURCE_DB).select_related("bank").all().order_by("name", "id"))
    
    progress.update(bank_task, total=len(local_banks), completed=0, description="[bold yellow]Syncing Banks...")
    bank_map = {}
    
    with transaction.atomic(using=TARGET_DB):
        for idx, bank in enumerate(local_banks, start=1):
            synced = upsert_bank(bank, dry_run=False, verbose=False)
            if synced:
                bank_map[str(bank.id)] = synced
            progress.update(bank_task, completed=idx, description=f"[bold yellow]Bank ({idx}/{len(local_banks)}): {bank.name[:30]}")
    
    log_msg(f"Synced {len(bank_map)} Banks")

    progress.update(contact_task, total=len(local_contacts), completed=0, description="[bold yellow]Syncing Contacts...")
    contact_map = {}
    
    with transaction.atomic(using=TARGET_DB):
        for idx, contact in enumerate(local_contacts, start=1):
            synced = upsert_contact(contact, bank_map, dry_run=False, verbose=False)
            if synced:
                contact_map[str(contact.id)] = synced
            progress.update(contact_task, completed=idx, description=f"[bold yellow]Contact ({idx}/{len(local_contacts)}): {contact.name[:30]}")

    log_msg(f"Synced {len(contact_map)} Contacts")
    return bank_map, contact_map

def main():
    args = parse_args()

    mode_str = "INCREMENTAL RESUME MODE" if args.skip_wipe else "FULL RESET & BACKFILL MODE"
    console.print(Panel.fit(
        f"[bold green]Hosted Database Sync ({mode_str})[/bold green]\n"
        "[dim]Check existing records -> Upsert missing/updated deals -> Preserves login & reference data[/dim]",
        border_style="green"
    ))
    
    log_msg(f"Started TUI Sync Session - Mode: {mode_str}")

    stage_progress = Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        TimeElapsedColumn(),
        console=console
    )

    detail_progress = Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        TimeRemainingColumn(),
        console=console
    )

    with Live(Panel(stage_progress, title="[bold]System Setup[/bold]", border_style="cyan"), console=console):
        stage1 = stage_progress.add_task("[bold red]Stage 1: Clean Hosted DB", total=100)
        wipe_hosted_database(stage_progress, stage1, skip_wipe=args.skip_wipe)

        stage2 = stage_progress.add_task("[bold cyan]Stage 2: Stream Migrations", total=100)
        run_fresh_migrations_streaming(stage_progress, stage2)

    console.print("\n[bold green]✓ Database schema ready![/bold green]\n")

    # Fetch analyzed deal IDs
    analyzed_deal_ids = list(set(DealAnalysis.objects.using(SOURCE_DB).values_list('deal_id', flat=True)) | set(DealDocument.objects.using(SOURCE_DB).values_list('deal_id', flat=True)))
    analyzed_deal_ids = [did for did in analyzed_deal_ids if did]
    
    analyzed_deals = list(
        Deal.objects.using(SOURCE_DB).filter(id__in=analyzed_deal_ids).order_by("title").only(
            "id", "title", "deal_status", "current_phase", "funding_ask", "industry", "sector"
        )
    )

    log_msg(f"Found {len(analyzed_deals)} Analyzed Deals to Check/Backfill")

    from bulk_sync_local_db_to_prod import sync_single_deal

    with detail_progress:
        user_task = detail_progress.add_task("[bold yellow]Users & Profiles", total=100)
        bank_task = detail_progress.add_task("[bold yellow]Banks", total=100)
        contact_task = detail_progress.add_task("[bold yellow]Contacts", total=100)
        
        bank_map, contact_map = sync_reference_data_tui(detail_progress, user_task, bank_task, contact_task)
        
        deal_task = detail_progress.add_task(
            "[bold green]Syncing Analyzed Deals...", total=len(analyzed_deals)
        )

        skipped_unchanged = 0
        synced_count = 0

        for idx, deal in enumerate(analyzed_deals, start=1):
            detail_progress.update(
                deal_task, 
                completed=idx, 
                description=f"[bold green]Deal ({idx}/{len(analyzed_deals)}): {deal.title[:28]} [dim](Skipped: {skipped_unchanged})[/dim]"
            )
            
            try:
                result = sync_single_deal(
                    deal,
                    bank_map,
                    contact_map,
                    dry_run=False,
                    verbose=False,
                    progress_interval=0
                )
                if not result.get("rewritten", True):
                    skipped_unchanged += 1
                    log_msg(f"SKIP [{deal.title}]: Intact in production (docs={result['documents']}, chunks={result['chunks']})")
                else:
                    synced_count += 1
                    log_msg(
                        f"SYNC/REWRITE [{deal.title}]: docs={result['documents']} chunks={result['chunks']}"
                    )
            except Exception as e:
                log_msg(f"ERROR [{deal.title}]: {e}")

    console.print(Panel.fit(
        "[bold green]🎉 DATABASE SYNC COMPLETE![/bold green]\n"
        f"[dim]Total Checked: {len(analyzed_deals)} deals | Skipped Intact Deals: {skipped_unchanged} | Rewritten/Synced: {synced_count}\n"
        f"Detailed log saved to: {LOG_FILE}[/dim]",
        border_style="bold green"
    ))

if __name__ == "__main__":
    main()
