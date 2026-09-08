"""Revision-checked human decisions and source-owned evidence reconciliation."""
from django.db import transaction
from django.utils import timezone

from ai_orchestrator.models import DocumentChunk, DealRetrievalProfile
from deals.models import DealAnalysis
from microsoft.models import Email, EmailIngestionRun, EmailEvidenceLink


class StaleEmailDecision(ValueError):
    pass


def deal_email_evidence_gaps(deal):
    """Return captured email sources that do not currently have a usable document."""
    latest_runs = []
    seen_email_ids = set()
    runs = (
        EmailIngestionRun.objects.filter(email__deal=deal)
        .exclude(status='superseded')
        .select_related('email')
        .prefetch_related('occurrences__evidence__document')
        .order_by('email_id', '-created_at')
    )
    for run in runs:
        if run.email_id in seen_email_ids:
            continue
        seen_email_ids.add(run.email_id)
        latest_runs.append(run)

    gaps = []
    for run in latest_runs:
        for occurrence in run.occurrences.all():
            link = occurrence.evidence
            document = link.document if link and link.document_id else None
            if document:
                continue
            source_kind = (
                link.kind if link else
                'email_link' if occurrence.source_key.startswith('link:') else
                'email_attachment' if occurrence.source_key.startswith('attachment:') else
                'email_body'
            )
            metadata = occurrence.metadata if isinstance(occurrence.metadata, dict) else {}
            gaps.append({
                'source_id': str(occurrence.id),
                'email_id': str(run.email_id),
                'source_kind': source_kind,
                'title': metadata.get('name') or run.email.subject or 'Email evidence',
                'status': occurrence.status,
                'error': occurrence.error or (link.error if link else '') or 'Deal document has not been created.',
                'source_url': metadata.get('url'),
            })
    return gaps


def run_status(run, *, include_content=False):
    if not run:
        return {
            'run_id': None, 'status': 'not_started', 'revision': 0,
            'classification': {}, 'match': {}, 'stages': {}, 'outputs': [],
            'deal_id': None,
            'manifest': {'expected': 0, 'ready': 0, 'processing': 0, 'failed': 0},
            'report_ready': False, 'can_build_with_gaps': False,
            'blockers': ['Process the email to capture its evidence.'],
        }
    from deals.services.document_artifacts import DocumentArtifactService

    outputs = []
    for occurrence in run.occurrences.select_related('evidence', 'contribution').all():
        link = occurrence.evidence
        document = link.document if link and link.document_id else None
        artifact_status = DocumentArtifactService.artifact_status(document) if document else 'pending'
        item = {'id': str(occurrence.id), 'source_key': occurrence.source_key,
                'status': occurrence.status, 'error': occurrence.error,
                'filename': occurrence.metadata.get('name'),
                'source_kind': link.kind if link else (
                    'email_link' if occurrence.source_key.startswith('link:')
                    else 'email_attachment' if occurrence.source_key.startswith('attachment:')
                    else 'email_body'
                ),
                'document_id': str(link.document_id) if link and link.document_id else None,
                'meeting_note_id': str(link.meeting_note_id) if link and link.meeting_note_id else None,
                'index_status': link.index_status if link else 'pending',
                'index_error': link.error if link else '',
                'transcription_status': document.transcription_status if document else 'pending',
                'artifact_status': artifact_status,
                'chunking_status': document.chunking_status if document else 'not_chunked',
                'chunk_count': int((link.provenance or {}).get('chunk_count') or 0) if link else 0,
                'reused': bool(link and link.occurrences.exclude(run=run).exists())}
        if include_content and occurrence.contribution_id:
            item['text'] = occurrence.contribution.text
            item['headers'] = occurrence.contribution.headers
        outputs.append(item)
    # A failed external href does not stop the rest of the email pipeline, but
    # it remains in the manifest so the deal VDR and report disclose the gap.
    required_outputs = outputs
    expected = len(required_outputs)
    ready = sum(1 for item in required_outputs if item['index_status'] == 'completed' and item['artifact_status'] == 'complete')
    failed = sum(1 for item in required_outputs if item['status'] == 'failed' or item['index_status'] == 'failed')
    processing = sum(1 for item in required_outputs if item['status'] != 'failed' and item['index_status'] not in ('completed', 'failed'))
    deal_id = run.match.get('deal_id') or run.match.get('suggested_deal_id')
    blockers = []
    if not deal_id:
        blockers.append('Confirm the target deal before saving evidence.')
    if failed:
        blockers.append(f'{failed} evidence item(s) failed and require attention.')
    if processing:
        blockers.append(f'{processing} evidence item(s) are still being prepared.')
    if expected == 0:
        blockers.append('No evidence items have been captured yet.')
    stages = {
        'scan': 'completed',
        'confirmation': 'completed' if run.match.get('status') == 'matched' else 'needs_review',
        'capture': run.stages.get('save', 'pending'),
        'extraction': 'completed' if outputs and all(item['transcription_status'] == 'complete' for item in outputs) else 'pending',
        'artifact': run.stages.get('artifacts', 'pending'),
        'index': run.stages.get('chunks', run.stages.get('index', 'pending')),
        **run.stages,
    }
    return {'run_id': str(run.id), 'status': run.status, 'revision': run.revision,
            'classification': run.classification, 'match': run.match, 'stages': stages,
            'error': run.error, 'outputs': outputs,
            'deal_id': deal_id,
            'manifest': {'expected': expected, 'ready': ready, 'processing': processing, 'failed': failed},
            'report_ready': bool(expected) and ready == expected and not failed,
            'can_build_with_gaps': ready > 0 and ready < expected,
            'blockers': blockers,
            'original_text': (run.source.get('body_text') or '') if include_content else None}


@transaction.atomic
def confirm_decision(run_id, *, email, deal, expected_revision, kind=None, actor=None):
    Email.objects.select_for_update().get(pk=email.pk)
    run = EmailIngestionRun.objects.select_for_update().get(pk=run_id, email=email)
    if run.revision != expected_revision or EmailIngestionRun.objects.filter(email=email, created_at__gt=run.created_at).exists():
        raise StaleEmailDecision('The email decision changed. Reload before confirming.')
    if EmailIngestionRun.objects.filter(email=email, lease_until__gt=timezone.now()).exists():
        raise StaleEmailDecision('Processing is in progress. Wait for it to finish before correcting the decision.')
    if kind not in (None, 'MEETING_NOTE', 'NORMAL_EMAIL'):
        raise ValueError('Choose MEETING_NOTE or NORMAL_EMAIL.')
    previous = {'classification': run.classification, 'match': run.match}
    changing = str(email.deal_id or '') != str(deal.id) or (kind and kind != run.classification.get('type'))
    if changing:
        links = list(EmailEvidenceLink.objects.filter(occurrences__run__email=email).distinct())
        # Only this source's membership is removed. Shared outputs remain in use.
        for link in links:
            link.occurrences.filter(run__email=email).update(evidence=None, status='pending')
            if link.occurrences.exists():
                continue
            if link.document_id:
                DocumentChunk.objects.filter(source_type='document', source_id=str(link.document_id)).delete()
                link.document.delete()
                link.document = None
            if link.meeting_note_id:
                DocumentChunk.objects.filter(source_type='meeting_note', source_id=str(link.meeting_note_id), deal=link.deal).delete()
                link.meeting_note.deals.remove(link.deal)
                link.meeting_note.is_indexed = False
                link.meeting_note.save(update_fields=['is_indexed'])
                link.meeting_note = None
            link.active = False
            link.index_status = 'pending'
            link.save()
            DealRetrievalProfile.objects.filter(deal=link.deal).delete()
            # Preserve analyses, but make their stale-evidence status inspectable.
            for analysis in DealAnalysis.objects.filter(deal=link.deal):
                payload = dict(analysis.analysis_json or {})
                metadata = dict(payload.get('metadata') or {})
                corrections = list(metadata.get('email_evidence_corrections') or [])
                corrections.append({'email_id': str(email.id), 'evidence_id': str(link.id), 'review_required': True})
                metadata['email_evidence_corrections'] = corrections
                payload['metadata'] = metadata
                analysis.analysis_json = payload
                analysis.save(update_fields=['analysis_json'])
        # Legacy email-source chunks must not survive a correction either.
        DocumentChunk.objects.filter(source_type='email', source_id=str(email.id)).delete()
    EmailIngestionRun.objects.filter(email=email).exclude(pk=run.pk).update(status='superseded')
    run.match = {'status': 'matched', 'deal_id': str(deal.id), 'decision_source': 'manual', 'evidence': 'Analyst confirmed'}
    if kind:
        run.classification = {'type': kind, 'method': 'manual', 'status': 'completed', 'segment_roles': []}
    run.decision_log = [*run.decision_log, {'actor_id': actor.pk if actor else None,
        'at': timezone.now().isoformat(), 'previous': previous, 'deal_id': str(deal.id), 'kind': kind}]
    run.revision += 1
    run.status = 'pending'
    run.next_attempt_at = None
    run.error = ''
    run.save()
    Email.objects.filter(pk=email.pk).update(deal=deal, is_indexed=False, processing_status='pending')
    return run
