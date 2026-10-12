"""Supply persisted competitor research to report and rewrite retrieval."""
import json
from urllib.parse import urlsplit

from ai_orchestrator.models import DocumentChunk
from ai_orchestrator.services.token_budget import estimate_message_tokens

COMPETITOR_SECTIONS = {'Executive Summary', 'Industry Overview',
                      'Transaction / Trading Multiples', 'Investment Rationale', 'Risk Factors'}


def saved_competitor_blocks(deal, title, *, max_tokens):
    if title not in COMPETITOR_SECTIONS:
        return []
    blocks = []
    used = 0

    def add(source_id, name, text, url, location):
        nonlocal used
        if not text or used + estimate_message_tokens(text) + 160 > max_tokens:
            return
        used += estimate_message_tokens(text) + 160
        blocks.append({'source_id': source_id, 'title': name, 'text': text,
                       'url': url, 'location': location})

    for index, candidate in enumerate(getattr(deal, 'competitor_candidates', []) or []):
        if not isinstance(candidate, dict):
            continue
        name = candidate.get('name') or candidate.get('company_name')
        urls = [str(url) for url in candidate.get('evidence_urls', [])
                if urlsplit(str(url)).scheme in {'https', 'http'} and urlsplit(str(url)).netloc]
        if not name or not urls:
            continue
        add(f'competitor-search:{deal.id}:{index}', f'Competitor search: {name}',
            'Saved web-search candidate, not a confirmed internal prior deal. '
            'Use only the supplied research claims; missing financials remain Not provided.\n'
            + json.dumps(candidate, ensure_ascii=False, default=str), urls[0], 'Saved competitor search')

    relations = getattr(deal, 'vi_relations', None)
    if relations is not None:
        for relation in relations.filter(relation_type='competitor').select_related('company_profile'):
            profile = relation.company_profile
            source_ids = [f'vi_{profile.id}', f'screener_{profile.id}']
            for chunk in DocumentChunk.objects.filter(deal=deal, source_type='extracted_source',
                                                      source_id__in=source_ids).order_by('created_at', 'id'):
                add(str(chunk.source_id), f'Saved competitor profile: {profile.name}',
                    'Selected competitor profile. Keep its figures separate from the target company.\n'
                    + str(chunk.content or ''), profile.website or '', f'Profile chunk {chunk.id}')
    return blocks
