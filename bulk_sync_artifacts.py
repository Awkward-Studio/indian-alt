import os
import django
import json
import time
import gc
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

# Setup Django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings.base')
django.setup()

from deals.models import Deal
from ai_orchestrator.models import DocumentChunk
from ai_orchestrator.services.embedding_processor import EmbeddingService
from deals.services.bulk_sync_resolution import load_synthesis_artifact, resolve_existing_deal
from django.db import connections

# ==============================================================================
# SYNC SETTINGS (ZERO-AI DIRECT LOADER)
# ==============================================================================
SYNC_WORKERS = 32               # High concurrency (just DB writes, no AI)
# ==============================================================================

def sync_worker(task):
    """Zero-AI Sync: Directly loads Phase 2 intelligence into the DB."""
    try:
        deal_obj = task['deal_obj']
        art_path = task['art_path']
        filename = task['filename']

        # 1. Load the pre-computed intelligence from Phase 2
        with open(art_path, 'r') as f:
            data = json.load(f)
        
        clean_text = data.get("normalized_text", "")
        if not clean_text:
            return f"  [SKIP] {filename} (No text in artifact)"

        # 2. EXTRACT PRE-COMPUTED INTEL (No AI calls needed!)
        doc_summary = data.get("document_summary", "No summary extracted.")
        metrics = data.get("metrics", {})
        risks = data.get("risks", [])
        doc_type = data.get("document_type", "Other")

        # 3. DATABASE PUSH
        # Chunk and embed the artifact's normalized text so chat retrieval can
        # use only the relevant slices instead of one full-document blob.
        source_id = filename + ".json"
        embed_service = EmbeddingService()
        chunk_count = embed_service.chunk_and_embed(
            text=clean_text,
            deal=deal_obj,
            source_type='extracted_source',
            source_id=source_id,
            metadata={
                'filename': filename,
                'is_artifact': True,
                'metrics': metrics,
                'summary': doc_summary,
                'doc_type': doc_type,
                'risks': risks,
                'chunk_kind': 'normalized_text',
                'synced_at': time.time(),
            },
            replace_existing=True,
        )
        if not chunk_count:
            return f"  [SKIP] {filename} (No retrievable chunks created)"

        return f"  [OK] {filename} ({chunk_count} chunks)"
    except Exception as e:
        return f"  [ERROR] {task['filename']}: {e}"
    finally:
        # Close connection to prevent exhaustion in the pool
        for conn in connections.all(): conn.close()
        gc.collect()

def run():
    base_dir = Path("data/extractions")
    if not base_dir.exists():
        print(f"Error: {base_dir} not found.")
        return

    print(f"\n>>> ZERO-AI ARTIFACT SYNC: LOADING PHASE 2 INTEL")
    print("-" * 60)

    all_sync_tasks = []
    touched_deals = {}

    # 1. Gather all deals
    for deal_dir in sorted(base_dir.iterdir()):
        if not deal_dir.is_dir(): continue

        print(f"Scanning Folder: {deal_dir.name}...")

        synth_artifact = load_synthesis_artifact(deal_dir)
        resolution = resolve_existing_deal(deal_dir.name, synth_artifact)
        canonical_title = resolution.canonical_title or deal_dir.name

        if resolution.duplicates:
            print(
                f"  [WARN] {canonical_title}: found {len(resolution.duplicates) + 1} matching deal rows; "
                f"attaching chunks to canonical deal {resolution.deal.id if resolution.deal else 'new'}"
            )

        deal_obj = resolution.deal
        if not deal_obj:
            if synth_artifact is None:
                print(f"  [SKIP] {deal_dir.name}: no synthesis artifact, refusing to create a deal from folder name alone")
                continue
            deal_obj = Deal.objects.create(title=canonical_title)
            print(f"  [NEW] Created canonical deal: {canonical_title}")

        touched_deals[str(deal_obj.id)] = deal_obj
        
        # 2. Find all finalized artifacts
        artifacts = list(deal_dir.glob("*.artifact.json"))
        for art_file in artifacts:
            # Skip the Master Deal Synthesis file if it exists
            if "DEAL_SYNTHESIS" in art_file.name: continue
            
            filename = art_file.name.replace(".artifact.json", "")
            
            # Skip only if retrievable embedded chunks already exist for this artifact.
            if DocumentChunk.objects.filter(
                deal=deal_obj,
                source_type='extracted_source',
                source_id=filename + ".json",
            ).exclude(embedding__isnull=True).exists():
                continue

            all_sync_tasks.append({
                'deal_obj': deal_obj,
                'art_path': art_file,
                'filename': filename
            })

    total = len(all_sync_tasks)
    if total == 0:
        print(">>> No new artifacts found. Database is up to date!")
        return

    print(f"\n>>> SYNCING {total} DOCUMENTS TO DATABASE...")
    
    # Pre-close Django connections for thread safety
    connections.close_all()

    with ThreadPoolExecutor(max_workers=SYNC_WORKERS) as executor:
        futures = {executor.submit(sync_worker, t): t for t in all_sync_tasks}
        done = 0
        for future in as_completed(futures):
            done += 1
            print(f"[{done}/{total}] {future.result()}")

    print("\n>>> REFRESHING DEAL RETRIEVAL PROFILES...")
    embed_service = EmbeddingService()
    refreshed = 0
    for deal_obj in touched_deals.values():
        try:
            if embed_service.refresh_deal_profile(deal_obj):
                refreshed += 1
        except Exception as e:
            print(f"  [WARN] {deal_obj.title}: profile refresh failed: {e}")

    print("-" * 60)
    print(f"Sync Complete. Your Deal Database is now fully populated. Refreshed profiles: {refreshed}")

if __name__ == "__main__":
    run()
