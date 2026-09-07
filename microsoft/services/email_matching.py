"""Shared classification and existing-deal resolution for both email branches."""
import json
import re
from difflib import SequenceMatcher

from django.db.models import Q
from microsoft.models import Email
from .email_contributions import normalize


import logging
logger = logging.getLogger(__name__)


class EmailDecisionService:
    @staticmethod
    def ai(stage, payload, source_id):
        from ai_orchestrator.services.ai_processor import AIProcessorService
        result = AIProcessorService().process_content(
            content=json.dumps(payload, ensure_ascii=False), source_type='email_ingestion', source_id=str(source_id),
            metadata={'pipeline_key': 'email_evidence', 'stage_key': stage,
                      'response_mode': 'json', 'response_format': {'type': 'json_object'},
                      'temperature': 0, 'request_timeout': 90, 'max_tokens': 2000})
        value = result.get('parsed_json', result) if isinstance(result, dict) else {}
        if not isinstance(value, dict) or value.get('error'):
            raise ValueError('Email decision service returned no valid decision.')
        return value

    @classmethod
    def classify(cls, parts, *, source_id, use_ai=True):
        roles = []
        for part in parts:
            text = part.text
            meeting = bool(re.search(r'(?im)^\s*#{0,6}\s*(transcript|meeting notes|meeting summary|summary|action items)\s*:?', text))
            strong = bool(re.search(r'(?im)^\s*#{0,6}\s*(transcript|notes)\s*:?\s*$', text)) and meeting
            if strong:
                roles.append({'type': 'MEETING_NOTE', 'method': 'structure', 'evidence': text[:250]})
            elif meeting:
                if not use_ai:
                    roles.append({'type': 'UNKNOWN', 'method': 'waiting_service', 'evidence': ''})
                    continue
                result = cls.ai('classify', {'text': text}, source_id)
                kind = result.get('type')
                excerpt = result.get('evidence', '')
                if kind not in ('MEETING_NOTE', 'NORMAL_EMAIL') or not isinstance(excerpt, str) or not excerpt or excerpt not in text:
                    raise ValueError('Classification must contain a supported type and an exact source excerpt.')
                roles.append({'type': kind, 'method': 'ai', 'evidence': excerpt})
            else:
                roles.append({'type': 'NORMAL_EMAIL', 'method': 'structure', 'evidence': text[:250]})
        kind = roles[0]['type'] if roles else 'NORMAL_EMAIL'
        # A forwarding wrapper with no business content need not hide its meeting.
        if roles and any(r['type'] == 'MEETING_NOTE' for r in roles) and len(parts[0].text) < 40:
            kind = 'MEETING_NOTE'
        return {'type': kind, 'segment_roles': roles, 'revision': 1,
                'status': 'waiting_service' if any(r['type'] == 'UNKNOWN' for r in roles) else 'completed'}

    @staticmethod
    def candidates(text, deals, *, use_semantic=True, limit=12):
        """
        Primary path: Query the VM embedding service for semantic deal profiles,
        then rerank candidates using the VM cross-encoder reranker.
        Fallback path: Word matching and SequenceMatcher if semantic services are offline or return no hits.
        """
        semantic_candidates = []

        if use_semantic:
            try:
                from ai_orchestrator.services.embedding_processor import EmbeddingService
                service = EmbeddingService()
                if service.is_embedding_available(timeout=2):
                    hits = service.search_deal_profiles(text[:12000], limit=max(limit * 2, 24))
                    permitted_ids = set(deals.filter(id__in=[d.id for d in hits]).values_list('id', flat=True))
                    filtered_hits = [d for d in hits if d.id in permitted_ids]

                    if filtered_hits:
                        reranked_hits = filtered_hits
                        if service.reranker_model and getattr(service, 'reranker', None):
                            try:
                                candidate_docs = []
                                for d in filtered_hits:
                                    profile = getattr(d, 'retrieval_profile', None)
                                    prof_text = str(getattr(profile, 'profile_text', '') or d.deal_summary or '')
                                    candidate_docs.append(f"{d.title}. Sector: {d.sector or ''}. Industry: {d.industry or ''}. {prof_text}"[:1500])

                                rerank_results = service.reranker.rerank(
                                    model=service.reranker_model,
                                    query=text[:4000],
                                    documents=candidate_docs,
                                )
                                if rerank_results:
                                    index_to_score = {
                                        item.get('index'): float(item.get('score', 0))
                                        for item in rerank_results
                                        if item.get('index') is not None
                                    }
                                    reranked_hits = sorted(
                                        filtered_hits,
                                        key=lambda d: index_to_score.get(filtered_hits.index(d), -999.0),
                                        reverse=True,
                                    )
                            except Exception as exc:
                                logger.warning("Reranker failed during candidate ranking: %s", exc)

                        for rank, deal in enumerate(reranked_hits[:limit]):
                            profile = getattr(deal, 'retrieval_profile', None)
                            prof_text = str(getattr(profile, 'profile_text', '') or deal.deal_summary or '')
                            haystack = ' '.join([deal.title, deal.sector or '', deal.industry or '', prof_text])
                            score = max(1.0, 100.0 - (rank * 5.0))
                            semantic_candidates.append({
                                'deal_id': str(deal.id),
                                'title': deal.title,
                                'score': score,
                                'context': haystack[:3000],
                                'evidence': 'VM embedding + reranking',
                            })
            except Exception as exc:
                logger.warning("Semantic candidate retrieval failed, falling back: %s", exc)

        if semantic_candidates:
            return semantic_candidates

        # Fallback: word / token matching when semantic search is unavailable or yields no candidates
        terms = set(re.findall(r'[\w-]{3,}', text.casefold()))
        scored = {}
        for deal in deals.select_related('retrieval_profile', 'primary_contact').iterator():
            title = normalize(deal.title).casefold()
            profile = getattr(deal, 'retrieval_profile', None)
            profile_text = str(getattr(profile, 'profile_text', '') or '')
            haystack = ' '.join([title, deal.sector or '', deal.industry or '', profile_text]).casefold()
            score = len(terms & set(re.findall(r'[\w-]{3,}', haystack)))
            if title and re.search(r'(?<!\w)' + re.escape(title) + r'(?!\w)', text.casefold()):
                score += 100
            score += SequenceMatcher(None, text[:300].casefold(), title).ratio()
            if score > 1:
                scored[str(deal.id)] = {'deal_id': str(deal.id), 'title': deal.title, 'score': score,
                    'context': haystack[:3000], 'evidence': 'name/profile terms (fallback)'}
        return sorted(scored.values(), key=lambda x: (-x['score'], x['deal_id']))[:limit]

    @classmethod
    def match(cls, email, text, deals, *, use_ai=True):
        scoped_thread = Email.objects.filter(email_account_id=email.email_account_id)
        scoped_thread = scoped_thread.filter(conversation_id=email.conversation_id) if email.conversation_id else scoped_thread.filter(pk=email.pk)
        linked = {str(x) for x in scoped_thread.exclude(deal=None).values_list('deal_id', flat=True)}
        allowed = {str(x) for x in deals.filter(pk__in=linked).values_list('pk', flat=True)}
        explicit = re.search(r'deal_name\s*=\s*[\"\']?([^\"\'\r\n;|]+)', text, re.I)
        names = list(deals.filter(title__iexact=explicit.group(1).strip()).values_list('id', flat=True)) if explicit else []
        identities = linked | {str(x) for x in names}
        if len(identities) > 1 or linked != allowed:
            return {'status': 'needs_review', 'deal_id': None, 'candidates': [], 'evidence': 'Conflicting or unavailable linked deal identities.'}
        if len(identities) == 1:
            return {'status': 'matched', 'deal_id': next(iter(identities)), 'candidates': [],
                    'evidence': explicit.group(0) if explicit else 'Verified mailbox-scoped email.deal relationship', 'decision_source': 'deterministic'}
        candidates = cls.candidates(text, deals, use_semantic=use_ai)
        result = {'status': 'needs_review' if candidates else 'unmatched', 'deal_id': None,
                  'candidates': candidates, 'evidence': '', 'decision_source': 'retrieval'}
        if use_ai and candidates:
            ranked = cls.ai('match', {'email': text, 'candidates': candidates}, email.id)
            picked = str(ranked.get('deal_id') or '')
            excerpt = ranked.get('evidence', '')
            if picked and picked not in {c['deal_id'] for c in candidates}:
                raise ValueError('Matcher selected an unauthorized or nonexistent candidate.')
            if not isinstance(excerpt, str) or (excerpt and excerpt not in text):
                raise ValueError('Matcher evidence does not occur in the email.')
            result.update(suggested_deal_id=picked or None, evidence=excerpt, decision_source='ai')
            if picked:
                result['status'] = 'needs_review'
        return result
