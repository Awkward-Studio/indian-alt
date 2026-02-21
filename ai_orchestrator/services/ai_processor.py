import json
import logging
import requests
import time
from typing import Dict, Any, Optional
from django.conf import settings
from bs4 import BeautifulSoup
import re

from ..models import AIPersonality, AISkill, AIAuditLog

logger = logging.getLogger(__name__)

class AIProcessorService:
    """
    Service for processing emails and other content using an LLM.
    Handles text cleaning, prompt construction, and result logging.
    Includes an autonomous routing engine to pick the best model for the task.
    """

    def __init__(self):
        # Configure the default Ollama settings
        self.ollama_url = getattr(settings, 'OLLAMA_URL', 'http://20.204.254.152:11434')
        self.default_text_model = getattr(settings, 'OLLAMA_DEFAULT_TEXT_MODEL', 'llama3.1:latest')
        self.default_vision_model = getattr(settings, 'OLLAMA_DEFAULT_VISION_MODEL', 'llava:latest')

    def get_available_models(self) -> list:
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

    def clean_html(self, html_content: str) -> str:
        """
        Removes HTML tags and returns clean text using BeautifulSoup.
        """
        if not html_content:
            return ""
        
        soup = BeautifulSoup(html_content, "html.parser")
        for script_or_style in soup(["script", "style"]):
            script_or_style.decompose()
            
        text = soup.get_text()
        lines = (line.strip() for line in text.splitlines())
        chunks = (phrase.strip() for line in lines for phrase in line.split("  "))
        text = '\n'.join(chunk for chunk in chunks if chunk)
        
        return text

    def strip_signatures(self, text: str) -> str:
        """
        Simple signature stripping using common markers.
        """
        if not text:
            return ""
        
        signature_markers = [
            r'--\s*$',
            r'^Best regards,',
            r'^Regards,',
            r'^Sincerely,',
            r'^Thanks,',
            r'^Warm regards,',
            r'^Kind regards,',
            r'^Sent from my iPhone',
            r'^Sent from my Android',
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
        - If skill is 'document_analysis' (likely charts in PDF/XLS) -> Vision Model
        - If content mentions "chart", "table", "image", or "attached" -> Vision Model
        - Otherwise -> Text Model
        """
        if images:
            return "vision"
        
        if skill_name == "document_analysis":
            return "vision"
            
        vision_keywords = ['chart', 'table', 'graph', 'diagram', 'image', 'photo', 'screenshot']
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
        images: Optional[list] = None
    ) -> Dict[str, Any]:
        """
        Orchestrates the LLM processing pipeline with autonomous routing.
        """
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
        if personality:
            model_name = personality.vision_model_name if route == "vision" else personality.text_model_name
            model_provider = personality.model_provider
        else:
            model_name = self.default_vision_model if route == "vision" else self.default_text_model
            model_provider = "ollama"

        # 3. Clean content
        cleaned_text = self.clean_html(content)
        cleaned_text = self.strip_signatures(cleaned_text)
        
        # 4. Construct Prompts
        system_instructions = personality.system_instructions if personality else "You are a Private Equity analyst. Be precise and return JSON only."
        prompt_template = skill.prompt_template if skill else "Analyze this content and return JSON."
        
        if metadata:
            for key, value in metadata.items():
                prompt_template = prompt_template.replace(f"{{{{ {key} }}}}", str(value))
        
        user_prompt = f"{prompt_template}\n\nCONTENT:\n{cleaned_text}"
        
        # 5. Call LLM
        start_time = time.time()
        payload = {
            "model": model_name,
            "prompt": user_prompt,
            "system": system_instructions,
            "stream": False,
            "format": "json"
        }
        
        if images:
            payload["images"] = images
        
        audit_log = AIAuditLog(
            source_type=source_type,
            source_id=source_id,
            personality=personality,
            skill=skill,
            model_provider=model_provider,
            model_used=model_name,
            system_prompt=system_instructions,
            user_prompt=user_prompt
        )
        
        try:
            # Note: This logic currently assumes Ollama. If model_provider is 'openai', 
            # you would add a switch statement here to call OpenAI instead.
            response = requests.post(f"{self.ollama_url}/api/generate", json=payload, timeout=90)
            response.raise_for_status()
            data = response.json()
            
            raw_response = data.get("response", "")
            audit_log.raw_response = raw_response
            
            try:
                parsed_json = json.loads(raw_response)
                audit_log.parsed_json = parsed_json
                audit_log.is_success = True
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
