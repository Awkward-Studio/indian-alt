import os
import json
import time
import sys
import threading
import queue
import gc
import socket
import requests
import re
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ==============================================================================
# MASTER TUNING DASHBOARD (INTELLIGENCE-FUSION v46)
# ==============================================================================
# 1. WORKER POOLS
# Synthesis is heavy on VRAM. 32 workers is the optimal limit for 250k prompts.
GPU_FIREHOSE_WORKERS = 32       
CONNECTION_POOL_SIZE = 300      

# 2. HIERARCHICAL SETTINGS
# Since we only use structured intel, 250k chars covers even 1,000-file deals.
CONTEXT_SAFE_LIMIT = 250000     
BUCKET_SIZE_CHARS = 100000      # Fallback bucket size for astronomical deals
# 3. NETWORK SETTINGS
# 20 minutes allowed for deep-reasoning synthesis passes.
NORM_TIMEOUT = (15, 1200)       
# ==============================================================================

# --- PRE-COMPILED ROBOTIC SCRUBBER ---
THINK_TAGS = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
PREAMBLE_PATTERNS = re.compile(r"^(Here is|Here's|Sure|Okay|Attached|I have|Cleaned).*?\n", re.MULTILINE | re.IGNORECASE)
MD_WRAPPERS = re.compile(r"^```json\n|^```markdown\n|^```\w*\n|```\n?$", re.IGNORECASE | re.MULTILINE)

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
    retries = Retry(total=5, backoff_factor=2, status_forcelist=[400, 500, 502, 503, 504])
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

CONTACT_SCHEMA = {
  "type": "object",
  "properties": {
    "name": {"type": ["string", "null"]},
    "email": {"type": ["string", "null"]},
    "designation": {"type": ["string", "null"]},
    "linkedin_url": {"type": ["string", "null"]},
    "phone": {"type": ["string", "null"]},
    "location": {"type": ["string", "null"]},
    "bank_name": {"type": ["string", "null"]},
    "bank_domain": {"type": ["string", "null"]}
  },
  "required": ["name", "email", "designation", "linkedin_url", "phone", "location", "bank_name", "bank_domain"],
  "additionalProperties": False
}

BANK_SCHEMA = {
  "type": "object",
  "properties": {
    "name": {"type": ["string", "null"]},
    "website_domain": {"type": ["string", "null"]},
    "description": {"type": ["string", "null"]}
  },
  "required": ["name", "website_domain", "description"],
  "additionalProperties": False
}

# STRUCTURED SCHEMA FOR PORTABLE DEAL
DEAL_SCHEMA = {
  "type": "object",
  "properties": {
    "deal_model_data": {
      "type": "object",
      "properties": {
        "title": {"type": "string"},
        "industry": {"type": "string"},
        "sector": {"type": "string"},
        "funding_ask": {"type": "string"},
        "funding_ask_for": {"type": "string"},
        "priority": {"type": "string", "enum": ["High", "Medium", "Low"]},
        "city": {"type": "string"},
        "state": {"type": "string"},
        "country": {"type": "string"},
        "themes": {"type": "array", "items": {"type": "string"}},
        "is_female_led": {"type": "boolean"},
        "deal_summary": {"type": "string"},
        "deal_details": {"type": "string"},
        "company_details": {"type": "string"},
        "priority_rationale": {"type": "string"}
      },
      "required": [
        "title", "industry", "sector", "funding_ask", "funding_ask_for", 
        "priority", "city", "state", "country", "themes", 
        "is_female_led", "deal_summary", "deal_details", "company_details", "priority_rationale"
      ],
      "additionalProperties": False
    },
    "source_relationships": {
      "type": "object",
      "properties": {
        "bank": BANK_SCHEMA,
        "primary_contact": {"anyOf": [CONTACT_SCHEMA, {"type": "null"}]},
        "additional_contacts": {"type": "array", "items": CONTACT_SCHEMA},
        "relationship_metadata": {
          "type": "object",
          "properties": {
            "source_type": {"type": ["string", "null"]},
            "source_documents": {"type": "array", "items": {"type": "string"}},
            "confidence": {"type": ["string", "null"], "enum": ["High", "Medium", "Low", None]},
            "ambiguities": {"type": "array", "items": {"type": "string"}}
          },
          "required": ["source_type", "source_documents", "confidence", "ambiguities"],
          "additionalProperties": False
        }
      },
      "required": ["bank", "primary_contact", "additional_contacts", "relationship_metadata"],
      "additionalProperties": False
    },
    "analyst_report": {"type": "string"},
    "metadata": {
      "type": "object",
      "properties": {
        "ambiguous_points": {"type": "array", "items": {"type": "string"}},
        "documents_analyzed": {"type": "array", "items": {"type": "string"}},
        "cross_document_conflicts": {
          "type": "array",
          "items": {
            "type": "object",
            "properties": {
              "topic": {"type": "string"},
              "details": {"type": "string"},
              "citations": {"type": "array", "items": {"type": "string"}}
            },
            "required": ["topic", "details", "citations"],
            "additionalProperties": False
          }
        },
        "missing_information_requests": {"type": "array", "items": {"type": "string"}}
      },
      "required": ["ambiguous_points", "documents_analyzed", "cross_document_conflicts", "missing_information_requests"],
      "additionalProperties": False
    }
  },
  "required": ["deal_model_data", "source_relationships", "analyst_report", "metadata"],
  "additionalProperties": False
}

SYSTEM_PROMPT = """You are a Senior Investment Analyst at India Alternatives. 
You are provided with structured summaries, metrics, and risks extracted from a folder of deal documents.
Synthesize this pre-extracted evidence into a structured investment report and deal model data.
The input is structured document metadata only. Weight KPIs, summaries, risks, table summaries, and document types more heavily than any single source.
Also extract the source bank/advisory firm and banker relationships for later import into the app.

RELATIONSHIP RULES:
- Populate source_relationships.bank with the bank or advisory firm the deal came from.
- Populate source_relationships.primary_contact with the main banker/contact when identifiable.
- Populate source_relationships.additional_contacts only with clearly identified secondary bankers/advisors.
- If only a bank is known, keep bank populated and primary_contact null.
- If a contact is known but the bank is unclear, keep the contact, leave bank fields null if needed, and explain the gap in relationship_metadata.ambiguities.
- Use source_documents to list the filenames that support the relationship extraction.
- Keep deal attributes in deal_model_data and relationship data in source_relationships. Do not mix them.
- Do not assume access to raw normalized document text. Use only the extracted metadata provided per document."""

def is_relationship_document(name, doc_type):
    doc_name = (name or "").lower()
    doc_type = (doc_type or "").lower()
    relationship_markers = (
        "teaser", "deck", "pitch", "imt", "im ", "information memorandum",
        "cover", "email", "mail", "mandate", "advisor", "advisory", "banker",
        "proposal", "introduction", "one pager", "one-pager", "nda"
    )
    return any(marker in doc_name or marker in doc_type for marker in relationship_markers)

def metadata_only_artifact(data):
    return {
        key: value
        for key, value in (data or {}).items()
        if key != 'normalized_text'
    }

def build_intel_item(data):
    artifact_metadata = metadata_only_artifact(data)
    document_name = artifact_metadata.get('document_name')
    document_type = artifact_metadata.get('document_type')
    relationship_doc = is_relationship_document(document_name, document_type)

    return {
        "source": document_name,
        "type": document_type,
        "summary": artifact_metadata.get('document_summary'),
        "metrics": artifact_metadata.get('metrics'),
        "risks": artifact_metadata.get('risks'),
        "tables": artifact_metadata.get('tables_summary'),
        "quality_flags": artifact_metadata.get('quality_flags') or [],
        "source_file": artifact_metadata.get('source_file'),
        "relationship_signal": relationship_doc,
        "document_analysis": artifact_metadata,
    }

def build_document_metadata(data):
    artifact_metadata = metadata_only_artifact(data)
    document_name = artifact_metadata.get('document_name')
    document_type = artifact_metadata.get('document_type')
    relationship_doc = is_relationship_document(document_name, document_type)

    return {
        **artifact_metadata,
        "relationship_signal": relationship_doc,
    }

deal_bucket_counters = {}
completion_lock = threading.Lock()

def swarm_worker(task_queue, v_cfg, session):
    """Immortal swarm worker for synthesis passes."""
    while True:
        try:
            task = task_queue.get()
            if task is None:
                task_queue.task_done()
                break
            
            deal_name = task['deal_name']
            deal_dir = Path(task['deal_dir'])
            task_type = task['type'] 

            try:
                headers = {"Authorization": f"Bearer {v_cfg['key']}"}
                
                if task_type == 'bucket':
                    # Fallback for truly astronomical deals
                    print(f"    [BUCKET] {deal_name}: Fusing Intermediate batch {task['idx']+1}...", flush=True)
                    bucket_start = time.time()
                    payload = {
                        "model": v_cfg['model'],
                        "messages": [
                            {"role": "system", "content": "Extract and group all financial KPIs, risks, and business facts from this Intel batch. Bullets only."},
                            {"role": "user", "content": task['content']}
                        ],
                        "temperature": 0.0,
                        "chat_template_kwargs": {"enable_thinking": False}
                    }
                    with session.post(v_cfg['url'], json=payload, headers=headers, timeout=NORM_TIMEOUT) as resp:
                        resp.raise_for_status()
                        result_text = resp.json()['choices'][0]['message']['content']
                        
                    bucket_path = deal_dir / f"bucket_{task['idx']}.tmp"
                    bucket_path.write_text(result_text)
                    elapsed = time.time() - bucket_start
                    print(
                        f"    [BUCKET-DONE] {deal_name}: Intermediate batch {task['idx']+1} completed ({elapsed:.1f}s)",
                        flush=True,
                    )
                    
                    should_fuse = False
                    with completion_lock:
                        deal_bucket_counters[deal_name] -= 1
                        if deal_bucket_counters[deal_name] == 0: should_fuse = True
                    
                    if should_fuse:
                        print(f"    [QUEUE-FUSION] {deal_name}: All buckets complete, queueing final fusion...", flush=True)
                        task_queue.put({
                            'type': 'fusion',
                            'deal_name': deal_name,
                            'deal_dir': deal_dir,
                            'is_hierarchical': True,
                            'document_metadata': task.get('document_metadata') or []
                        })

                elif task_type == 'fusion':
                    print(f"    [FUSION] {deal_name}: Finalizing Portable Artifact...", flush=True)
                    
                    if task.get('is_hierarchical'):
                        evidence = []
                        for b_path in sorted(deal_dir.glob("bucket_*.tmp")):
                            evidence.append(b_path.read_text()); b_path.unlink()
                        combined_evidence = "\n\n--- NEXT EVIDENCE BATCH ---\n\n".join(evidence)
                    else:
                        combined_evidence = task['content']

                    payload = {
                        "model": v_cfg['model'],
                        "messages": [
                            {"role": "system", "content": SYSTEM_PROMPT},
                            {"role": "user", "content": f"STRUCTURED INTEL FROM ALL DEAL DOCUMENTS:\n\n{combined_evidence}"}
                        ],
                        "response_format": {"type": "json_schema", "json_schema": {"name": "deal_synth", "schema": DEAL_SCHEMA, "strict": True}},
                        "temperature": 0.0,
                        "chat_template_kwargs": {"enable_thinking": True} # Thinking ON for final synthesis
                    }
                    
                    with session.post(v_cfg['url'], json=payload, headers=headers, timeout=NORM_TIMEOUT) as resp:
                        resp.raise_for_status()
                        data = resp.json()
                        raw_content = data['choices'][0]['message']['content']
                        thinking = data['choices'][0]['message'].get('thinking', "No thinking trace.")
                        
                        try: structured_data = json.loads(fast_scrub(raw_content))
                        except: structured_data = {"error": "JSON parse failed", "raw": raw_content}

                        master_artifact = {
                            "deal_name": deal_name,
                            "portable_deal_data": structured_data,
                            "thinking_process": thinking,
                            "metadata": {
                                "timestamp": time.time(),
                                "version": "v49-fusion-metadata-only",
                                "documents_used_count": len(task.get('document_metadata') or []),
                                "documents_used": task.get('document_metadata') or [],
                            }
                        }
                        
                        (deal_dir / "DEAL_SYNTHESIS.artifact.json").write_text(json.dumps(master_artifact, indent=2))
                        (deal_dir / "INVESTMENT_REPORT.md").write_text(structured_data.get('analyst_report', 'Report failed.'))
                        print(f"    [SUCCESS] {deal_name}: Artifact saved.", flush=True)

            except Exception as e:
                print(f"    [ERROR] {deal_name} ({task_type}): {e}", flush=True)
            finally:
                task_queue.task_done()
                gc.collect()

        except Exception as e:
            print(f"Worker Recovery: {e}", flush=True)
            time.sleep(5)

def run():
    if not os.path.exists('deal_discovery.json'): return
    with open('deal_discovery.json', 'r') as f: discovery = json.load(f)
    deals_metadata = sorted(discovery['deals'], key=lambda x: x.get('file_count', 0))

    print(f"\n>>> SCRIPT 3: INTELLIGENCE-FUSION SYNTHESIS (v46)")
    for i, d in enumerate(deals_metadata, 1):
        print(f"  {i:2}. {d['name']:<40} ({d.get('file_count', 0)} files)")
    
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

    redo_choice = input("Redo existing synthesis for selected deals? (y/n): ").lower().strip()
    redo_all = (redo_choice == 'y')

    v_cfg = get_vllm_config(); session = get_robust_session(pool_size=CONNECTION_POOL_SIZE)
    base_dir = Path("data/extractions"); task_queue = queue.Queue()

    print(f"\n>>> SCANNING & INJECTING INTEL (Redo={'ON' if redo_all else 'OFF'})...")
    deals_queued = 0
    for d_meta in to_process:
        deal_dir = base_dir / d_meta['name'].replace(" ", "_").replace("/", "-")
        if not deal_dir.exists(): continue
        
        # Skip if already done AND redo is not requested
        if (deal_dir / "DEAL_SYNTHESIS.artifact.json").exists() and not redo_all:
            print(f"    [SKIPPED] {d_meta['name']} (Artifact already exists)")
            continue

        # DIRECT JSON INJECTION: Load only structured intel from Phase 2
        all_intel = []
        document_metadata = []
        for p in sorted(deal_dir.glob("*.artifact.json")):
            if "DEAL_SYNTHESIS" in p.name: continue
            try:
                with open(p, 'r') as f:
                    data = json.load(f)
                    all_intel.append(json.dumps(build_intel_item(data)))
                    document_metadata.append(build_document_metadata(data))
            except: continue
        
        if not all_intel: continue
        
        total_len = sum(len(t) for t in all_intel)
        if total_len < CONTEXT_SAFE_LIMIT:
            task_queue.put({
                'type': 'fusion', 'deal_name': d_meta['name'], 'deal_dir': deal_dir, 
                'content': "\n\n".join(all_intel), 'is_hierarchical': False,
                'document_metadata': document_metadata
            })
        else:
            # Hierarchical Bucketing (Fallback for truly massive folders)
            buckets = []
            curr_b = ""
            for intel_str in all_intel:
                if len(curr_b) + len(intel_str) > BUCKET_SIZE_CHARS:
                    if curr_b:
                        buckets.append(curr_b)
                    curr_b = intel_str
                else:
                    curr_b = f"{curr_b}\n\n{intel_str}" if curr_b else intel_str
            if curr_b: buckets.append(curr_b)
            
            with completion_lock:
                deal_bucket_counters[d_meta['name']] = len(buckets)
            for i, b_content in enumerate(buckets):
                task_queue.put({
                    'type': 'bucket',
                    'deal_name': d_meta['name'],
                    'deal_dir': deal_dir,
                    'content': b_content,
                    'idx': i,
                    'document_metadata': document_metadata
                })
        deals_queued += 1

    print(f">>> STARTING Swarm ({GPU_FIREHOSE_WORKERS} lanes) for {deals_queued} deals...")
    worker_threads = []
    for _ in range(GPU_FIREHOSE_WORKERS):
        t = threading.Thread(target=swarm_worker, args=(task_queue, v_cfg, session))
        t.daemon = True
        t.start()
        worker_threads.append(t)

    task_queue.join()
    for _ in range(GPU_FIREHOSE_WORKERS):
        task_queue.put(None)
    for t in worker_threads: t.join()

    print(f"\nPhase 3 Complete. Master Artifacts saved.")
    os._exit(0)

if __name__ == "__main__": run()
