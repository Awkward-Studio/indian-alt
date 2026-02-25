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
    Service for processing emails and other content using an LLM.
    Handles dynamic model selection based on available models in Ollama.
    """

    def __init__(self):
        # Configure the default Ollama settings
        self.ollama_url = getattr(settings, 'OLLAMA_URL', 'http://52.172.249.12:11434')
        self.available_models = self.get_available_models()
        
        # Priority list for T4 GPU (16GB VRAM)
        self.text_priority = ['mistral-nemo:latest', 'qwen2.5:7b', 'llama3.1:8b', 'gemma3:4b']
        # Priority list for Vision/Complex models
        self.vision_priority = ['qwen2.5vl:7b', 'llama3.2-vision:latest', 'llava:latest']

    def get_available_models(self) -> List[str]:
        """
        Fetches the list of available models from the Ollama API.
        """
        try:
            response = requests.get(f"{self.ollama_url}/api/tags", timeout=5)
            response.raise_for_status()
            data = response.json()
            return [model['name'] for model in data.get('models', [])]
        except Exception as e:
            logger.error(f"Error fetching Ollama models: {str(e)}")
            return []

    def select_best_model(self, model_type: str = "text") -> str:
        """
        Selects the best available model based on a priority list.
        """
        priority_list = self.vision_priority if model_type == "vision" else self.text_priority
        
        for model in priority_list:
            if model in self.available_models:
                return model
        
        # Fallback to whatever is available if priority list fails
        if self.available_models:
            return self.available_models[0]
            
        return "llama3.1:latest" # Absolute fallback

    def clean_html(self, html_content: str) -> str:
        if not html_content:
            return ""
        soup = BeautifulSoup(html_content, "html.parser")
        for script_or_style in soup(["script", "style"]):
            script_or_style.decompose()
        text = soup.get_text()
        
        # Remove noisy artifacts from Docling/OCR
        # 1. Remove <!-- image ... --> tags
        text = re.sub(r'<!-- image .*? -->', '', text, flags=re.DOTALL)
        # 2. Remove long base64-like blocks or random binary noise (strings > 100 chars without spaces)
        text = re.sub(r'[A-Za-z0-9+/=]{100,}', ' [DATA BLOCK] ', text)
        # 3. Aggressive Markdown Cleanup: Remove table borders and repetitive symbols that confuse smaller models
        text = text.replace('|---|', ' ').replace('|', ' ')
        text = re.sub(r'#{2,}', ' ', text) # Remove multiple hashtags
        text = re.sub(r'[\-\*\_]{3,}', ' ', text) # Remove long separators
        # 4. Collapse whitespace
        text = re.sub(r'\s+', ' ', text)
        
        lines = (line.strip() for line in text.splitlines())
        chunks = (phrase.strip() for line in lines for phrase in line.split("  "))
        return '\n'.join(chunk for chunk in chunks if chunk)

    def strip_signatures(self, text: str) -> str:
        if not text:
            return ""
        signature_markers = [
            r'--\s*$', r'^Best regards,', r'^Regards,', r'^Sincerely,', 
            r'^Thanks,', r'^Warm regards,', r'^Kind regards,',
            r'^Sent from my iPhone', r'^Sent from my Android',
        ]
        lines = text.splitlines()
        for i, line in enumerate(lines):
            for marker in signature_markers:
                if re.search(marker, line, re.IGNORECASE):
                    return '\n'.join(lines[:i]).strip()
        return text.strip()

    def route_request(self, images: list, skill_name: str, content: str) -> str:
        """
        Autonomous routing logic:
        - If images are present -> Vision Model
        - If skill involves documents or complex tables -> Vision Model (Qwen2.5-VL)
        - If content mentions complex visual structures -> Vision Model
        """
        if images:
            return "vision"
        
        if skill_name in ["document_analysis", "deal_extraction"]:
            # Even for deal extraction, if we have markdown from Docling, 
            # a vision-capable or high-context model is better.
            return "vision"
            
        vision_keywords = ['chart', 'table', 'graph', 'diagram', 'image', 'screenshot', '|---|'] # |---| for markdown tables
        if any(keyword in content.lower() for keyword in vision_keywords):
            return "vision"
            
        return "text"

    def process_content(
        self,
        content: str,
        personality_name: str = "default",
        skill_name: str = "deal_extraction",
        metadata: Optional[Dict[str, Any]] = None,
        source_id: Optional[str] = None,
        source_type: str = "email",
        images: Optional[list] = None,
        model_override: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Orchestrates the LLM processing pipeline. 
        If model_override is provided, it attempts to use that specific model.
        """
        # Refresh available models
        self.available_models = self.get_available_models()

        # 1. Fetch Personality and Skill
        try:
            if personality_name == "default":
                personality = AIPersonality.objects.get(is_default=True)
            else:
                personality = AIPersonality.objects.get(name=personality_name)
        except AIPersonality.DoesNotExist:
            personality = None

        try:
            skill = AISkill.objects.get(name=skill_name)
        except AISkill.DoesNotExist:
            skill = None

        # 2. Routing Decision
        route = self.route_request(images, skill_name, content)
        
        # 3. Model Selection
        if model_override and model_override in self.available_models:
            selected_model = model_override
        else:
            selected_model = self.select_best_model(route)
        
        model_provider = "ollama"

        # 4. Clean content
        cleaned_text = self.clean_html(content)
        cleaned_text = self.strip_signatures(cleaned_text)
        
        # 5. Smart Truncation
        max_chars = 96000
        if len(cleaned_text) > max_chars:
            cleaned_text = cleaned_text[:60000] + "\n\n[... TRUNCATED ...]\n\n" + cleaned_text[-36000:]

        # 6. Construct Prompts
        system_instructions = personality.system_instructions if personality else "You are a Private Equity analyst."
        prompt_template = skill.prompt_template if skill else "Analyze this content."
        
        if metadata:
            for key, value in metadata.items():
                prompt_template = prompt_template.replace(f"{{{{ {key} }}}}", str(value))
        
        user_prompt = f"{prompt_template}\n\nCONTENT:\n{cleaned_text}"
        
        # 7. Call LLM
        start_time = time.time()
        payload = {
            "model": selected_model,
            "prompt": user_prompt,
            "system": system_instructions,
            "stream": False,
            "format": "json",
            "keep_alive": "30m", # Keep model in VRAM for 30 mins to avoid re-loads
            "options": {
                "num_ctx": 32768,
                "temperature": 0.1
            }
        }
        
        if images:
            payload["images"] = images
        
        audit_log = AIAuditLog(
            source_type=source_type,
            source_id=source_id,
            personality=personality,
            skill=skill,
            model_provider=model_provider,
            model_used=selected_model,
            system_prompt=system_instructions,
            user_prompt=user_prompt
        )
        
        try:
            # Increased timeout to 180s to allow for model loading/swapping
            response = requests.post(f"{self.ollama_url}/api/generate", json=payload, timeout=180)
            response.raise_for_status()
            data = response.json()
            
            raw_response = data.get("response", "")
            audit_log.raw_response = raw_response
            
            try:
                parsed_json = json.loads(raw_response)
                audit_log.parsed_json = parsed_json
                audit_log.is_success = True
                # Return both parsed and raw for full visibility
                parsed_json["_raw_response"] = raw_response
            except json.JSONDecodeError:
                audit_log.is_success = False
                audit_log.error_message = "JSON parsing error"
                parsed_json = {"error": "JSON parsing error", "raw": raw_response}
                
        except Exception as e:
            logger.error(f"Error calling LLM: {str(e)}")
            audit_log.is_success = False
            audit_log.error_message = str(e)
            parsed_json = {"error": str(e)}
            
        finally:
            audit_log.request_duration_ms = int((time.time() - start_time) * 1000)
            audit_log.save()
            
        return parsed_json
