"""Coverage review history for the current deal report, without exposing prompts."""
from ai_orchestrator.models import AIAuditLog
from django.db.models import Q
from ai_orchestrator.services.report_source_review import material_review_errors


def recorded_report_gaps(deal):
    """Return the saved section warnings used by the report source-gap card."""
    parents = list(AIAuditLog.objects.filter(source_id=str(deal.id), source_metadata__queue_kind='report')
        .filter(Q(source_metadata__queue_state__isnull=True) | ~Q(source_metadata__queue_state='cancelled'))
        .order_by('-created_at').values_list('id', flat=True)[:10])
    seen_sections, gaps = set(), []
    attempts = AIAuditLog.objects.filter(source_type='vdr_report_section', status='COMPLETED',
        source_metadata__vdr_parent_audit_id__in=[str(pk) for pk in parents]).only('id', 'source_metadata').order_by('-created_at')
    for attempt in attempts:
        metadata = attempt.source_metadata or {}
        if metadata.get('report_section_outcome') in {'draft_ready', 'draft_ready_with_gaps', 'superseded'} or metadata.get('inference_state') == 'superseded':
            continue
        section = metadata.get('report_section')
        if not section or section in seen_sections:
            continue
        seen_sections.add(section)
        for index, issue in enumerate(metadata.get('report_validation_warnings') or []):
            if not isinstance(issue, dict) or not str(issue.get('message') or '').strip():
                continue
            gaps.append({'source_type': 'report_validation', 'source_id': f'{attempt.pk}:{index}',
                'title': section, 'error': issue['message'], 'status': 'needs_review',
                'issue_kind': issue.get('kind'), 'confirmed': bool(issue.get('confirmed', False)),
                'details': issue.get('details') or []})
    return gaps


def latest_review_feedback(deal):
    """Snapshot only the current run's latest completed review of each section."""
    parent = AIAuditLog.objects.filter(source_id=str(deal.id)).filter(
        Q(source_metadata__queue_kind='report') | Q(source_type='deal_full_synthesis')
    ).exclude(source_metadata__queue_state='cancelled').order_by('-created_at').first()
    if not parent:
        return {}
    result = dict((parent.source_metadata or {}).get('regeneration_feedback') or {})
    reviewed_at = {}
    for review in AIAuditLog.objects.filter(source_type='report_section_quality_review',
            source_id=str(parent.id), status='COMPLETED').order_by('created_at'):
        payload = review.parsed_json or {}
        title = (review.source_metadata or {}).get('report_section')
        if title and isinstance(payload.get('coverage_gaps'), list):
            reviewed_at[title] = review.created_at
            result[title] = {'report_audit_id': str(parent.id), 'review_audit_id': str(review.id),
                'coverage_gaps': [gap for gap in payload['coverage_gaps'] if isinstance(gap, str)],
                'source_errors': material_review_errors(payload.get('findings') or [])}
    latest_generations = {}
    for generation in AIAuditLog.objects.filter(source_type='vdr_report_section', source_id=str(parent.id)).order_by('created_at'):
        title = (generation.source_metadata or {}).get('report_section')
        if title:
            latest_generations[title] = generation
    for title, generation in latest_generations.items():
        if generation.status == 'FAILED' and generation.error_message and (
                title not in reviewed_at or generation.created_at > reviewed_at[title]):
            result.setdefault(title, {'report_audit_id': str(parent.id), 'coverage_gaps': [], 'source_errors': []})
            result[title]['validation_error'] = generation.error_message
    return result


def format_review_feedback(feedback):
    if not feedback:
        return ''
    lines = ['Prior review feedback for this section. Re-check each correction against the current primary evidence; '
             'review findings are guidance, not primary facts. Resolve coverage with supported analysis or an explicit evidence gap. '
             'Prior R-number citation markers belong to the previous evidence pack. Find and cite the matching current '
             'primary source; never reuse an old rank as proof.']
    gaps = feedback.get('coverage_gaps') or []
    errors = feedback.get('source_errors') or []
    if gaps:
        lines += ['Coverage gaps:'] + ['- '+str(gap)[:600] for gap in gaps[:12]]
    if errors:
        lines += ['Source corrections:'] + [f"- Claim: {str(error.get('claim',''))[:200]}. Issue: {str(error.get('issue',''))[:600]}. Correction: {str(error.get('correction',''))[:600]}" for error in errors[:8]]
    if feedback.get('validation_error'):
        lines += ['Calculation or output validation: '+str(feedback['validation_error'])[:2000]]
    return '\n'.join(lines)[:12000] if gaps or errors or feedback.get('validation_error') else ''


def report_coverage_for_deal(deal):
    parent = AIAuditLog.objects.filter(source_id=str(deal.id)).filter(
        Q(source_metadata__queue_kind='report') | Q(source_type='deal_full_synthesis')
    ).exclude(source_metadata__queue_state='cancelled').order_by('-created_at').first()
    if not parent:
        return None
    reviews = AIAuditLog.objects.filter(source_type='report_section_quality_review',
        source_id=str(parent.id), status='COMPLETED').order_by('created_at').only(
            'id', 'parsed_json', 'source_metadata', 'created_at', 'completed_at')
    sections = {}
    for title, feedback in ((parent.source_metadata or {}).get('regeneration_feedback') or {}).items():
        inherited = feedback.get('coverage_gaps') or []
        if inherited:
            checked_at = parent.created_at.isoformat()
            sections[title] = {'title': title, 'latest_review_at': checked_at,
                'gaps': {gap: {'text': gap, 'status': 'open', 'first_seen_at': checked_at,
                    'last_review_at': checked_at, 'review_audit_id': feedback.get('review_audit_id', '')}
                    for gap in inherited if isinstance(gap, str) and gap.strip()}}
    review_count = 0
    for review in reviews:
        payload = review.parsed_json or {}
        gaps = payload.get('coverage_gaps')
        if not isinstance(gaps, list) or not all(isinstance(gap, str) for gap in gaps):
            continue
        title = (review.source_metadata or {}).get('report_section')
        if not title:
            continue
        review_count += 1
        section = sections.setdefault(title, {'title': title, 'gaps': {}, 'latest_review_at': None})
        checked_at = (review.completed_at or review.created_at).isoformat()
        current = {gap.strip() for gap in gaps if gap.strip()}
        for text in current:
            section['gaps'].setdefault(text, {'text': text, 'first_seen_at': checked_at})
        for text, gap in section['gaps'].items():
            status = 'open' if text in current else 'addressed' if not current else gap.get('status', 'earlier')
            if text not in current and current and status == 'open':
                status = 'earlier'
            gap.update(status=status,
                       last_review_at=checked_at, review_audit_id=str(review.id))
        section['latest_review_at'] = checked_at
    result = [{**section, 'gaps': list(section['gaps'].values())} for section in sections.values()]
    result = [section for section in result if section['gaps']]
    return {'report_audit_id': str(parent.id), 'report_state': parent.status.lower(),
            'report_started_at': parent.created_at.isoformat(), 'review_count': review_count,
            'open_count': sum(gap['status'] == 'open' for section in result for gap in section['gaps']),
            'addressed_count': sum(gap['status'] == 'addressed' for section in result for gap in section['gaps']),
            'sections': result}
