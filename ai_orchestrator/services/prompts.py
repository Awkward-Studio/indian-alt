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

        if skill and skill.name == "deal_extraction":
            system_instructions += (
                "\n\n### DEAL EXTRACTION OUTPUT CONTRACT:\n"
                "- Return exactly one JSON object and nothing else.\n"
                "- Do not wrap the JSON in markdown fences or <json> tags.\n"
                "- Do not repeat keys or restart the JSON object.\n"
                "- Keep `analyst_report` concise and bounded; prefer summary quality over length.\n"
                "- `funding_ask` must be a string in INR Cr, not a number.\n"
                "- `themes`, `ambiguous_points`, and `sources_cited` must be JSON arrays of strings.\n"
            )
        elif skill and skill.name == "document_evidence_extraction":
            system_instructions += (
                "\n\n### DOCUMENT EVIDENCE OUTPUT CONTRACT:\n"
                "- Return exactly one JSON object and nothing else.\n"
                "- `document_summary`, `normalized_text`, and `reasoning` must be strings.\n"
                "- `claims`, `risks`, `open_questions`, `citations`, and `quality_flags` must be arrays of strings.\n"
                "- `metrics`, `tables_summary`, `contacts_found`, and `source_map` must be valid JSON values.\n"
            )
        elif skill and skill.name == "deal_synthesis":
            system_instructions += (
                "\n\n### DEAL SYNTHESIS OUTPUT CONTRACT:\n"
                "- Return exactly one JSON object and nothing else.\n"
                "- `analyst_report` must be a string with citations.\n"
                "- `document_evidence`, `cross_document_conflicts`, and `missing_information_requests` must be arrays.\n"
                "- `metadata` must include `ambiguous_points`, `sources_cited`, `documents_analyzed`, `analysis_input_files`, and `failed_files`.\n"
                "- Preserve supporting citations from the supplied evidence where possible.\n"
            )
        elif skill and skill.name == "vdr_incremental_analysis":
            system_instructions += (
                "\n\n### INCREMENTAL ANALYSIS OUTPUT CONTRACT:\n"
                "- Return exactly one JSON object and nothing else.\n"
                "- The JSON must contain `analyst_report` as a string.\n"
                "- `document_evidence`, `cross_document_conflicts`, and `missing_information_requests` must be arrays.\n"
                "- Focus only on newly supplied documents; do not rewrite the full prior report.\n"
            )

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
        # 180k chars keeps recall-oriented universal chat contexts intact while still
        # fitting within the configured large-context inference budget.
        max_chars = 180000
        if len(cleaned_content) > max_chars:
            head_chars = 100000
            tail_chars = 60000
            cleaned_content = (
                cleaned_content[:head_chars]
                + "\n\n[... TRUNCATED DUE TO CONTEXT LIMITS ...]\n\n"
                + cleaned_content[-tail_chars:]
            )
            
        user_prompt = user_prompt.replace('{{content}}', cleaned_content)
        user_prompt = user_prompt.replace('{{ content }}', cleaned_content)
        user_prompt = user_prompt.replace('{{  content  }}', cleaned_content)
        
        # 3. Fallback
        if cleaned_content not in user_prompt:
            user_prompt = f"{user_prompt}\n\n[USER INQUIRY]:\n{cleaned_content}"
            
        return user_prompt, cleaned_content
