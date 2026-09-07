"""Shared classification and existing-deal resolution for both email branches."""
import json
import re
from difflib import SequenceMatcher

from django.db.models import Q
from microsoft.models import Email
from .email_contributions import normalize


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
        """Use the same persisted retrieval profiles as global chat, with a scoped pool."""
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
                    'context': haystack[:3000], 'evidence': 'name/profile terms'}
        if use_semantic:
            from ai_orchestrator.services.embedding_processor import EmbeddingService
            service = EmbeddingService()
            if service.is_embedding_available(timeout=1):
                hits = service.search_deal_profiles(text[:12000], limit=30)
                permitted = {str(x) for x in deals.filter(id__in=[d.id for d in hits]).values_list('id', flat=True)}
                for rank, deal in enumerate(hits):
                    key = str(deal.id)
                    if key in permitted:
                        item = scored.setdefault(key, {'deal_id': key, 'title': deal.title, 'score': 0,
                            'context': (deal.deal_summary or '')[:3000], 'evidence': 'semantic profile'})
                        item['score'] += 10 / (rank + 1)
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
