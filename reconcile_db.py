import os
import django
import json
from pathlib import Path

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings.base')
django.setup()

from ai_orchestrator.models import DocumentChunk

base_dir = Path("data/extractions")
artifacts = list(base_dir.glob("**/*.artifact.json"))
artifacts = [p for p in artifacts if "DEAL_SYNTHESIS" not in p.name]

missing_count = 0
found_count = 0

for art_path in artifacts:
    filename = art_path.name.replace(".artifact.json", "")
    source_id = filename + ".json"
    
    if DocumentChunk.objects.filter(source_id=source_id).exists():
        found_count += 1
    else:
        missing_count += 1

print(f"Artifacts on Disk: {len(artifacts)}")
print(f"Chunks found in DB: {found_count}")
print(f"Chunks MISSING from DB: {missing_count}")
