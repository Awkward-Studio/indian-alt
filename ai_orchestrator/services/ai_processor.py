import json
import logging
import requests
import time
from typing import Dict, Any, Optional, List
from django.conf import settings
from bs4 import BeautifulSoup
import re

from ..models import AIPersonality, AISkill, AIAuditLog

logger = logging.getLogger(__name__)

class AIProcessorService:
    """
    Orchestrated Forensic Pipeline:
    1. OCR PASS: GLM-OCR transcribes all images/PDF pages into high-fidelity text.
    2. REASONING PASS: Qwen 3.5 analyzes combined Email + OCR text for deal signals.
    3. Persists full context for RAG indexing.
    """

    def __init__(self):
        self.ollama_url = getattr(settings, 'OLLAMA_URL', 'http://52.172.249.12:11434')
        self.available_models = self.get_available_models()
        self.text_priority = ['qwen3.5:latest']
        self.vision_priority = ['qwen3.5:latest']

    def get_available_models(self) -> List[str]:
        try:
            response = requests.get(f"{self.ollama_url}/api/tags", timeout=5)
            response.raise_for_status()
            data = response.json()
            return [model['name'] for model in data.get('models', [])]
        except Exception as e:
            logger.error(f"Error fetching Ollama models: {str(e)}")
            return []

    def clean_html(self, html_content: str) -> str:
        if not html_content: return ""
        soup = BeautifulSoup(html_content, "html.parser")
        for script_or_style in soup(["script", "style"]):
            script_or_style.decompose()
        text = soup.get_text()
        text = re.sub(r'\n\s*\n', '\n\n', text)
        text = re.sub(r' +', ' ', text)
        return text.strip()

    def _extract_json(self, text: str) -> str:
        if not text: return "{}"
        json_match = re.search(r'```json\s*(\{.*?\})\s*```', text, re.DOTALL)
        if json_match: return json_match.group(1)
        try:
            last_brace = text.rfind('}')
            if last_brace != -1:
                depth = 0
                for i in range(last_brace, -1, -1):
                    if text[i] == '}': depth += 1
                    if text[i] == '{': depth -= 1
                    if depth == 0: return text[i:last_brace + 1]
        except: pass
        return text

    def ocr_transcribe(self, images: list) -> str:
        """Step 1: Specialized OCR using GLM-OCR specialist model."""
        if not images: return ""
        print(f"[AI-PIPELINE] Phase 1: Transcribing {len(images)} document pages via GLM-OCR...")
        transcription = ""
        for i, img in enumerate(images):
            print(f"    [AI-PIPELINE] Phase 1: Transcribing page {i+1} of {len(images)} via GLM-OCR...")
            payload = {
                "model": "glm-ocr:latest",
                "prompt": "Extract all text and tabular data from this document page exactly. Output Markdown.",
                "images": [img],
                "stream": False,
                "keep_alive": "2h"
            }
            try:
                resp = requests.post(f"{self.ollama_url}/api/generate", json=payload, timeout=120)
                if resp.status_code == 200:
                    page_text = resp.json().get("response", "")
                    transcription += f"\n\n--- PDF PAGE {i+1} TRANSCRIPTION ---\n{page_text}"
            except Exception as e:
                logger.error(f"OCR Phase failed on page {i+1}: {str(e)}")
        return transcription

    def process_content(
        self,
        content: str,
        personality_name: str = "default",
        skill_name: str = "deal_extraction",
        metadata: Optional[Dict[str, Any]] = None,
        source_id: Optional[str] = None,
        source_type: str = "email",
        images: Optional[list] = None,
        model_override: Optional[str] = None,
        stream: bool = False
    ) -> Any:
        """
        Orchestrates the multi-model forensic analysis.
        """
        # PHASE 1: OCR (if images exist)
        ocr_context = ""
        if images and skill_name == "deal_extraction":
            ocr_context = self.ocr_transcribe(images)
            # We add this high-fidelity transcription to our prompt context
            content = f"{content}\n\n[HIGH-FIDELITY DOCUMENT OCR]:\n{ocr_context}"
            # Images are cleared to prevent double-processing and token bloat in Qwen
            images = None 

        # PHASE 2: REASONING (Qwen 3.5)
        print(f"[AI-PIPELINE] Phase 2: Orchestrating Forensic Logic with Qwen 3.5...")
        
        try:
            if personality_name == "default":
                personality = AIPersonality.objects.get(is_default=True)
            else:
                personality = AIPersonality.objects.get(name=personality_name)
        except: personality = None

        try:
            skill = AISkill.objects.get(name=skill_name) if skill_name else None
        except: skill = None

        cleaned_text = self.clean_html(content)
        max_chars = 160000 
        if len(cleaned_text) > max_chars:
            cleaned_text = cleaned_text[:100000] + "\n\n[... TRUNCATED ...]\n\n" + cleaned_text[-60000:]

        system_instructions = personality.system_instructions if personality else "You are a PE analyst."
        
        # --- DYNAMIC PROTOCOL INJECTION ---
        from ..models import AnalysisProtocol
        from .forex_service import ForexService
        
        protocol = AnalysisProtocol.objects.filter(is_active=True).first()
        if protocol and protocol.directives:
            forex = ForexService()
            live_rate = forex.get_crore_string()
            
            directives_text = "\n### INSTITUTIONAL ANALYSIS DIRECTIVES:\n"
            for d in protocol.directives:
                # If the directive mentions currency, append the live rate
                if any(word in d.lower() for word in ['currency', 'inr', 'crore', '$']):
                    directives_text += f"- {d} (CURRENT LIVE RATE: 1M USD = {live_rate})\n"
                else:
                    directives_text += f"- {d}\n"
            system_instructions += directives_text
        # ----------------------------------

        if not stream:
            system_instructions += "\n\nIMPORTANT: Return ONLY a valid JSON object. Do not include any thinking text in the final response."
        
        prompt_template = skill.prompt_template if skill else "Analyze this content."
        user_prompt = prompt_template
        if metadata:
            for key, value in metadata.items():
                user_prompt = re.sub(r'\{\{\s*' + re.escape(key) + r'\s*\}\}', str(value), user_prompt)
        
        user_prompt = re.sub(r'\{\{\s*content\s*\}\}', lambda _: cleaned_text, user_prompt)
        if cleaned_text not in user_prompt:
            user_prompt = f"{user_prompt}\n\nCONTENT:\n{cleaned_text}"

        audit_log = AIAuditLog.objects.create(
            source_type=source_type, source_id=source_id,
            personality=personality, skill=skill,
            model_used='qwen3.5:latest', system_prompt=system_instructions, user_prompt=user_prompt
        )

        payload = {
            "model": 'qwen3.5:latest',
            "prompt": user_prompt,
            "system": system_instructions,
            "stream": stream,
            "keep_alive": "2h",
            "options": {
                "num_ctx": 32768,
                "temperature": 0.1,
                "num_gpu": 99
            }
        }

        if stream:
            return self._stream_response(payload, audit_log)
        
        result = self._standard_response(payload, audit_log)
        # We attach the full combined context (Email + OCR) to the result 
        # so the backend view can save it for RAG
        result["_full_context"] = cleaned_text
        return result

    def _stream_response(self, payload: dict, audit_log: AIAuditLog):
        try:
            response = requests.post(f"{self.ollama_url}/api/generate", json=payload, stream=True, timeout=300)
            response.raise_for_status()
            full_response = ""
            for line in response.iter_lines():
                if line:
                    # Yield raw line so frontend can parse both 'response' and 'thinking'
                    yield line.decode('utf-8') + "\n"
                    
                    chunk = json.loads(line)
                    text = chunk.get("response") or chunk.get("thinking", "")
                    full_response += text
                    if chunk.get("done"): break
            audit_log.raw_response = full_response
            audit_log.is_success = True
            audit_log.save()
        except Exception as e:
            yield json.dumps({"response": f"Error: {str(e)}", "done": True})

    def _standard_response(self, payload: dict, audit_log: AIAuditLog) -> Dict[str, Any]:
        start_time = time.time()
        try:
            response = requests.post(f"{self.ollama_url}/api/generate", json=payload, timeout=300)
            response.raise_for_status()
            data = response.json()
            
            raw_response = data.get("response") or data.get("thinking", "")
            thinking = data.get("thinking", "")
            
            audit_log.raw_response = raw_response
            clean_json_str = self._extract_json(raw_response)
            try:
                parsed_json = json.loads(clean_json_str)
                if "deal_model_data" not in parsed_json: parsed_json["deal_model_data"] = {}
                if "metadata" not in parsed_json: parsed_json["metadata"] = {"ambiguous_points": [], "missing_fields": []}
                if "analyst_report" not in parsed_json: parsed_json["analyst_report"] = raw_response
                
                # Include thinking in the parsed response for the UI
                parsed_json["thinking"] = thinking
                
                audit_log.parsed_json = parsed_json
                audit_log.is_success = True
                parsed_json["_raw_response"] = raw_response
            except:
                audit_log.is_success = False
                parsed_json = {"error": "JSON parsing error", "raw": raw_response, "thinking": thinking}
        except Exception as e:
            audit_log.is_success = False
            parsed_json = {"error": str(e)}
        finally:
            audit_log.request_duration_ms = int((time.time() - start_time) * 1000)
            audit_log.save()
        return parsed_json
