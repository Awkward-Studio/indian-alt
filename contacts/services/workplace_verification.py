from __future__ import annotations

import json
import re
from urllib.parse import urlparse

from django.db import transaction
from django.utils import timezone

from ai_orchestrator.models import AIAuditLog
from ai_orchestrator.services.prompt_catalog import PromptCatalogService
from ai_orchestrator.services.search_provider import SearXNGProviderService
from banks.models import Bank
from contacts.models import Contact, WorkplaceVerificationSuggestion


BLOCKED_DOMAINS = {
    'facebook.com',
    'instagram.com',
    'linkedin.com',
    'twitter.com',
    'x.com',
}
DESIGNATIONS = (
    'Managing Director',
    'Executive Director',
    'Vice President',
    'Senior Associate',
    'Associate Director',
    'Director',
    'Partner',
    'Principal',
    'Associate',
)


class WorkplaceVerificationService:
    def __init__(self, search_service=None):
        self.search_service = search_service or SearXNGProviderService()

    @transaction.atomic
    def verify(self, *, contact: Contact, requested_by):
        name = (contact.name or '').strip()
        if not name:
            raise ValueError('A banker name is required before workplace verification.')

        current_bank = (contact.bank.name if contact.bank else '').strip()
        query_context = f' {current_bank}' if current_bank else ' investment banker'
        queries = [
            f'"{name}"{query_context} current role',
            f'"{name}" banker current employer designation',
        ]
        results = self.search_service.search_many(
            queries,
            results_per_query=5,
            max_results=10,
        )
        eligible = [
            result for result in results
            if self._is_permitted_result(result.get('url'))
        ]
        proposal = self._best_proposal(contact=contact, results=eligible)

        audit = AIAuditLog.objects.create(
            source_type='banker_workplace_verification',
            source_id=str(contact.id),
            context_label=f'Workplace verification: {name}',
            requested_by=requested_by,
            model_provider='local_search',
            model_used='searxng',
            system_prompt=PromptCatalogService.get('workplace_verification_policy'),
            user_prompt='\n'.join(queries),
            raw_response=json.dumps(eligible, ensure_ascii=False, default=str),
            parsed_json=proposal or {'status': 'NO_SUGGESTION'},
            source_metadata={
                'contact_id': str(contact.id),
                'queries': queries,
                'result_count': len(eligible),
                'blocked_domains': sorted(BLOCKED_DOMAINS),
                'human_review_required': True,
            },
            is_success=True,
            status='COMPLETED',
            completed_at=timezone.now(),
        )

        if not proposal:
            return None, audit

        WorkplaceVerificationSuggestion.objects.filter(
            contact=contact,
            status=WorkplaceVerificationSuggestion.Status.PENDING,
        ).update(status=WorkplaceVerificationSuggestion.Status.SUPERSEDED)

        suggestion = WorkplaceVerificationSuggestion.objects.create(
            contact=contact,
            old_bank_name=current_bank,
            old_designation=(contact.designation or '').strip(),
            proposed_bank_name=proposal['bank_name'],
            proposed_designation=proposal['designation'],
            source_url=proposal['source_url'],
            source_title=proposal['source_title'],
            source_snippet=proposal['source_snippet'],
            source_domain=proposal['source_domain'],
            search_query=proposal['search_query'],
            confidence=proposal['confidence'],
            retrieved_at=timezone.now(),
            requested_by=requested_by,
            audit_log=audit,
        )
        audit.parsed_json = {
            **proposal,
            'suggestion_id': str(suggestion.id),
            'status': suggestion.status,
        }
        audit.source_metadata = {
            **(audit.source_metadata or {}),
            'suggestion_id': str(suggestion.id),
        }
        audit.save(update_fields=['parsed_json', 'source_metadata'])
        return suggestion, audit

    @staticmethod
    def _is_permitted_result(url) -> bool:
        parsed = urlparse(str(url or '').strip())
        if parsed.scheme not in {'http', 'https'} or not parsed.netloc:
            return False
        hostname = (parsed.hostname or '').lower()
        return not any(
            hostname == domain or hostname.endswith(f'.{domain}')
            for domain in BLOCKED_DOMAINS
        )

    def _best_proposal(self, *, contact, results):
        known_banks = list(
            Bank.objects.exclude(name__isnull=True)
            .exclude(name='')
            .values('name', 'website_domain')
        )
        candidates = []
        for result in results:
            title = str(result.get('title') or '').strip()
            snippet = str(result.get('snippet') or '').strip()
            text = f'{title} {snippet}'
            bank_name, bank_domain = self._extract_bank(text, known_banks)
            designation = self._extract_designation(text)
            if not bank_name and not designation:
                continue
            source_url = str(result.get('url') or '').strip()
            source_domain = (urlparse(source_url).hostname or '').lower()
            confidence = 0.58
            if bank_domain and (
                source_domain == bank_domain
                or source_domain.endswith(f'.{bank_domain}')
            ):
                confidence = 0.92
            elif bank_name and designation:
                confidence = 0.78
            elif source_domain.endswith(('.org', '.gov.in', '.co.in')):
                confidence = 0.68
            candidates.append({
                'bank_name': bank_name,
                'designation': designation,
                'source_url': source_url,
                'source_title': title[:500],
                'source_snippet': snippet[:1500],
                'source_domain': source_domain[:255],
                'search_query': str(result.get('query') or '').strip(),
                'confidence': confidence,
            })
        if not candidates:
            return None
        return max(candidates, key=lambda candidate: candidate['confidence'])

    @staticmethod
    def _extract_bank(text, known_banks):
        folded = text.casefold()
        matches = [
            bank for bank in known_banks
            if bank['name'] and bank['name'].casefold() in folded
        ]
        if matches:
            match = max(matches, key=lambda bank: len(bank['name']))
            return match['name'], (match['website_domain'] or '').lower()

        pattern = re.compile(
            r'\b(?:at|with|joins?)\s+([A-Z][A-Za-z0-9&.\'-]*(?:\s+[A-Z][A-Za-z0-9&.\'-]*){0,5})'
        )
        match = pattern.search(text)
        if not match:
            return '', ''
        candidate = match.group(1).strip(' .,-')
        if candidate.casefold() in {'the', 'a', 'an'}:
            return '', ''
        return candidate[:200], ''

    @staticmethod
    def _extract_designation(text):
        folded = text.casefold()
        for designation in DESIGNATIONS:
            if designation.casefold() in folded:
                return designation
        if re.search(r'\bvp\b', text, flags=re.IGNORECASE):
            return 'Vice President'
        if re.search(r'\bmd\b', text, flags=re.IGNORECASE):
            return 'Managing Director'
        return ''


@transaction.atomic
def review_workplace_suggestion(
    *,
    suggestion: WorkplaceVerificationSuggestion,
    reviewer,
    decision: str,
    accepted_fields: list[str],
    comment: str = '',
):
    # Lock only the suggestion row. PostgreSQL rejects FOR UPDATE across the
    # nullable bank/audit outer joins that select_related would introduce.
    suggestion = WorkplaceVerificationSuggestion.objects.select_for_update().get(
        id=suggestion.id
    )
    if suggestion.status != WorkplaceVerificationSuggestion.Status.PENDING:
        raise ValueError('This workplace suggestion has already been reviewed.')

    decision = decision.upper()
    allowed_fields = {'bank', 'designation'}
    accepted_fields = list(dict.fromkeys(accepted_fields or []))
    if not set(accepted_fields).issubset(allowed_fields):
        raise ValueError('accepted_fields may contain only bank and designation.')
    if decision == 'ACCEPT' and not accepted_fields:
        raise ValueError('Select at least one field to accept.')
    if decision not in {'ACCEPT', 'REJECT'}:
        raise ValueError('decision must be ACCEPT or REJECT.')

    contact = suggestion.contact
    changed_fields = []
    if decision == 'ACCEPT':
        if 'bank' in accepted_fields and suggestion.proposed_bank_name:
            bank = Bank.objects.filter(
                name__iexact=suggestion.proposed_bank_name
            ).first()
            if bank is None:
                bank = Bank.objects.create(name=suggestion.proposed_bank_name)
            if contact.bank_id != bank.id:
                contact.bank = bank
                changed_fields.append('bank')
        if (
            'designation' in accepted_fields
            and suggestion.proposed_designation
            and contact.designation != suggestion.proposed_designation
        ):
            contact.designation = suggestion.proposed_designation
            changed_fields.append('designation')
        if changed_fields:
            contact.save(update_fields=changed_fields)
            if 'bank' in changed_fields:
                from deals.services.contact_linking import sync_primary_contact_bank
                sync_primary_contact_bank(contact)
        suggestion.status = WorkplaceVerificationSuggestion.Status.ACCEPTED
    else:
        suggestion.status = WorkplaceVerificationSuggestion.Status.REJECTED
        accepted_fields = []

    suggestion.accepted_fields = accepted_fields
    suggestion.reviewer_comment = (comment or '').strip()
    suggestion.reviewed_by = reviewer
    suggestion.reviewed_at = timezone.now()
    suggestion.save(update_fields=[
        'status', 'accepted_fields', 'reviewer_comment', 'reviewed_by',
        'reviewed_at',
    ])

    if suggestion.audit_log:
        suggestion.audit_log.parsed_json = {
            **(suggestion.audit_log.parsed_json or {}),
            'status': suggestion.status,
            'accepted_fields': accepted_fields,
            'reviewed_by_id': reviewer.id,
            'reviewed_at': suggestion.reviewed_at.isoformat(),
        }
        suggestion.audit_log.source_metadata = {
            **(suggestion.audit_log.source_metadata or {}),
            'review': {
                'decision': decision,
                'accepted_fields': accepted_fields,
                'reviewed_by_id': reviewer.id,
                'reviewed_at': suggestion.reviewed_at.isoformat(),
            },
        }
        suggestion.audit_log.save(update_fields=['parsed_json', 'source_metadata'])
    return suggestion
