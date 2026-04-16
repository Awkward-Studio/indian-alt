import os
import django
import json
import time
import sys
import threading
from pathlib import Path
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed

# Setup Django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings.base')
django.setup()

from deals.models import Deal
from ai_orchestrator.models import DocumentChunk, AIAuditLog, AIPersonality, AISkill
from microsoft.services.graph_service import GraphAPIService
from ai_orchestrator.services.document_processor import DocumentProcessorService
from ai_orchestrator.services.ai_processor import AIProcessorService
from ai_orchestrator.services.runtime import AIRuntimeService
from deals.services.document_artifacts import DocumentArtifactService

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
            sys.stdout.write(f"\r    [PROGRESS] {self.filename}: {elapsed:.1f}s...")
            sys.stdout.flush()
            time.sleep(1)

    def stop(self, status="DONE"):
        self.active = False
        elapsed = time.time() - self.start_time
        sys.stdout.write(f"\r    [{status}] {self.filename}: {elapsed:.1f}s\n")
        sys.stdout.flush()

def process_file(file_info, drive_id, user_email, deal_obj, audit_log, deal_dir, doc_proc, ai_service, graph):
    file_name = file_info['name']
    ext = os.path.splitext(file_name)[1].lower()
    if ext not in ['.pdf', '.png', '.jpg', '.jpeg', '.docx', '.pptx', '.xlsx']:
        return None

    output_path = deal_dir / f"{file_name}.json"
    artifact_path = deal_dir / f"{file_name}.artifact.json"

    if artifact_path.exists():
        return f"Verified {file_name}"

    timer = ExtractionTimer(file_name)
    try:
        content = graph.get_drive_item_content(user_email, file_info['id'], drive_id)
        raw_result = doc_proc.get_extraction_result(content, file_name)
        with open(output_path, 'w') as f:
            json.dump(raw_result, f, indent=2)

        artifact = DocumentArtifactService.build_document_artifact(
            file_name=file_name,
            extracted_text=raw_result.get('normalized_text') or raw_result.get('text') or "",
            document_type="Other",
            extraction_mode=raw_result.get('mode', 'remote'),
            ai_service=ai_service,
            source_metadata={"source_id": file_info['id'], "audit_log_id": str(audit_log.id)}
        )
        with open(artifact_path, 'w') as f:
            json.dump(artifact, f, indent=2)
        
        DocumentChunk.objects.create(
            deal=deal_obj,
            audit_log=audit_log,
            source_type='extracted_source',
            source_id=file_info['id'],
            content=artifact.get('normalized_text') or "",
            metadata={'filename': file_name, 'drive_id': drive_id, 'is_artifact': True}
        )
        timer.stop("SUCCESS")
        return f"Finished {file_name}"
    except Exception as e:
        timer.stop("FAILED")
        return f"ERROR {file_name}: {str(e)}"

def run_selected_extraction():
    if not os.path.exists('deal_discovery.json'):
        print("Error: deal_discovery.json not found.")
        return

    with open('deal_discovery.json', 'r') as f:
        discovery = json.load(f)

    deals_metadata = sorted(discovery['deals'], key=lambda x: x.get('file_count', 0))
    drive_id = discovery['drive_id']
    user_email = discovery['user_email']
    
    print(f"\n>>> AVAILABLE DEALS")
    for i, d in enumerate(deals_metadata, 1):
        print(f"  {i:2}. {d['name']:<40} | Files: {d.get('file_count', 0)}")

    choice = input("\nEnter indices (e.g. 1, 3, 5): ")
    try:
        indices = [int(x.strip()) - 1 for x in choice.split(',')]
        to_process = [deals_metadata[i] for i in indices]
    except: return

    graph = GraphAPIService()
    doc_proc = DocumentProcessorService()
    ai_service = AIProcessorService()
    doc_proc.docproc_timeout = 1800
    base_dir = Path("data/extractions")
    base_dir.mkdir(parents=True, exist_ok=True)
    
    # GLOBAL WORKER POOL
    max_workers = 30 
    all_files_to_process = []

    print(f"\nGathering file lists for all selected deals...")
    for deal_meta in to_process:
        deal_name = deal_meta['name']
        deal_dir = base_dir / deal_name.replace(" ", "_").replace("/", "-")
        deal_dir.mkdir(parents=True, exist_ok=True)
        
        deal_obj, _ = Deal.objects.get_or_create(title=deal_name)
        audit_log = AIRuntimeService.create_audit_log(source_type='onedrive_folder', source_id=deal_meta['id'], context_label=deal_name, status='PROCESSING')

        files = graph.get_folder_tree(drive_id, deal_meta['id'], user_email)
        for f in files[:40]: # Safety cap per deal
            f['deal_obj'] = deal_obj
            f['audit_log'] = audit_log
            f['deal_dir'] = deal_dir
            all_files_to_process.append(f)

    print(f"\n>>> STARTING GLOBAL PIPELINE: {len(all_files_to_process)} total files across {len(to_process)} deals")
    print(f">>> Using {max_workers} simultaneous local workers to flood the H100.")

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [
            executor.submit(process_file, f, drive_id, user_email, f['deal_obj'], f['audit_log'], f['deal_dir'], doc_proc, ai_service, graph)
            for f in all_files_to_process
        ]
        
        for future in as_completed(futures):
            res = future.result()
            if res: print(f"    {res}")

    print(f"\nBatch complete. All pipelines drained.")
    os._exit(0)

if __name__ == "__main__":
    run_selected_extraction()
