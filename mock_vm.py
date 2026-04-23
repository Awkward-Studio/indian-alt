import uvicorn
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel
import time
import random

app = FastAPI()

class ExtractRequest(BaseModel):
    filename: str
    content_base64: str

@app.post("/extract/document")
async def mock_extract(request: ExtractRequest, authorization: str = Header(None)):
    # 1. Check Auth
    if not authorization or "local-dev-key" not in authorization:
        raise HTTPException(status_code=401, detail="Unauthorized")
    
    # 2. Simulate Work (Sleep for 2-5 seconds)
    work_time = random.uniform(2, 5)
    time.sleep(work_time)
    
    print(f"  [MOCK VM] Processed: {request.filename} in {work_time:.1f}s")
    
    # 3. Return fake OCR data
    return {
        "normalized_text": f"MOCK DATA FOR {request.filename}\nThis is a simulated OCR response.",
        "transcription_status": "complete",
        "quality_flags": ["mock_verified"],
        "render_metadata": {"page_count": 1}
    }

if __name__ == "__main__":
    print(">>> MOCK VM STARTING ON PORT 8100...")
    print(">>> Point your bulk_1_extract.py to http://localhost:8100 to test for free.")
    uvicorn.run(app, host="0.0.0.0", port=8100)
