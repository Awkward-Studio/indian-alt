import re
from bs4 import BeautifulSoup
from typing import Optional, Dict, Any
import logging
from ..models import AIPersonality, AISkill, AnalysisProtocol

logger = logging.getLogger(__name__)

class PromptBuilderService:
    """
    Constructs prompts securely, injects system instructions, loads AIPersonalities, 
    merges AISkills, and processes HTML/Text context.
    """

    @staticmethod
    def clean_html(html_content: str) -> str:
        if not html_content: return ""
        soup = BeautifulSoup(html_content, "html.parser")
        for script_or_style in soup(["script", "style"]):
            script_or_style.decompose()
        text = soup.get_text()
        text = re.sub(r'\n\s*\n', '\n\n', text)
        text = re.sub(r' +', ' ', text)
        return text.strip()

    @staticmethod
    def build_system_instructions(personality: Optional[AIPersonality], skill: Optional[AISkill], stream: bool = False) -> str:
        system_instructions = personality.system_instructions if personality else "You are a PE analyst."
        
        if skill:
            skill_identity = f"# CURRENT TASK: {skill.name.upper().replace('_', ' ')}\n{skill.description}\n\n"
            system_instructions = skill_identity + system_instructions
        
        # Inject Dynamic Protocols
        protocol = AnalysisProtocol.objects.filter(is_active=True).first()
        if protocol and protocol.directives:
            from .forex_service import ForexService
            forex = ForexService()
            live_rate = forex.get_crore_string()
            
            directives_text = "\n### INSTITUTIONAL ANALYSIS DIRECTIVES:\n"
            for d in protocol.directives:
                if any(word in d.lower() for word in ['currency', 'inr', 'crore', '$']):
                    directives_text += f"- {d} (CURRENT LIVE RATE: 1M USD = {live_rate})\n"
                else:
                    directives_text += f"- {d}\n"
            system_instructions += directives_text

        if not stream:
            system_instructions += "\n\nIMPORTANT: Return ONLY a valid JSON object. Do not include any thinking text in the final response."
            
        return system_instructions

    @staticmethod
    def build_user_prompt(template: str, content: str, metadata: Optional[Dict[str, Any]] = None) -> str:
        user_prompt = template
        
        # 1. Replace specific metadata keys
        if metadata:
            for key, value in metadata.items():
                val_str = str(value)
                user_prompt = user_prompt.replace('{{' + key + '}}', val_str)
                user_prompt = user_prompt.replace('{{ ' + key + ' }}', val_str)
                user_prompt = user_prompt.replace('{{  ' + key + '  }}', val_str)
                user_prompt = user_prompt.replace('{{' + key.lower() + '}}', val_str)
                user_prompt = user_prompt.replace('{{ ' + key.lower() + ' }}', val_str)
        
        # 2. Replace primary content
        cleaned_content = PromptBuilderService.clean_html(content)
        # 100k chars is approx 25k-28k tokens, leaving safe room for system instructions 
        # and a full 4k-8k token response within the 32k context window.
        max_chars = 100000 
        if len(cleaned_content) > max_chars:
            cleaned_content = cleaned_content[:60000] + "\n\n[... TRUNCATED DUE TO CONTEXT LIMITS ...]\n\n" + cleaned_content[-40000:]
            
        user_prompt = user_prompt.replace('{{content}}', cleaned_content)
        user_prompt = user_prompt.replace('{{ content }}', cleaned_content)
        user_prompt = user_prompt.replace('{{  content  }}', cleaned_content)
        
        # 3. Fallback
        if cleaned_content not in user_prompt:
            user_prompt = f"{user_prompt}\n\n[USER INQUIRY]:\n{cleaned_content}"
            
        return user_prompt, cleaned_content
