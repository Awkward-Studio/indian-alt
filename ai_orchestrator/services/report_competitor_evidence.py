"""Supply persisted competitor research to report and rewrite retrieval."""
import json
from datetime import datetime, timezone
from urllib.parse import urlsplit

from ai_orchestrator.models import DocumentChunk
from ai_orchestrator.services.token_budget import estimate_message_tokens

COMPETITOR_SECTIONS = {'Executive Summary', 'Industry Overview',
                      'Transaction / Trading Multiples', 'Investment Rationale', 'Risk Factors'}


def saved_competitor_blocks(deal, title, *, max_tokens):
    if title is not None and title not in COMPETITOR_SECTIONS:
        return []
    blocks = []
    def add(source_id, name, text, url, location, available_at=None):
        if text:
            blocks.append({'source_id': source_id, 'title': name, 'text': text,
                           'url': url, 'location': location, 'available_at': available_at})

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
            + json.dumps(candidate, ensure_ascii=False, default=str), urls[0], 'Saved competitor search',
            candidate.get('saved_at') or candidate.get('researched_at'))

    relations = getattr(deal, 'vi_relations', None)
    if relations is not None:
        for relation in relations.filter(relation_type='competitor').select_related('company_profile'):
            profile = relation.company_profile
            source_ids = [f'vi_{profile.id}', f'screener_{profile.id}']
            chunks = DocumentChunk.objects.filter(deal=deal, source_type='extracted_source', source_id__in=source_ids)
            chunks = chunks.order_by('-created_at', '-id')[:12] if title is None else chunks.order_by('created_at', 'id')
            for chunk in chunks:
                add(str(chunk.source_id), f'Saved competitor profile: {profile.name}',
                    'Selected competitor profile. Keep its figures separate from the target company.\n'
                    + str(chunk.content or ''), profile.website or '', f'Profile chunk {chunk.id}',
                    max(chunk.created_at, relation.created_at))

    if title is None:
        def recency(indexed):
            index, block = indexed
            value = block['available_at']
            try:
                date = datetime.fromisoformat(str(value)) if value else None
            except ValueError:
                date = None
            if date and date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
            return (date.timestamp() if date else float('-inf'), index)

        blocks = [block for _, block in sorted(enumerate(blocks), key=recency, reverse=True)]

    selected = []
    remaining = max_tokens
    for block in blocks:
        if title is None:
            block['text'] = block['text'][:4800]
        cost = estimate_message_tokens(block['text']) + 160
        if cost <= remaining:
            selected.append(block)
            remaining -= cost
    return selected
