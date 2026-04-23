import os
import django
import json
from pathlib import Path

# Setup Django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings.base')
django.setup()

from deals.models import Deal
from ai_orchestrator.models import DocumentChunk
from microsoft.services.graph_service import GraphAPIService

def check_integrity():
    if not os.path.exists('deal_discovery.json'):
        print("Error: deal_discovery.json not found.")
        return

    with open('deal_discovery.json', 'r') as f:
        discovery = json.load(f)

    deals_metadata = sorted(discovery['deals'], key=lambda x: x.get('file_count', 0))
    drive_id, user_email = discovery['drive_id'], discovery['user_email']
    
    print(f"\n>>> STRICT PIPELINE AUDIT")
    print("-" * 125)
    print(f"{'#':<3} | {'DEAL NAME':<35} | {'CLOUD':<5} | {'OCR':<5} | {'NORM':<5} | {'DB':<5} | {'STATUS'}")
    print("-" * 125)

    graph = GraphAPIService()
    base_dir = Path("data/extractions")

    for idx, deal_meta in enumerate(deals_metadata, 1):
        deal_name = deal_meta['name']
        deal_dir = base_dir / deal_name.replace(" ", "_").replace("/", "-")
        
        # 1. LIVE CLOUD COUNT
        try:
            live_files = graph.get_folder_tree(drive_id, deal_meta['id'], user_email)
            supported = [f for f in live_files if os.path.splitext(f['name'])[1].lower() in ['.pdf', '.png', '.jpg', '.jpeg', '.docx', '.doc', '.pptx', '.ppt', '.xlsx', '.xls', '.msg']]
            expected = len(supported)
        except:
            expected = deal_meta.get('file_count', 0)
            
        ocr_count = 0
        norm_count = 0
        corrupt_count = 0
        
        # 2. DISK AUDIT
        if deal_dir.exists():
            all_files = list(deal_dir.glob("*.json"))
            for f in all_files:
                if f.stat().st_size < 500:
                    corrupt_count += 1
                    continue
                if ".artifact.json" in f.name:
                    norm_count += 1
                elif f.name != ".complete" and ".part" not in f.name:
                    ocr_count += 1
        
        # 3. DATABASE AUDIT
        db_count = 0
        try:
            deal_obj = Deal.objects.get(title=deal_name)
            db_count = DocumentChunk.objects.filter(deal=deal_obj, source_type='extracted_source').count()
        except: pass

        # 4. STRICT STATUS LOGIC
        if expected == 0:
            status = "⚪ EMPTY"
        elif corrupt_count > 0:
            status = f"❌ {corrupt_count} CORRUPT"
        elif db_count >= norm_count and norm_count >= expected:
            status = "✅ 100% CLEAN"
        elif db_count < norm_count and norm_count >= expected:
            status = f"⏳ DB SYNC ({db_count}/{norm_count})"
        elif norm_count > 0:
            status = f"🚧 NORM ({norm_count}/{expected})"
        elif ocr_count >= expected:
            status = "📦 OCR DONE"
        else:
            status = f"⚠️ PARTIAL ({ocr_count}/{expected})"

        print(f"{idx:<3} | {deal_name[:35]:<35} | {expected:^5} | {ocr_count:^5} | {norm_count:^5} | {db_count:^5} | {status}")

    print("-" * 125)
    print("Legend: # = Selection Index | DB SYNC = Files are cleaned on Disk but not in Database.")

if __name__ == "__main__":
    check_integrity()
