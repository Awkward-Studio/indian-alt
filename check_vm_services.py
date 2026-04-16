import requests
import sys
import base64

VM_IP = "20.244.11.248"
VLLM_URL = f"http://{VM_IP}:8000/v1"
DOCPROC_URL = f"http://{VM_IP}:8100"
API_KEY = "local-dev-key"

def check_vllm():
    print(f"Checking vLLM at {VLLM_URL}...")
    headers = {"Authorization": f"Bearer {API_KEY}"}
    try:
        resp = requests.get(f"{VLLM_URL}/models", headers=headers, timeout=10)
        if resp.status_code == 200:
            return True
        else:
            print(f"  [ERROR] vLLM returned status {resp.status_code}")
    except Exception as e:
        print(f"  [ERROR] Could not reach vLLM: {e}")
    return False

def check_docproc_auth():
    print(f"Checking Docproc Auth at {DOCPROC_URL}...")
    headers = {"Authorization": f"Bearer {API_KEY}"}
    # Send a tiny dummy request to verify auth
    payload = {
        "filename": "test.txt",
        "content_base64": base64.b64encode(b"hello").decode("utf-8")
    }
    try:
        resp = requests.post(f"{DOCPROC_URL}/extract/document", headers=headers, json=payload, timeout=10)
        if resp.status_code == 200:
            print("  [OK] Docproc Auth is SUCCESSFUL.")
            return True
        elif resp.status_code == 401:
            print("  [ERROR] Docproc Auth FAILED (401 Unauthorized). Check the key on the VM.")
        else:
            print(f"  [ERROR] Docproc returned status {resp.status_code}: {resp.text}")
    except Exception as e:
        print(f"  [ERROR] Could not reach Docproc: {e}")
    return False

if __name__ == "__main__":
    vllm_ok = check_vllm()
    docproc_ok = check_docproc_auth()
    
    if vllm_ok and docproc_ok:
        print("\n>>> SUCCESS: Connectivity and Auth verified.")
    else:
        sys.exit(1)
