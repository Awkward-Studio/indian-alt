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
    def resolve_thread_to_deal(email_id: str) -> Tuple[Deal, bool]:
        """
        Takes an email, finds its thread, and resolves it to a Deal.
        Creates a new Deal/Bank/Contact if necessary.
        Returns (Deal, created)
        """
        try:
            root_email = Email.objects.get(id=email_id)
        except Email.DoesNotExist:
            raise ValueError(f"Email {email_id} not found")

        # 1. Get entire thread
        thread = Email.objects.filter(conversation_id=root_email.conversation_id).order_by('created_at')
        
        # 2. Check for existing link
        existing_deal = thread.filter(deal__isnull=False).values_list('deal', flat=True).first()
        if existing_deal:
            deal = Deal.objects.get(id=existing_deal)
            # Ensure all emails in thread are linked
            thread.filter(deal__isnull=True).update(deal=deal)
            return deal, False

        # 3. Autonomous Extraction via vLLM
        ai_service = AIProcessorService()
        thread_context = "\n\n".join([
            f"FROM: {e.from_email}\nSUBJECT: {e.subject}\nBODY: {e.body_preview}"
            for e in thread
        ])

        routing_prompt = f"Analyze this thread:\n{thread_context[:10000]}"

        result = ai_service.process_content(
            content=routing_prompt,
            skill_name="deal_routing",
            source_type="email_thread"
        )

        extraction = result.get('parsed_json', {}) if isinstance(result, dict) else {}
        company_name = extraction.get('company_name')
        bank_name = extraction.get('bank_name')
        banker_name = extraction.get('banker_name')
        banker_email = extraction.get('banker_email')

        if not company_name:
            # Fallback to subject if extraction fails
            company_name = root_email.subject or f"New Deal from {root_email.from_email}"

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
                    bank_name=bank.name if bank else bank_name,
                    primary_contact_name=contact.name if contact else banker_name,
                    primary_contact=contact
                )
                created = True
            
            # Link entire thread to deal
            thread.update(deal=deal)
            
            # Update deal with any new banker info if not already set
            if contact and not deal.primary_contact:
                deal.primary_contact = contact
                deal.save(update_fields=['primary_contact'])

            return deal, created
