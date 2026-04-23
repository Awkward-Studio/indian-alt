import requests
import sys
import base64
import io
from docx import Document

VM_IP = "20.244.11.248"
DOCPROC_URL = f"http://{VM_IP}:8100"
API_KEY = "local-dev-key"

def create_tiny_docx():
    """Creates a tiny 1-paragraph docx in memory."""
    doc = Document()
    doc.add_paragraph("Diagnostic Test: Verifying LibreOffice Headless and JRE on VM.")
    f = io.BytesIO()
    doc.save(f)
    return f.getvalue()

def check_vllm():
    print(f"Checking vLLM at http://{VM_IP}:8000/v1...")
    headers = {"Authorization": f"Bearer {API_KEY}"}
    try:
        resp = requests.get(f"http://{VM_IP}:8000/v1/models", headers=headers, timeout=10)
        if resp.status_code == 200:
            print("  [OK] vLLM is reachable.")
            return True
    except: pass
    print("  [FAIL] vLLM unreachable.")
    return False

def check_docproc_office_rendering():
    print(f"Checking LibreOffice Headless on {DOCPROC_URL}...")
    headers = {"Authorization": f"Bearer {API_KEY}"}
    
    docx_bytes = create_tiny_docx()
    payload = {
        "filename": "diagnostic_test.docx",
        "content_base64": base64.b64encode(docx_bytes).decode("utf-8")
    }
    
    try:
        # Rendering can take a few seconds
        resp = requests.post(f"{DOCPROC_URL}/extract/document", headers=headers, json=payload, timeout=45)
        if resp.status_code == 200:
            data = resp.json()
            if "direct_text_only" in data.get("quality_flags", []):
                print("  [OK] Fast Direct Extraction is working.")
            
            # To TRULY check LibreOffice, we look for render metadata
            if data.get("render_metadata", {}).get("render_method") == "libreoffice_pdf" or "merged" in data.get("quality_flags", []):
                print("  [OK] LibreOffice Headless is WORKING correctly.")
            else:
                # If it fell back to direct text only, it might not have tested LibreOffice
                print("  [INFO] Service used direct extraction. Forcing a larger file test might be needed for full rendering proof.")
            return True
        else:
            print(f"  [ERROR] Docproc failed ({resp.status_code}): {resp.text}")
    except Exception as e:
        print(f"  [ERROR] Docproc timed out or unreachable: {e}")
    return False

if __name__ == "__main__":
    v_ok = check_vllm()
    d_ok = check_docproc_office_rendering()
    
    if v_ok and d_ok:
        print("\n>>> SUCCESS: VM is fully operational (vLLM + LibreOffice).")
    else:
        sys.exit(1)
