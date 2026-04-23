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
from ai_orchestrator.services.ai_processor import AIProcessorService
from ai_orchestrator.services.runtime import AIRuntimeService
from deals.services.document_artifacts import DocumentArtifactService

class ExtractionTimer:
    def __init__(self, filename, phase="OCR"):
        self.filename = filename
        self.phase = phase
        self.start_time = time.time()
        self.active = True
        self.thread = threading.Thread(target=self._run)
        self.thread.daemon = True
        self.thread.start()
        print(f"    [{self.phase} START] {self.filename}")

    def _run(self):
        last_announced = 0
        while self.active:
            elapsed = time.time() - self.start_time
            if int(elapsed) // 30 > last_announced:
                last_announced = int(elapsed) // 30
                print(f"    [{self.phase} WORKING] {self.filename}: {elapsed:.0f}s elapsed...")
            time.sleep(1)

    def stop(self, status="DONE"):
        self.active = False
        elapsed = time.time() - self.start_time
        print(f"    [{self.phase} {status}] {self.filename}: {elapsed:.1f}s")

def ocr_only(file_info, drive_id, user_email, deal_dir, doc_proc, graph):
    file_name = file_info['name']
    output_path = deal_dir / f"{file_name}.json"
    
    if output_path.exists():
        return f"Skipped OCR (Cached): {file_name}"

    timer = ExtractionTimer(file_name, "OCR")
    try:
        content = graph.get_drive_item_content(user_email, file_info['id'], drive_id)
        raw_result = doc_proc.get_extraction_result(content, file_name)
        with open(output_path, 'w') as f:
            json.dump(raw_result, f, indent=2)
        timer.stop("SUCCESS")
        return f"OCR Complete: {file_name}"
    except Exception as e:
        timer.stop("FAILED")
        return f"OCR ERROR {file_name}: {str(e)}"

def normalize_only(file_info, deal_obj, audit_log, deal_dir, ai_service):
    file_name = file_info['name']
    output_path = deal_dir / f"{file_name}.json"
    artifact_path = deal_dir / f"{file_name}.artifact.json"

    if artifact_path.exists():
        # Ensure it's in DB
        if not DocumentChunk.objects.filter(deal=deal_obj, source_id=file_info['id']).exists():
            with open(artifact_path, 'r') as f:
                artifact = json.load(f)
                DocumentChunk.objects.create(
                    deal=deal_obj, audit_log=audit_log, source_type='extracted_source',
                    source_id=file_info['id'], content=artifact.get('normalized_text', ""),
                    metadata={'filename': file_name, 'is_artifact': True}
                )
        return f"Already Normalized: {file_name}"

    if not output_path.exists():
        return f"Normalization Pending (No OCR data): {file_name}"

    timer = ExtractionTimer(file_name, "NORM")
    try:
        with open(output_path, 'r') as f:
            raw_result = json.load(f)

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
            deal=deal_obj, audit_log=audit_log, source_type='extracted_source',
            source_id=file_info['id'], content=artifact.get('normalized_text', ""),
            metadata={'filename': file_name, 'is_artifact': True}
        )
        timer.stop("SUCCESS")
        return f"Normalized: {file_name}"
    except Exception as e:
        timer.stop("FAILED")
        return f"NORM ERROR {file_name}: {str(e)}"

def run_selected_extraction():
    if not os.path.exists('deal_discovery.json'): return
    with open('deal_discovery.json', 'r') as f: discovery = json.load(f)

    deals_metadata = sorted(discovery['deals'], key=lambda x: x.get('file_count', 0))
    drive_id, user_email = discovery['drive_id'], discovery['user_email']
    
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
    
    all_files = []
    for deal_meta in to_process:
        deal_name = deal_meta['name']
        deal_dir = base_dir / deal_name.replace(" ", "_").replace("/", "-")
        deal_dir.mkdir(parents=True, exist_ok=True)
        deal_obj, _ = Deal.objects.get_or_create(title=deal_name)
        audit_log = AIRuntimeService.create_audit_log(source_type='onedrive_folder', source_id=deal_meta['id'], context_label=deal_name, status='PROCESSING')

        files = graph.get_folder_tree(drive_id, deal_meta['id'], user_email)
        for f in files[:50]:
            f.update({'deal_obj': deal_obj, 'audit_log': audit_log, 'deal_dir': deal_dir})
            all_files.append(f)

    # PASS 1: OCR BLITZ
    print(f"\n>>> PASS 1: STARTING OCR BLITZ ({len(all_files)} files)")
    with ThreadPoolExecutor(max_workers=50) as executor:
        futures = [executor.submit(ocr_only, f, drive_id, user_email, f['deal_dir'], doc_proc, graph) for f in all_files]
        for future in as_completed(futures):
            res = future.result()
            if res: print(f"    {res}")

    # PASS 2: NORMALIZATION PASS
    print(f"\n>>> PASS 2: STARTING AI NORMALIZATION")
    with ThreadPoolExecutor(max_workers=20) as executor: # Fewer workers for heavy LLM reasoning
        futures = [executor.submit(normalize_only, f, f['deal_obj'], f['audit_log'], f['deal_dir'], ai_service) for f in all_files]
        for future in as_completed(futures):
            res = future.result()
            if res: print(f"    {res}")

    print(f"\nBatch complete.")
    os._exit(0)

if __name__ == "__main__":
    run_selected_extraction()
