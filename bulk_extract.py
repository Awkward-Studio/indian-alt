import os
import django
import json
import time
import sys
import threading
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

# Setup Django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings.base')
django.setup()

from deals.models import Deal
from ai_orchestrator.models import DocumentChunk, AIAuditLog, AIPersonality, AISkill
from microsoft.services.graph_service import GraphAPIService
from ai_orchestrator.services.document_processor import DocumentProcessorService
from ai_orchestrator.services.runtime import AIRuntimeService

class ExtractionTimer:
    def __init__(self, filename):
        self.filename = filename
        self.start_time = time.time()
        self.active = True
        self.thread = threading.Thread(target=self._run)
        self.thread.daemon = True
        self.thread.start()

    def _run(self):
        while self.active:
            elapsed = time.time() - self.start_time
            sys.stdout.write(f"\r    [WAITING] {self.filename}: {elapsed:.1f}s...")
            sys.stdout.flush()
            time.sleep(1)

    def stop(self, status="DONE"):
        self.active = False
        elapsed = time.time() - self.start_time
        sys.stdout.write(f"\r    [{status}] {self.filename}: {elapsed:.1f}s\n")
        sys.stdout.flush()

def process_file(file_info, drive_id, user_email, deal_obj, audit_log, deal_dir, doc_proc, graph):
    file_name = file_info['name']
    ext = os.path.splitext(file_name)[1].lower()
    
    if ext not in ['.pdf', '.png', '.jpg', '.jpeg', '.docx', '.pptx', '.xlsx']:
        return None

    output_path = deal_dir / f"{file_name}.json"
    if output_path.exists():
        if not DocumentChunk.objects.filter(deal=deal_obj, source_id=file_info['id']).exists():
            try:
                with open(output_path, 'r') as f:
                    data = json.load(f)
                    DocumentChunk.objects.create(
                        deal=deal_obj,
                        audit_log=audit_log,
                        source_type='extracted_source',
                        source_id=file_info['id'],
                        content=data.get('normalized_text') or "",
                        metadata={'filename': file_name, 'drive_id': drive_id}
                    )
            except: pass
        return f"Verified {file_name}"

    timer = ExtractionTimer(file_name)
    try:
        content = graph.get_drive_item_content(user_email, file_info['id'], drive_id)
        result = doc_proc.get_extraction_result(content, file_name)
        
        with open(output_path, 'w') as f:
            json.dump(result, f, indent=2)
        
        DocumentChunk.objects.create(
            deal=deal_obj,
            audit_log=audit_log,
            source_type='extracted_source',
            source_id=file_info['id'],
            content=result.get('normalized_text') or "",
            metadata={'filename': file_name, 'drive_id': drive_id, 'extraction_mode': result.get('mode', 'remote')}
        )
        timer.stop("SUCCESS")
        return f"Finished {file_name}"
    except Exception as e:
        timer.stop("FAILED")
        return f"ERROR {file_name}: {str(e)}"

def extract_batch(limit=5, max_workers=2):
    if not os.path.exists('deal_discovery.json'):
        print("Error: deal_discovery.json not found.")
        return

    with open('deal_discovery.json', 'r') as f:
        discovery = json.load(f)

    deals_metadata = discovery['deals']
    drive_id = discovery['drive_id']
    user_email = discovery['user_email']
    
    graph = GraphAPIService()
    doc_proc = DocumentProcessorService()
    
    from django.conf import settings
    setattr(settings, "DOC_PROCESSOR_TIMEOUT", 1800)
    doc_proc.docproc_timeout = 1800

    base_dir = Path("data/extractions")
    base_dir.mkdir(parents=True, exist_ok=True)

    personality = AIRuntimeService.get_default_personality()
    skill = AISkill.objects.filter(name='deal_extraction').first()
    
    processed_deals = 0
    print(f"\n>>> PHASE A: OPTIMIZED BATCH EXTRACTION (Workers: {max_workers} | Timeout: 1800s)")

    for deal_meta in deals_metadata:
        if processed_deals >= limit:
            break
            
        deal_name = deal_meta['name']
        deal_id_ms = deal_meta['id']
        deal_dir = base_dir / deal_name.replace(" ", "_").replace("/", "-")
        
        if (deal_dir / ".complete").exists():
            continue

        deal_dir.mkdir(parents=True, exist_ok=True)
        print(f"\n[DEAL] {deal_name}")
        
        deal_obj, _ = Deal.objects.get_or_create(title=deal_name)
        audit_log = AIRuntimeService.create_audit_log(
            source_type='onedrive_folder',
            source_id=deal_id_ms,
            context_label=deal_name,
            personality=personality,
            skill=skill,
            status='PROCESSING'
        )

        try:
            files = graph.get_folder_tree(drive_id, deal_id_ms, user_email)
            if not files:
                print(f"  - No files found.")
                continue

            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = [
                    executor.submit(process_file, f, drive_id, user_email, deal_obj, audit_log, deal_dir, doc_proc, graph)
                    for f in files
                ]
                for future in as_completed(futures):
                    res = future.result()
                    if res: print(f"    {res}")

            with open(deal_dir / ".complete", "w") as f:
                f.write("done")
            
            audit_log.status = 'COMPLETED'
            audit_log.save()
            processed_deals += 1

        except Exception as e:
            print(f"  CRITICAL ERROR: {e}")
            audit_log.status = 'FAILED'
            audit_log.error_message = str(e)
            audit_log.save()
            break

    print(f"\nBatch complete.")

if __name__ == "__main__":
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 5
    extract_batch(limit, max_workers=2)
