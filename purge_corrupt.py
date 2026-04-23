import os
import django
import json
from pathlib import Path

# Setup Django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings.base')
django.setup()

from deals.models import Deal
from ai_orchestrator.models import DocumentChunk

def purge_corrupt():
    base_dir = Path("data/extractions")
    if not base_dir.exists():
        print(f"Error: {base_dir} not found.")
        return

    print(f"\n>>> PURGING CORRUPT EXTRACTION FILES (< 500 bytes)")
    print("-" * 60)
    
    deleted_files = 0
    deleted_chunks = 0
    
    # 1. Scan and Delete tiny files on Disk
    for deal_dir in base_dir.iterdir():
        if not deal_dir.is_dir(): continue
        
        for f in deal_dir.glob("*.json"):
            if f.stat().st_size < 500:
                print(f"  [PURGE DISK] {f.name} ({f.stat().st_size} bytes)")
                
                # Also delete related DB Chunk if it exists
                # We match by filename stored in metadata
                filename_to_match = f.name.replace(".artifact.json", "").replace(".json", "")
                chunks = DocumentChunk.objects.filter(metadata__filename=filename_to_match)
                deleted_chunks += chunks.count()
                chunks.delete()

                os.remove(f)
                deleted_files += 1

    print("-" * 60)
    print(f"SUCCESS: Removed {deleted_files} corrupt files and cleaned {deleted_chunks} DB records.")
    print("You can now rerun your Phase 1 or Phase 2 scripts to fix these gaps.")

if __name__ == "__main__":
    purge_corrupt()
