import os
import django
import json
import base64
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

# Setup Django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings.base')
django.setup()

from microsoft.services.graph_service import GraphAPIService
from ai_orchestrator.services.document_processor import DocumentProcessorService
from bulk_1_extract import get_robust_session, process_chunk_with_heartbeat, Timer

# ==============================================================================
# RESUME SETTINGS
# ==============================================================================
RESUME_START_PAGE = 500         # Start after the first 500 pages
RESUME_END_PAGE = 2000          # Scan up to 2000 total
RESUME_TIMEOUT = 1800           # 30 mins
RESUME_WORKERS = 5              # Keep local RAM safe
# ==============================================================================

def resume_worker(file_info, drive_id, user_email, deal_dir, doc_proc, graph, session):
    file_name = file_info['name']
    local_json_path = deal_dir / f"{file_name}.json"
    
    if not local_json_path.exists():
        return f"    [SKIP] {file_name} (No existing JSON to append to)"

    try:
        print(f"    [RESUMING] {file_name}: Fetching pages {RESUME_START_PAGE+1}-{RESUME_END_PAGE}...")
        
        # 1. Download original file again
        content = graph.get_drive_item_content(user_email, file_info['id'], drive_id)
        
        # 2. Call VM with start_page offset
        t = Timer(file_name, phase="RESUME")
        new_data = process_chunk_with_heartbeat(
            content, file_name, doc_proc, session, 
            RESUME_TIMEOUT, RESUME_END_PAGE, start_page=RESUME_START_PAGE
        )
        
        # 3. Read existing data and MERGE
        with open(local_json_path, 'r') as f:
            existing_data = json.load(f)
        
        old_text = existing_data.get("normalized_text", "")
        new_text = new_data.get("normalized_text", "")
        
        if not new_text.strip():
            t.stop("EMPTY")
            return f"    [FINISHED] {file_name} (No more pages found after 500)"

        merged_text = f"{old_text}\n\n[RESUMED DATA START (PAGE {RESUME_START_PAGE+1})]\n{new_text}"
        
        existing_data["normalized_text"] = merged_text
        existing_data["raw_extracted_text"] = merged_text
        existing_data["quality_flags"].append("excel_resumed_beyond_500")
        
        # 4. Save merged result
        with open(local_json_path, 'w') as f:
            json.dump(existing_data, f, indent=2)
            
        t.stop("SUCCESS")
        return f"    [SUCCESS] {file_name}: Merged pages 1-{RESUME_END_PAGE}."

    except Exception as e:
        return f"    [ERROR] {file_name}: {str(e)}"

def run():
    if not os.path.exists('deal_discovery.json'): return
    with open('deal_discovery.json', 'r') as f: discovery = json.load(f)
    deals_metadata = sorted(discovery['deals'], key=lambda x: x.get('file_count', 0))
    drive_id, user_email = discovery['drive_id'], discovery['user_email']

    print(f"\n>>> SCRIPT: EXCEL DEEP SCAN (Pages {RESUME_START_PAGE+1}-{RESUME_END_PAGE})")
    for i, d in enumerate(deals_metadata, 1):
        print(f"  {i:2}. {d['name']:<40}")
    
    choice = input("\nEnter indices or 'all': ")
    to_process = []
    try:
        indices = []
        if choice.lower() == 'all': indices = range(len(deals_metadata))
        else:
            for part in choice.split(','):
                part = part.strip()
                if '-' in part:
                    s, e = map(int, part.split('-'))
                    indices.extend(range(s-1, e))
                else: indices.append(int(part)-1)
        to_process = [deals_metadata[i] for i in sorted(list(set(indices))) if 0 <= i < len(deals_metadata)]
    except: return

    graph, doc_proc = GraphAPIService(), DocumentProcessorService()
    session = get_robust_session()
    base_dir = Path("data/extractions")
    
    all_files = []
    for d_meta in to_process:
        d_dir = base_dir / d_meta['name'].replace(" ", "_").replace("/", "-")
        if not d_dir.exists(): continue
        
        files = graph.get_folder_tree(drive_id, d_meta['id'], user_email)
        for f in files:
            ext = os.path.splitext(f['name'])[1].lower()
            if ext in {'.xls', '.xlsx'}:
                f.update({'deal_dir': d_dir})
                all_files.append(f)

    print(f"\n>>> SCANNING {len(all_files)} EXCEL FILES | WORKERS: {RESUME_WORKERS}")

    with ThreadPoolExecutor(max_workers=RESUME_WORKERS) as executor:
        futures = {executor.submit(resume_worker, f, drive_id, user_email, f['deal_dir'], doc_proc, graph, session): f for f in all_files}
        for future in as_completed(futures): print(future.result())

    print(f"\nExcel Deep Scan Complete.")

if __name__ == "__main__": run()
