import os
import django
import json
from pathlib import Path

# Setup Django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings.base')
django.setup()

from django.db import transaction
from deals.models import Deal
from ai_orchestrator.models import DocumentChunk

def sync_to_prod():
    base_dir = Path("data/extractions")
    if not base_dir.exists():
        print(f"Error: {base_dir} not found.")
        return

    print(f"\n>>> SYNCING ARTIFACTS TO PRODUCTION (Batch Mode)")
    print("-" * 60)

    for deal_dir in base_dir.iterdir():
        if not deal_dir.is_dir(): continue
        
        deal_name = deal_dir.name.replace("_", " ").replace("-", "/")
        print(f"Deal: {deal_name}")
        
        deal_obj, _ = Deal.objects.get_or_create(title=deal_name)
        
        artifacts = list(deal_dir.glob("*.artifact.json"))
        chunks_to_create = []
        
        # 1. Identify missing artifacts for this deal
        for art_file in artifacts:
            try:
                # Existence check
                if DocumentChunk.objects.filter(deal=deal_obj, source_id=art_file.name).exists():
                    continue

                with open(art_file, 'r') as f:
                    data = json.load(f)
                
                filename = art_file.name.replace(".artifact.json", "")
                
                # 2. Build the object (no network call yet)
                chunks_to_create.append(DocumentChunk(
                    deal=deal_obj,
                    source_type='extracted_source',
                    source_id=art_file.name,
                    content=data.get("normalized_text", ""),
                    metadata={
                        'filename': filename,
                        'is_artifact': True,
                        'metrics': data.get('metrics', []),
                        'summary': data.get('document_summary', '')
                    }
                ))
            except Exception as e:
                print(f"  [WARN] Skipping {art_file.name}: {e}")

        # 3. Executing Batch Insert (One network call per deal)
        if chunks_to_create:
            try:
                with transaction.atomic():
                    DocumentChunk.objects.bulk_create(chunks_to_create)
                print(f"  [SUCCESS] Batch-inserted {len(chunks_to_create)} documents.")
            except Exception as e:
                print(f"  [ERROR] Batch failed for {deal_name}: {e}")
        else:
            print("  [SKIP] All documents already exist.")

    print("-" * 60)
    print("Sync Complete. Your Railway database is now live.")

if __name__ == "__main__":
    sync_to_prod()
