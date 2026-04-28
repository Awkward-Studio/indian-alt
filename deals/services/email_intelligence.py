import logging
from typing import Optional, Tuple
from django.db import transaction
from microsoft.models import Email
from banks.models import Bank
from contacts.models import Contact
from deals.models import Deal
from ai_orchestrator.services.ai_processor import AIProcessorService

logger = logging.getLogger(__name__)

class EmailIntelligenceService:
    """
    Autonomous routing and intelligence service for email threads.
    Resolves threads to Deals, Banks, and Bankers.
    """

    @staticmethod
    def propose_thread_routing(email_id: str) -> dict:
        """
        Analyzes an email thread and returns proposed deal metadata (routing).
        Bypasses Audit Logging to prevent frontend dialog race conditions.
        """
        try:
            root_email = Email.objects.get(id=email_id)
        except Email.DoesNotExist:
            raise ValueError(f"Email {email_id} not found")

        # 1. Get entire thread
        thread = Email.objects.filter(conversation_id=root_email.conversation_id).order_by('created_at')
        
        # 2. Extract Context
        import re
        def clean_html(raw_html):
            if not raw_html: return ""
            # Remove style and script tags
            clean = re.sub(r'<(style|script)[^>]*>.*?</\1>', '', raw_html, flags=re.DOTALL | re.IGNORECASE)
            # Remove all other tags
            clean = re.sub(r'<[^>]+>', ' ', clean)
            # Remove extra whitespace
            clean = re.sub(r'\s+', ' ', clean).strip()
            return clean

        thread_context = "\n\n".join([
            f"### MESSAGE FROM: {e.from_email} | SUBJECT: {e.subject}\n{clean_html(e.body_preview or e.body_text or '')}"
            for e in thread
        ]).strip()

        if not thread_context:
            logger.warning(f"No thread context found for email {email_id}")
            return {"company_name": root_email.subject or "Unknown Deal"}

        routing_prompt = f"Analyze this thread and propose deal routing metadata:\n{thread_context[:10000]}"

        # 3. Direct Provider Call (No Audit Log)
        from ai_orchestrator.services.llm_providers import VLLMProviderService
        from ai_orchestrator.services.parsers import ResponseParserService
        from ai_orchestrator.models import AISkill, AIPersonality
        from ai_orchestrator.services.runtime import AIRuntimeService
        
        provider = VLLMProviderService()
        skill = AISkill.objects.filter(name="deal_routing").first()
        personality = AIPersonality.objects.filter(is_default=True).first()
        active_model = AIRuntimeService.get_text_model(personality)
        
        payload = {
            "model": active_model,
            "messages": [
                {"role": "system", "content": skill.system_template if skill else "Return exactly one valid JSON object."},
                {"role": "user", "content": routing_prompt}
            ],
            "temperature": 0.0,
            "chat_template_kwargs": {"enable_thinking": False}, # Disable thinking for speed
            "response_format": {"type": "json_object"}
        }

        try:
            data = provider.execute_standard(payload)
            raw_response = data.get("response") or ""
            
            # Robust extraction of JSON from response (handling thinking blocks)
            parsed_json, success, _, _ = ResponseParserService.parse_standard_response(
                raw_response, "", is_extraction_skill=True
            )
            extraction = parsed_json if success else {}
        except Exception as e:
            logger.error(f"Routing extraction failed: {e}")
            extraction = {}
        
        # Ensure we have a company name fallback
        if not extraction.get('company_name'):
            extraction['company_name'] = root_email.subject or f"New Deal from {root_email.from_email}"
            
        return extraction

    @staticmethod
    def create_deal_from_intelligence(email_id: str, intelligence: dict) -> Tuple[Deal, bool]:
        """
        Actually creates/resolves the Deal, Bank, and Contact records.
        Used after user confirmation.
        """
        root_email = Email.objects.get(id=email_id)
        thread = Email.objects.filter(conversation_id=root_email.conversation_id)
        
        company_name = intelligence.get('company_name')
        bank_name = intelligence.get('bank_name')
        banker_name = intelligence.get('banker_name')
        banker_email = intelligence.get('banker_email')

        with transaction.atomic():
            # Resolve Bank
            bank = None
            if bank_name:
                bank, _ = Bank.objects.get_or_create(
                    name__iexact=bank_name,
                    defaults={'name': bank_name}
                )

            # Resolve Contact (Banker)
            contact = None
            if banker_email or banker_name:
                contact_query = Contact.objects.filter(email__iexact=banker_email) if banker_email else Contact.objects.filter(name__iexact=banker_name)
                contact = contact_query.first()
                if not contact:
                    contact = Contact.objects.create(
                        name=banker_name or banker_email,
                        email=banker_email,
                        bank=bank
                    )

            # Resolve/Create Deal
            deal = Deal.objects.filter(title__iexact=company_name).first()
            created = False
            if not deal:
                deal = Deal.objects.create(
                    title=company_name,
                    bank=bank,
                    primary_contact=contact,
                    source_email_id=str(root_email.id)
                )
                created = True
            
            # Link entire thread to deal
            thread.update(deal=deal)
            
            # Update deal with any new banker info if not already set
            if contact and not deal.primary_contact:
                deal.primary_contact = contact
                deal.save(update_fields=['primary_contact'])

            return deal, created

    @staticmethod
    def resolve_thread_to_deal(email_id: str) -> Tuple[Deal, bool]:
        """
        LEGACY/AUTONOMOUS: Immediately resolves and creates.
        """
        intelligence = EmailIntelligenceService.propose_thread_routing(email_id)
        return EmailIntelligenceService.create_deal_from_intelligence(email_id, intelligence)
