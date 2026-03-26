import json
import logging
from django.core.exceptions import ObjectDoesNotExist
from deals.models import Deal, DealDocument, DocumentType

logger = logging.getLogger(__name__)

class DealCreationService:
    """
    Domain Service for handling the complex side-effects of Deal Creation.
    Responsible for contact discovery, bank linkage, email threading, 
    and initial document extraction.
    """
    
    @staticmethod
    def process_deal_creation(deal: Deal, validated_data: dict, request_user=None):
        """
        Orchestrates the post-save creation hooks for a new Deal.
        """
        source_email_id = validated_data.get('source_email_id')
        contact_discovery = validated_data.get('contact_discovery')
        analysis_json = validated_data.get('analysis_json')
        
        # Parse strings to dicts if necessary
        if isinstance(contact_discovery, str):
            try: contact_discovery = json.loads(contact_discovery)
            except: contact_discovery = None
            
        if isinstance(analysis_json, str):
            try: analysis_json = json.loads(analysis_json)
            except: analysis_json = None
            
        # 1. Handle Ambiguities mapping
        DealCreationService._map_ambiguities(deal, analysis_json)
        
        # 2. Handle Contact & Bank Discovery
        DealCreationService._discover_and_link_contacts(deal, contact_discovery)
        
        # 3. Handle Email Linking & Threading
        DealCreationService._link_email_thread(deal, source_email_id, request_user)

    @staticmethod
    def _get_analysis_model_data(analysis_json: dict | None) -> dict:
        if not isinstance(analysis_json, dict):
            return {}
        model_data = analysis_json.get('deal_model_data')
        return model_data if isinstance(model_data, dict) else {}

    @staticmethod
    def _normalize_string_list(value) -> list[str]:
        if not isinstance(value, list):
            return []
        return [item.strip() for item in value if isinstance(item, str) and item.strip()]

    @staticmethod
    def apply_analysis_to_deal(deal: Deal, analysis_json: dict | None, *, overwrite: bool = False, overwrite_themes: bool = False):
        if not isinstance(analysis_json, dict):
            return

        model_data = DealCreationService._get_analysis_model_data(analysis_json)
        analyst_report = analysis_json.get('analyst_report')
        changed_fields = []

        field_mapping = {
            'title': 'title',
            'industry': 'industry',
            'sector': 'sector',
            'funding_ask': 'funding_ask',
            'funding_ask_for': 'funding_ask_for',
            'priority': 'priority',
            'city': 'city',
            'state': 'state',
            'country': 'country',
        }

        for analysis_key, deal_field in field_mapping.items():
            value = model_data.get(analysis_key)
            if not isinstance(value, str):
                continue

            normalized_value = value.strip()
            if not normalized_value:
                continue

            current_value = getattr(deal, deal_field)
            if overwrite or not current_value:
                if current_value != normalized_value:
                    setattr(deal, deal_field, normalized_value)
                    changed_fields.append(deal_field)

        if isinstance(analyst_report, str):
            normalized_report = analyst_report.strip()
            if normalized_report and (overwrite or not deal.deal_summary):
                if deal.deal_summary != normalized_report:
                    deal.deal_summary = normalized_report
                    changed_fields.append('deal_summary')

        themes = DealCreationService._normalize_string_list(model_data.get('themes'))
        if themes and (overwrite_themes or not deal.themes):
            if deal.themes != themes:
                deal.themes = themes
                changed_fields.append('themes')

        if changed_fields:
            deal.save(update_fields=list(dict.fromkeys(changed_fields)))

    @staticmethod
    def _map_ambiguities(deal: Deal, analysis_json: dict):
        if analysis_json and 'metadata' in analysis_json:
            try:
                from deals.models import DealAnalysis
                DealCreationService.apply_analysis_to_deal(deal, analysis_json)
                ambiguities = analysis_json['metadata'].get('ambiguous_points', [])
                thinking = analysis_json.get('thinking', '')
                
                # Create a DealAnalysis record
                DealAnalysis.objects.create(
                    deal=deal,
                    version=1,
                    thinking=thinking,
                    ambiguities=ambiguities,
                    analysis_json=analysis_json
                )
                print(f"[ANALYSIS] Created initial DealAnalysis record for {deal.title}")
            except Exception as e:
                logger.error(f"Error mapping ambiguities: {str(e)}")

    @staticmethod
    def _discover_and_link_contacts(deal: Deal, contact_discovery: dict):
        if not contact_discovery:
            return
            
        try:
            from banks.models import Bank
            from contacts.models import Contact
            
            firm_name = contact_discovery.get('firm_name')
            firm_domain = contact_discovery.get('firm_domain')
            banker_name = contact_discovery.get('name')
            
            bank = None
            if firm_domain:
                bank = Bank.objects.filter(website_domain__iexact=firm_domain).first()
            if not bank and firm_name:
                bank = Bank.objects.filter(name__icontains=firm_name).first()
            
            # Create Bank if not found
            if not bank and firm_name:
                bank = Bank.objects.create(name=firm_name, website_domain=firm_domain)
            
            if banker_name:
                # Find or create contact
                contact, created = Contact.objects.get_or_create(
                    name=banker_name,
                    bank=bank,
                    defaults={
                        'designation': contact_discovery.get('designation'),
                        'linkedin_url': contact_discovery.get('linkedin')
                    }
                )
                deal.primary_contact = contact
                if bank: 
                    deal.bank = bank
                
                # Increment source count for influencer tracking
                contact.source_count += 1
                contact.save(update_fields=['source_count'])
                deal.save(update_fields=['primary_contact', 'bank'])
                print(f"[DISCOVERY] Linked {deal.title} to {banker_name} ({firm_name})")
        except Exception as e:
            logger.error(f"Discovery error: {str(e)}")

    @staticmethod
    def _link_email_thread(deal: Deal, source_email_id: str, request_user=None):
        if not source_email_id:
            return
            
        try:
            from microsoft.models import Email
            from ai_orchestrator.services.embedding_processor import EmbeddingService
            
            source_email = Email.objects.filter(id=source_email_id).first()
            if not source_email:
                return
                
            source_email.deal = deal
            source_email.is_processed = True
            source_email.save(update_fields=['deal', 'is_processed'])
            
            # LINK THE WHOLE THREAD (All replies/forwards in this conversation)
            if source_email.conversation_id:
                Email.objects.filter(
                    conversation_id=source_email.conversation_id
                ).update(deal=deal)
                print(f"[THREADING] Linked entire thread {source_email.conversation_id} to deal")

            # Create DealDocument records for attachments
            if source_email.attachments:
                DealCreationService._extract_documents_from_email(deal, source_email, request_user)

            # Copy extracted text to deal if empty
            if not deal.extracted_text and source_email.extracted_text:
                deal.extracted_text = source_email.extracted_text
                deal.save(update_fields=['extracted_text'])
            
            # Asynchronous vectorization
            try:
                embed_service = EmbeddingService()
                embed_service.vectorize_deal(deal)
                embed_service.vectorize_email(source_email)
            except Exception as e:
                logger.error(f"Vectorization failed: {str(e)}")
        except Exception as e:
            logger.error(f"Email linking failed: {str(e)}")

    @staticmethod
    def _extract_documents_from_email(deal: Deal, source_email, request_user=None):
        for att in source_email.attachments:
            # Avoid duplicates
            if not DealDocument.objects.filter(deal=deal, title=att.get('name')).exists():
                # Determine type from filename
                name = att.get('name', '').lower()
                doc_type = DocumentType.OTHER
                if 'financial' in name or 'mis' in name or 'model' in name: 
                    doc_type = DocumentType.FINANCIALS
                elif 'legal' in name or 'sha' in name or 'ssa' in name: 
                    doc_type = DocumentType.LEGAL
                elif 'teaser' in name or 'deck' in name or 'pitch' in name: 
                    doc_type = DocumentType.PITCH_DECK
                
                user_profile = request_user.profile if (request_user and hasattr(request_user, 'profile')) else None
                DealDocument.objects.create(
                    deal=deal,
                    title=att.get('name'),
                    document_type=doc_type,
                    onedrive_id=att.get('id'),
                    uploaded_by=user_profile
                )
                print(f"[DOCUMENT] Created DealDocument artifact: {att.get('name')}")
