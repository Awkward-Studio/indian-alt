import os
import json
import time
import sys
import threading
import requests
import re
import queue
import gc
import socket
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ==============================================================================
# MASTER TUNING DASHBOARD (BANKED PROGRESS v46)
# ==============================================================================
# 1. WORKER POOLS
GPU_FIREHOSE_WORKERS = 50       
ASSEMBLY_POOL_WORKERS = 4       
PRODUCER_SLICER_WORKERS = 8     

# 2. CHUNK SETTINGS
SAFE_CHAR_LIMIT = 20000         
CHUNK_OVERLAP = 2000           

# 3. NETWORK SETTINGS
NORM_TIMEOUT = (15, 2100)       
INTEL_TIMEOUT = (15, 600)       
CONNECTION_POOL_SIZE = 400      
TASK_QUEUE_MAXSIZE = 1000       
# ==============================================================================

# --- PRE-COMPILED ROBOTIC SCRUBBER ---
THINK_TAGS = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
PREAMBLE_PATTERNS = re.compile(r"^(Here is|Here's|Sure|Okay|Attached|I have|Cleaned).*?\n", re.MULTILINE | re.IGNORECASE)
MD_WRAPPERS = re.compile(r"^```markdown\n|^```\w*\n|```\n?$", re.IGNORECASE | re.MULTILINE)

def fast_scrub(text):
    if not text: return ""
    if "</think>" in text: text = text.split("</think>")[-1]
    text = THINK_TAGS.sub("", text)
    text = PREAMBLE_PATTERNS.sub("", text)
    text = MD_WRAPPERS.sub("", text)
    return text.strip()

class KeepAliveAdapter(HTTPAdapter):
    def init_poolmanager(self, *args, **kwargs):
        options = [(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)]
        if hasattr(socket, 'TCP_KEEPIDLE'): options.append((socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 60))
        kwargs['socket_options'] = options
        super().init_poolmanager(*args, **kwargs)

def get_robust_session(pool_size):
    s = requests.Session()
    retries = Retry(total=10, backoff_factor=1, status_forcelist=[400, 500, 502, 503, 504])
    adapter = KeepAliveAdapter(max_retries=retries, pool_connections=pool_size, pool_maxsize=pool_size)
    s.mount('http://', adapter)
    s.mount('https://', adapter)
    return s

def get_vllm_config():
    url = os.getenv("VLLM_BASE_URL", "http://20.244.11.248:8000/v1").rstrip("/")
    if not url.endswith("/v1"): url = f"{url}/v1"
    return {
        "url": f"{url}/chat/completions",
        "key": os.getenv("VLLM_API_KEY", "local-dev-key"),
        "model": "Qwen/Qwen3.6-35B-A3B" 
    }

CLEANUP_SYSTEM_PROMPT = """[SYSTEM: TEXT-TRANSFORMATION-ENGINE]
ROLE: Robotic document terminal. Clean OCR to Markdown.
STRICT: START immediately with content. NO thinking. NO filler."""

INTEL_SYSTEM_PROMPT = """[SYSTEM: EXTRACTION-ENGINE]
JSON ONLY. Output schema: document_type, document_summary, metrics, risks, tables_summary."""

completion_lock = threading.Lock()
doc_part_counters = {} 
global_parts_done = 0
global_total_parts = 0
global_files_done = 0
global_total_files = 0

ASSEMBLY_POOL = ThreadPoolExecutor(max_workers=ASSEMBLY_POOL_WORKERS)
INTEL_SESSION = get_robust_session(pool_size=100)

def assemble_document(doc_meta, vllm_config, session):
    global global_files_done
    try:
        # Give Disk time to settle
        all_ready = False
        for i in range(20):
            if all(p.exists() for p in doc_meta['parts']):
                all_ready = True
                break
            time.sleep(2)
            
        if not all_ready:
            missing = [p.name for p in doc_meta['parts'] if not p.exists()]
            print(f"    [LIMBO ERR] {doc_meta['name']} missing {len(missing)} pieces on disk.", flush=True)
            return False

        print(f"    [ASSEMBLING] {doc_meta['name']} ({len(doc_meta['parts'])} parts)...", flush=True)
        chunks = []
        for p in doc_meta['parts']:
            with open(p, 'r') as f: chunks.append(f.read())
        full_clean_text = "\n\n".join(chunks)
        del chunks
        
        json_schema = {"type": "object", "properties": {"document_type": {"type": "string"}, "document_summary": {"type": "string"}, "metrics": {"type": "object"}, "risks": {"type": "array", "items": {"type": "string"}}, "tables_summary": {"type": "string"}}, "required": ["document_type", "document_summary", "metrics", "risks", "tables_summary"], "additionalProperties": False}
        intel_payload = {"model": vllm_config['model'], "messages": [{"role": "system", "content": INTEL_SYSTEM_PROMPT}, {"role": "user", "content": f"EXTRACT:\n\n{full_clean_text[:50000]}"}], "temperature": 0.0, "response_format": {"type": "json_schema", "json_schema": {"name": "doc_ext", "schema": json_schema, "strict": True}}, "chat_template_kwargs": {"enable_thinking": False}}
        
        headers = {"Authorization": f"Bearer {vllm_config['key']}"}
        with session.post(vllm_config['url'], json=intel_payload, headers=headers, timeout=INTEL_TIMEOUT) as resp:
            resp.raise_for_status()
            try: intel_data = json.loads(fast_scrub(resp.json()['choices'][0]['message']['content']))
            except: intel_data = {}

        artifact = {
            "document_name": doc_meta['name'], "document_type": intel_data.get("document_type", "Other"),
            "document_summary": intel_data.get("document_summary", "No summary extracted"),
            "metrics": intel_data.get("metrics", {}), "risks": intel_data.get("risks", []),
            "tables_summary": intel_data.get("tables_summary", "No tables detected"),
            "normalized_text": full_clean_text, "quality_flags": ["cleaned_markdown", "banked_resumption_v46"], "source_file": doc_meta['name']
        }
        with open(doc_meta['final_path'], 'w') as f: json.dump(artifact, f, indent=2)
        for p in doc_meta['parts']: 
            try: os.remove(p)
            except: pass
        
        with completion_lock:
            global_files_done += 1
            percent = (global_files_done / global_total_files * 100) if global_total_files > 0 else 0
            print(f"    [COMPLETE] {doc_meta['name']} | OVERALL: {global_files_done}/{global_total_files} Files Finished ({percent:.1f}%)", flush=True)
        return True
    except Exception as e:
        print(f"    [ERR-ASSEMBLY] {doc_meta['name']}: {e}", flush=True)
    return False

def part_worker_firehose(task_queue, vllm_config, session, doc_lookup):
    global global_parts_done
    while True:
        try:
            try:
                task = task_queue.get(timeout=60)
            except queue.Empty:
                continue
                
            if task is None: break 
            
            gpu_start_time = time.time()
            doc_key = task['doc_key']
            fname = task['name']
            part_path = Path(task['part_path'])
            
            try:
                # --- SMART RESUMPTION: Check if part is already on disk ---
                if part_path.exists() and part_path.stat().st_size > 0:
                    print(f"    [RESUME] Skipping {fname[:30]} P{task['part_idx']+1} (Found on disk)", flush=True)
                elif task['segment'].strip():
                    for attempt in range(3):
                        try:
                            payload = {"model": vllm_config['model'], "messages": [{"role": "system", "content": CLEANUP_SYSTEM_PROMPT}, {"role": "user", "content": task['segment']}], "temperature": 0.0, "chat_template_kwargs": {"enable_thinking": False}}
                            headers = {"Authorization": f"Bearer {vllm_config['key']}", "Connection": "keep-alive"}
                            
                            with session.post(vllm_config['url'], json=payload, headers=headers, timeout=NORM_TIMEOUT) as resp:
                                resp.raise_for_status()
                                cleaned = fast_scrub(resp.json()['choices'][0]['message']['content'])
                                
                            with open(part_path, 'w') as f: f.write(cleaned)
                            elapsed = time.time() - gpu_start_time
                            print(f"    [DONE] {fname[:30]} [Part {task['part_idx']+1}/{task['total_parts']}] ({elapsed:.1f}s)", flush=True)
                            break
                        except Exception as e:
                            if attempt < 2: time.sleep(5)
                            else: raise e
                else:
                    with open(part_path, 'w') as f: f.write("")

            except Exception as e:
                print(f"      ! Fatal Error {fname} P{task['part_idx']}: {str(e)[:100]}", flush=True)
                try:
                    with open(part_path, 'w') as f:
                        f.write(f"\n\n[ERROR: Part {task['part_idx']} failed: {str(e)[:50]}]\n\n")
                except: pass
            finally:
                should_assemble = False
                with completion_lock:
                    doc_part_counters[doc_key] -= 1
                    global_parts_done += 1
                    if doc_part_counters[doc_key] == 0:
                        should_assemble = True
                
                if should_assemble:
                    ASSEMBLY_POOL.submit(assemble_document, doc_lookup[doc_key], vllm_config, INTEL_SESSION)
                
                task_queue.task_done()
                task = None; gc.collect()
        except Exception as e:
            time.sleep(1)

def slice_file_to_queue(f_info, task_queue, doc_lookup):
    f_path, art_path, deal_name, deal_dir = f_info
    global global_total_parts
    try:
        with open(f_path, 'r') as f:
            data = json.load(f)
        full_text = data.get('normalized_text') or data.get('text') or ""
        txt_len = len(full_text)
        total_file_parts = max(1, (txt_len + SAFE_CHAR_LIMIT - CHUNK_OVERLAP - 1) // (SAFE_CHAR_LIMIT - CHUNK_OVERLAP))
        
        doc_key = str(art_path)
        doc_meta = {'final_path': art_path, 'parts': [], 'name': f_path.name, 'total_parts': total_file_parts}
        
        with completion_lock:
            doc_lookup[doc_key] = doc_meta
            doc_part_counters[doc_key] = total_file_parts
            global_total_parts += total_file_parts

        start, p_idx = 0, 0
        while start < txt_len:
            p_path = deal_dir / f"{f_path.name}.p{p_idx}.tmp"
            doc_meta['parts'].append(p_path)
            task_queue.put({
                'name': f_path.name, 'doc_key': doc_key, 'part_idx': p_idx, 
                'total_parts': total_file_parts, 'part_path': str(p_path), 
                'segment': full_text[start:start+SAFE_CHAR_LIMIT]
            })
            start += (SAFE_CHAR_LIMIT - CHUNK_OVERLAP); p_idx += 1
            
        del full_text; del data; gc.collect()
        return True
    except Exception as e:
        print(f">>> [PRODUCER ERROR] Failed to slice {f_path.name}: {e}", flush=True)
        return False

def run():
    global global_total_parts, global_total_files, global_files_done
    if not os.path.exists('deal_discovery.json'): return
    with open('deal_discovery.json', 'r') as f: discovery = json.load(f)
    deals_metadata = sorted(discovery['deals'], key=lambda x: x.get('file_count', 0))

    print(f"\n>>> SCRIPT 2: BANKED-PROGRESS NORMALIZATION (v46)")
    for i, d in enumerate(deals_metadata, 1):
        print(f"  {i:2}. {d['name']:<40}", flush=True)
    
    choice = input("\nEnter indices or 'all': ")
    to_process_meta = []
    try:
        if choice.lower() == 'all': to_process_meta = deals_metadata
        else:
            indices = []
            for part in choice.split(','):
                part = part.strip()
                if '-' in part:
                    s, e = map(int, part.split('-'))
                    indices.extend(range(s-1, e))
                else: indices.append(int(part)-1)
            to_process_meta = [deals_metadata[i] for i in sorted(list(set(indices))) if 0 <= i < len(deals_metadata)]
    except: return

    v_cfg = get_vllm_config(); firehose_session = get_robust_session(pool_size=CONNECTION_POOL_SIZE)
    base_dir = Path("data/extractions"); doc_lookup = {}
    task_queue = queue.Queue(maxsize=TASK_QUEUE_MAXSIZE) 
    
    print("\n>>> PRE-FILTERING...", flush=True)
    files_to_process = []
    visited_files = set()
    for d_meta in to_process_meta:
        deal_dir = base_dir / d_meta['name'].replace(" ", "_").replace("/", "-")
        if not deal_dir.exists(): continue
        for f_path in deal_dir.glob("*.json"):
            if ".artifact.json" in f_path.name or ".part" in f_path.name or ".tmp" in f_path.name: continue
            abs_path = str(f_path.absolute())
            if abs_path in visited_files: continue
            visited_files.add(abs_path)

            art_path = deal_dir / f"{f_path.name.replace('.json', '')}.artifact.json"
            global_total_files += 1
            if art_path.exists(): 
                global_files_done += 1
                continue
            
            # --- FIXED: Do NOT delete old tmp files anymore ---
            files_to_process.append((f_path, art_path, d_meta['name'], deal_dir))

    files_to_process.sort(key=lambda x: x[0].stat().st_size)

    worker_threads = []
    for _ in range(GPU_FIREHOSE_WORKERS):
        t = threading.Thread(target=part_worker_firehose, args=(task_queue, v_cfg, firehose_session, doc_lookup))
        t.daemon = True
        t.start()
        worker_threads.append(t)

    print(f"\n>>> STARTING PARALLEL PUMP ({PRODUCER_SLICER_WORKERS} lanes)...", flush=True)
    with ThreadPoolExecutor(max_workers=PRODUCER_SLICER_WORKERS) as slicer_pool:
        futures = [slicer_pool.submit(slice_file_to_queue, f, task_queue, doc_lookup) for f in files_to_process]
        percent_start = (global_files_done / global_total_files * 100) if global_total_files > 0 else 0
        print(f">>> STARTING PROGRESS: {global_files_done}/{global_total_files} Files Finished ({percent_start:.1f}%)", flush=True)
        for f in as_completed(futures): pass
        for _ in range(GPU_FIREHOSE_WORKERS): task_queue.put(None)

    for t in worker_threads: t.join()
    ASSEMBLY_POOL.shutdown(wait=True)
    print(f"\nPhase 2 Complete.", flush=True)
    os._exit(0)

if __name__ == "__main__": run()
