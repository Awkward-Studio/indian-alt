import time
import random
import json
from fastapi import FastAPI, Request
import uvicorn

app = FastAPI()

# Track peak concurrency
max_concurrent = 0
current_running = 0

@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    global current_running, max_concurrent
    body = await request.json()
    
    current_running += 1
    if current_running > max_concurrent:
        max_concurrent = current_running
    
    print(f">>> [MOCK VM] INCOMING REQUEST | Running: {current_running} | Peak: {max_concurrent}")
    
    # Simulate H100 thinking time
    time.sleep(1)
    
    # Get the user content
    user_msg = body['messages'][-1]['content']

    # --- THE FULL ECHO PROOF ---
    if "response_format" in body:
        # If metadata pass, return valid JSON
        response_content = '{"document_type": "Pitch Deck", "document_summary": "FULL TEXT VERIFIED", "metrics": {"status": "SUCCESS"}, "risks": [], "tables_summary": "None"}'
    else:
        # If cleaning pass, echo the input EXACTLY to prove no truncation
        # We just remove our internal header if it exists
        original_text = user_msg.split("segment:\n\n")[-1] if "segment:\n\n" in user_msg else user_msg
        response_content = f"[MOCK CLEANED START]\n{original_text}\n[MOCK CLEANED END]"

    current_running -= 1
    
    return {
        "choices": [{
            "message": {
                "content": response_content
            }
        }],
        "usage": {"total_tokens": random.randint(100, 500)}
    }

if __name__ == "__main__":
    print(">>> MOCK vLLM STARTING (FULL ECHO MODE)...")
    uvicorn.run(app, host="0.0.0.0", port=8000)
