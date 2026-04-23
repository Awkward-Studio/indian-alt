import os
import django
import json
import time
import sys
import threading
import base64
import io
import gc
import socket
import queue
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

import fitz # PyMuPDF for local splitting
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ==============================================================================
# MASTER TUNING DASHBOARD (INDUSTRIAL OCR BLITZ v47)
# ==============================================================================
# 1. WORKER POOLS
OCR_MAX_WORKERS = 10            # Parallel extractions
OCR_PDF_CHUNK_SIZE = 50         # Pages per window for giant PDFs
OCR_PAGE_LIMIT = 500            # Hard limit per document

# 2. NETWORK SETTINGS
OCR_TIMEOUT = (15, 1800)        # 15s connect, 30 mins read (Critical for heavy OCR)
CONNECTION_POOL_SIZE = 300      # Prevents local congestion
TASK_QUEUE_MAXSIZE = 500        # Buffer for steady firehose
# ==============================================================================

# Setup Django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings.base')
django.setup()

from microsoft.services.graph_service import GraphAPIService
from ai_orchestrator.services.document_processor import DocumentProcessorService
from deals.models import Deal

class KeepAliveAdapter(HTTPAdapter):
    def init_poolmanager(self, *args, **kwargs):
        options = [(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)]
        if hasattr(socket, 'TCP_KEEPIDLE'): options.append((socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 60))
        kwargs['socket_options'] = options
        super().init_poolmanager(*args, **kwargs)

def get_robust_session(pool_size):
    s = requests.Session()
    retries = Retry(total=5, backoff_factor=1, status_forcelist=[500, 502, 503, 504])
    adapter = KeepAliveAdapter(max_retries=retries, pool_connections=pool_size, pool_maxsize=pool_size)
    s.mount('http://', adapter)
    s.mount('https://', adapter)
    return s

def process_chunk_with_heartbeat(chunk_content, chunk_name, doc_proc, session, timeout, page_limit, start_page=0):
    payload = {
        "filename": chunk_name,
        "content_base64": base64.b64encode(chunk_content).decode("utf-8"),
        "page_limit": page_limit,
        "start_page": start_page
    }
    headers = {"Authorization": f"Bearer {doc_proc.docproc_api_key}"}
    with session.post(f"{doc_proc.docproc_url}/extract/document", json=payload, headers=headers, timeout=timeout, stream=True) as response:
        response.raise_for_status()
        full_content = []
        for chunk in response.iter_content(chunk_size=1024): 
            if chunk: full_content.append(chunk)
        res = json.loads(b"".join(full_content).decode("utf-8").strip())
        return res

def ocr_worker_loop(task_queue, drive_id, user_email, doc_proc, graph, session):
    """Immortal OCR Worker with enhanced context logging."""
    while True:
        try:
            file_info = task_queue.get()
            if file_info is None: break
            
            file_name = file_info['name']
            deal_name = file_info['deal_name']
            deal_dir = Path(file_info['deal_dir'])
            final_output_path = deal_dir / f"{file_name}.json"
            
            ext = os.path.splitext(file_name)[1].lower()
            
            try:
                print(f"    [DOWNLOAD] Deal: {deal_name} | File: {file_name}", flush=True)
                content = graph.get_drive_item_content(user_email, file_info['id'], drive_id)
                
                # --- PDF SLIDING WINDOW LOGIC (CONFIRMED) ---
                if ext == ".pdf":
                    with fitz.open(stream=content, filetype="pdf") as doc:
                        total_pages = len(doc)
                        effective_total = min(total_pages, OCR_PAGE_LIMIT)
                        
                        if total_pages > OCR_PDF_CHUNK_SIZE:
                            print(f"    [CHUNK-MODE] Deal: {deal_name} | File: {file_name} ({total_pages} pages)", flush=True)
                            all_text_parts = []
                            base_name = os.path.splitext(file_name)[0]
                            
                            for start_idx in range(0, effective_total, OCR_PDF_CHUNK_SIZE):
                                end_idx = min(start_idx + OCR_PDF_CHUNK_SIZE, effective_total)
                                chunk_label = f"Pages {start_idx+1}-{end_idx}"
                                chunk_filename = f"{base_name} [{chunk_label}]{ext}"
                                chunk_cache_path = deal_dir / f"{chunk_filename}.json"

                                if chunk_cache_path.exists():
                                    with open(chunk_cache_path, 'r') as f: part_data = json.load(f)
                                else:
                                    print(f"      -> [SENDING CHUNK] Deal: {deal_name} | Window: {chunk_label}", flush=True)
                                    chunk_doc = fitz.open()
                                    chunk_doc.insert_pdf(doc, from_page=start_idx, to_page=end_idx-1)
                                    chunk_bytes = chunk_doc.tobytes()
                                    chunk_doc.close()
                                    part_data = process_chunk_with_heartbeat(chunk_bytes, chunk_filename, doc_proc, session, OCR_TIMEOUT, OCR_PDF_CHUNK_SIZE)
                                    with open(chunk_cache_path, 'w') as f: json.dump(part_data, f)

                                all_text_parts.append(part_data.get("normalized_text", ""))
                                del part_data
                                gc.collect() 
                            
                            full_text = "\n\n".join(all_text_parts)
                            final_result = {
                                "normalized_text": full_text,
                                "raw_extracted_text": full_text,
                                "transcription_status": "complete",
                                "quality_flags": ["local_pdf_chunking_active", "exhaustive_v47"]
                            }
                            with open(final_output_path, 'w') as f: json.dump(final_result, f, indent=2)
                            print(f"    [COMPLETE] Deal: {deal_name} | File: {file_name} (Merged {len(all_text_parts)} windows)", flush=True)
                            continue

                # Single pass for small files
                result = process_chunk_with_heartbeat(content, file_name, doc_proc, session, OCR_TIMEOUT, OCR_PAGE_LIMIT)
                with open(final_output_path, 'w') as f: json.dump(result, f, indent=2)
                print(f"    [COMPLETE] Deal: {deal_name} | File: {file_name}", flush=True)

            except Exception as e:
                print(f"    [ERROR] Deal: {deal_name} | File: {file_name} | Error: {e}", flush=True)
            finally:
                if 'content' in locals(): del content
                gc.collect()
                task_queue.task_done()

        except Exception as e:
            print(f"Worker Recovery: {e}", flush=True)
            time.sleep(2)

def run():
    if not os.path.exists('deal_discovery.json'): return
    with open('deal_discovery.json', 'r') as f: discovery = json.load(f)
    deals_metadata = sorted(discovery['deals'], key=lambda x: x.get('file_count', 0))
    drive_id, user_email = discovery['drive_id'], discovery['user_email']

    print(f"\n>>> SCRIPT 1: INDUSTRIAL OCR Swarm (v47)")
    for i, d in enumerate(deals_metadata, 1):
        print(f"  {i:2}. {d['name']:<40} | Files: {d.get('file_count', 0)}")
    
    choice = input("\nEnter indices or 'all': ")
    to_process = []
    try:
        if choice.lower() == 'all': to_process = deals_metadata
        else:
            indices = []
            for part in choice.split(','):
                part = part.strip()
                if '-' in part:
                    s, e = map(int, part.split('-'))
                    indices.extend(range(s-1, e))
                else: indices.append(int(part)-1)
            to_process = [deals_metadata[i] for i in sorted(list(set(indices))) if 0 <= i < len(deals_metadata)]
    except: return

    graph, doc_proc = GraphAPIService(), DocumentProcessorService()
    session = get_robust_session(CONNECTION_POOL_SIZE)
    base_dir = Path("data/extractions"); task_queue = queue.Queue(maxsize=TASK_QUEUE_MAXSIZE)
    
    ALLOWED_EXTS = {'.pdf', '.png', '.jpg', '.jpeg', '.docx', '.doc', '.pptx', '.ppt', '.xlsx', '.xls', '.msg'}
    
    print("\n>>> PRE-FILTERING & QUEUEING...")
    files_to_queue = 0
    visited_files = set()

    for d_meta in to_process:
        d_dir = base_dir / d_meta['name'].replace(" ", "_").replace("/", "-")
        d_dir.mkdir(parents=True, exist_ok=True)
        Deal.objects.get_or_create(title=d_meta['name'])
        
        files = graph.get_folder_tree(drive_id, d_meta['id'], user_email)
        for f in files:
            ext = os.path.splitext(f['name'])[1].lower()
            if ext in ALLOWED_EXTS:
                if f['id'] in visited_files: continue
                visited_files.add(f['id'])
                
                # CONSOLIDATED SKIP CHECK
                if (d_dir / f"{f['name']}.json").exists():
                    print(f"    [SKIPPED] Deal: {d_meta['name']} | File: {f['name']} (Already Done)", flush=True)
                    continue

                f.update({'deal_dir': d_dir, 'deal_name': d_meta['name']})
                task_queue.put(f)
                files_to_queue += 1

    print(f"\n>>> PIPELINE START: {files_to_queue} ACTIVE FILES | Swarm={OCR_MAX_WORKERS}")

    worker_threads = []
    for _ in range(OCR_MAX_WORKERS):
        t = threading.Thread(target=ocr_worker_loop, args=(task_queue, drive_id, user_email, doc_proc, graph, session))
        t.daemon = True
        t.start()
        worker_threads.append(t)

    for _ in range(OCR_MAX_WORKERS): task_queue.put(None)
    for t in worker_threads: t.join()

    print(f"\nPhase 1 Complete.")
    os._exit(0)

if __name__ == "__main__": run()
