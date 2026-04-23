import os
import django
from pathlib import Path

# Setup Django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings.base')
django.setup()

from ai_orchestrator.models import DocumentChunk

def clean_unsupported():
    base_dir = Path("data/extractions")
    if not base_dir.exists():
        print(f"Error: {base_dir} not found.")
        return

    # The ONLY extensions we want to keep
    ALLOWED_EXTS = {'.pdf', '.png', '.jpg', '.jpeg', '.docx', '.doc', '.pptx', '.ppt', '.xlsx', '.xls', '.msg'}
    
    print(f"\n>>> CLEANING UNSUPPORTED EXTRACTION RESULTS")
    print("-" * 60)
    
    deleted_files = 0
    deleted_chunks = 0
    
    for deal_dir in base_dir.iterdir():
        if not deal_dir.is_dir(): continue
        
        for f in deal_dir.glob("*.json"):
            # Skip artifacts and status markers
            if ".artifact.json" in f.name or f.name == ".complete": continue
            
            # Extract the 'original' extension from the json filename
            # e.g. "Report.zip.json" -> ".zip"
            # e.g. "Presentation.pptx.json" -> ".pptx"
            filename_without_json = f.name.replace(".json", "")
            ext = os.path.splitext(filename_without_json)[1].lower()
            
            if ext not in ALLOWED_EXTS:
                print(f"  [DELETE] {f.name} (Ext: {ext})")
                
                # 1. Clean from Database
                chunks = DocumentChunk.objects.filter(metadata__filename=filename_without_json)
                deleted_chunks += chunks.count()
                chunks.delete()
                
                # 2. Delete from Disk
                os.remove(f)
                deleted_files += 1

    print("-" * 60)
    print(f"SUCCESS: Removed {deleted_files} unsupported files and {deleted_chunks} DB records.")

if __name__ == "__main__":
    clean_unsupported()
