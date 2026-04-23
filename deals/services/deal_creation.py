import json
import logging
from copy import deepcopy
from django.core.exceptions import ObjectDoesNotExist
from deals.models import AnalysisKind, Deal, DealDocument, DocumentType

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
    def _merge_string_lists(*values) -> list[str]:
        seen = set()
        merged = []
        for value in values:
            for item in DealCreationService._normalize_string_list(value):
                if item not in seen:
                    seen.add(item)
                    merged.append(item)
        return merged

    @staticmethod
    def _merge_reports(previous_report: str | None, next_report: str | None, analysis_kind: str) -> str:
        previous = (previous_report or "").strip()
        current = (next_report or "").strip()
        if not previous:
            return current
        if not current or current in previous:
            return previous
        if analysis_kind == AnalysisKind.INITIAL:
            return current
        return f"{previous}\n\n--- Supplemental Update ---\n\n{current}"

    @staticmethod
    def build_canonical_snapshot(
        analysis_json: dict | None,
        *,
        previous_snapshot: dict | None = None,
        analysis_kind: str = AnalysisKind.INITIAL,
    ) -> dict:
        previous_snapshot = previous_snapshot if isinstance(previous_snapshot, dict) else {}
        current_payload = analysis_json if isinstance(analysis_json, dict) else {}

        previous_model_data = previous_snapshot.get("deal_model_data") if isinstance(previous_snapshot.get("deal_model_data"), dict) else {}
        next_model_data = DealCreationService._get_analysis_model_data(current_payload)
        merged_model_data = dict(previous_model_data)
        for key, value in next_model_data.items():
            if isinstance(value, str) and value.strip():
                merged_model_data[key] = value.strip()
            elif isinstance(value, list) and value:
                merged_model_data[key] = value

        previous_meta = previous_snapshot.get("metadata") if isinstance(previous_snapshot.get("metadata"), dict) else {}
        next_meta = current_payload.get("metadata") if isinstance(current_payload.get("metadata"), dict) else {}

        return {
            "deal_model_data": merged_model_data,
            "analyst_report": DealCreationService._merge_reports(
                previous_snapshot.get("analyst_report"),
                current_payload.get("analyst_report"),
                analysis_kind,
            ),
            "metadata": {
                "ambiguous_points": DealCreationService._merge_string_lists(
                    previous_meta.get("ambiguous_points"),
                    next_meta.get("ambiguous_points"),
                ),
            },
            "document_evidence": current_payload.get("document_evidence", previous_snapshot.get("document_evidence", [])),
            "cross_document_conflicts": current_payload.get("cross_document_conflicts", previous_snapshot.get("cross_document_conflicts", [])),
            "missing_information_requests": current_payload.get("missing_information_requests", previous_snapshot.get("missing_information_requests", [])),
        }

    @staticmethod
    def normalize_analysis_payload(
        analysis_json: dict | None,
        *,
        previous_snapshot: dict | None = None,
        analysis_kind: str = AnalysisKind.INITIAL,
        documents_analyzed: list[str] | None = None,
        analysis_input_files: list[dict] | None = None,
        failed_files: list[dict] | None = None,
    ) -> dict:
        normalized = deepcopy(analysis_json) if isinstance(analysis_json, dict) else {}
        normalized.setdefault("deal_model_data", {})
        normalized.setdefault("metadata", {})
        normalized.setdefault("analyst_report", "")
        normalized.setdefault("document_evidence", [])
        normalized.setdefault("cross_document_conflicts", [])
        normalized.setdefault("missing_information_requests", [])

        metadata = normalized["metadata"] if isinstance(normalized.get("metadata"), dict) else {}
        metadata["ambiguous_points"] = DealCreationService._normalize_string_list(metadata.get("ambiguous_points"))
        metadata["documents_analyzed"] = DealCreationService._normalize_string_list(
            documents_analyzed if documents_analyzed is not None else metadata.get("documents_analyzed")
        )
        metadata["analysis_input_files"] = analysis_input_files if isinstance(analysis_input_files, list) else list(metadata.get("analysis_input_files") or [])
        metadata["failed_files"] = failed_files if isinstance(failed_files, list) else list(metadata.get("failed_files") or [])
        metadata["sources_cited"] = DealCreationService._normalize_string_list(metadata.get("sources_cited"))
        normalized["metadata"] = metadata
        normalized["document_evidence"] = normalized["document_evidence"] if isinstance(normalized.get("document_evidence"), list) else []
        normalized["cross_document_conflicts"] = normalized["cross_document_conflicts"] if isinstance(normalized.get("cross_document_conflicts"), list) else []
        normalized["missing_information_requests"] = normalized["missing_information_requests"] if isinstance(normalized.get("missing_information_requests"), list) else []
        normalized["canonical_snapshot"] = DealCreationService.build_canonical_snapshot(
            normalized,
            previous_snapshot=previous_snapshot,
            analysis_kind=analysis_kind,
        )
        return normalized

    @staticmethod
    def apply_analysis_to_deal(deal: Deal, analysis_json: dict | None, *, overwrite: bool = False, overwrite_themes: bool = False):
        if not isinstance(analysis_json, dict):
            return

        model_data = DealCreationService._get_analysis_model_data(analysis_json)
        analyst_report = analysis_json.get('analyst_report')
        changed_fields = []

        field_mapping = {
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
                normalized_analysis = DealCreationService.normalize_analysis_payload(
                    analysis_json,
                    analysis_kind=AnalysisKind.INITIAL,
                )
                DealCreationService.apply_analysis_to_deal(deal, normalized_analysis)
                ambiguities = normalized_analysis['metadata'].get('ambiguous_points', [])
                thinking = analysis_json.get('thinking', '')
                
                # Create a DealAnalysis record
                DealAnalysis.objects.create(
                    deal=deal,
                    version=1,
                    analysis_kind=AnalysisKind.INITIAL,
                    thinking=thinking,
                    ambiguities=ambiguities,
                    analysis_json=normalized_analysis
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
            from deals.services.contact_linking import sync_deal_contact_links
            
            firm_name = contact_discovery.get('firm_name')
            firm_domain = contact_discovery.get('firm_domain')
            banker_name = contact_discovery.get('name')
            banker_email = contact_discovery.get('email')
            
            bank = None
            if firm_domain:
                bank = Bank.objects.filter(website_domain__iexact=firm_domain).first()
            if not bank and firm_name:
                bank = Bank.objects.filter(name__icontains=firm_name).first()
            
            # Create Bank if not found
            if not bank and firm_name:
                bank = Bank.objects.create(name=firm_name, website_domain=firm_domain)
            
            if banker_name:
                # Deduplication logic: try email first, then name + bank
                contact = None
                if banker_email:
                    contact = Contact.objects.filter(email__iexact=banker_email).first()
                
                if not contact:
                    contact = Contact.objects.filter(name__iexact=banker_name, bank=bank).first()
                
                if not contact:
                    contact = Contact.objects.create(
                        name=banker_name,
                        bank=bank,
                        email=banker_email,
                        designation=contact_discovery.get('designation'),
                        linkedin_url=contact_discovery.get('linkedin')
                    )
                else:
                    # Update fields if they were missing
                    updated_fields = []
                    if not contact.email and banker_email:
                        contact.email = banker_email
                        updated_fields.append('email')
                    if not contact.designation and contact_discovery.get('designation'):
                        contact.designation = contact_discovery.get('designation')
                        updated_fields.append('designation')
                    if not contact.linkedin_url and contact_discovery.get('linkedin'):
                        contact.linkedin_url = contact_discovery.get('linkedin')
                        updated_fields.append('linkedin_url')
                    if updated_fields:
                        contact.save(update_fields=updated_fields)

                deal.primary_contact = contact
                if bank: 
                    deal.bank = bank
                
                # Increment source count for influencer tracking
                contact.source_count += 1
                contact.save(update_fields=['source_count'])
                deal.save(update_fields=['primary_contact', 'bank'])
                sync_deal_contact_links(
                    deal,
                    primary_contact=contact,
                    primary_contact_provided=True,
                )
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
