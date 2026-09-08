"""Persist source evidence before any synthesis or embedding work."""
import base64
import hashlib
import json
from pathlib import PurePath

from django.conf import settings
from django.core.files.base import ContentFile
from django.db import transaction
from django.utils import timezone

from deals.models import DealDocument
from meetings.models import MeetingNote
from microsoft.models import (Email, EmailIngestionRun, EmailContribution,
    EmailContributionOccurrence, EmailEvidenceLink, EmailPrivateBlob)
from .email_contributions import EmailContributionParser, digest


class EmailEvidenceService:
    @staticmethod
    def snapshot(email):
        source = {field: getattr(email, field) for field in (
            'subject', 'body_html', 'body_text', 'body_preview', 'from_email',
            'to_emails', 'cc_emails', 'attachments', 'graph_id', 'internet_message_id', 'conversation_id')}
        for field in ('date_sent', 'date_received'):
            value = getattr(email, field)
            source[field] = value.isoformat() if value else None
        version = digest(json.dumps(source, sort_keys=True, default=str))
        run, _ = EmailIngestionRun.objects.get_or_create(email=email, input_version=version,
            defaults={'source': source, 'stages': {'classification': 'pending', 'match': 'pending', 'save': 'pending', 'index': 'pending'}})
        return run

    @staticmethod
    def parts(run, deal=None):
        known = EmailContribution.objects.none()
        if deal:
            known = EmailContribution.objects.filter(
                email_account_id=run.email.email_account_id,
                emailcontributionoccurrence__evidence__deal=deal,
                emailcontributionoccurrence__evidence__active=True,
                emailcontributionoccurrence__status='saved').distinct()
        return EmailContributionParser.parse(run.source, email_id=run.email_id, known=known)

    @staticmethod
    def provenance(run, **extra):
        return {'email_id': str(run.email_id), 'email_account_id': str(run.email.email_account_id),
            'graph_id': run.source.get('graph_id'), 'subject': run.source.get('subject'),
            'from': run.source.get('from_email'), 'date': run.source.get('date_sent') or run.source.get('date_received'),
            'run_id': str(run.id), **extra}

    @classmethod
    def save_parts(cls, run, deal, parts, classification):
        roles = classification.get('segment_roles', [])
        saved = []
        for position, part in enumerate(parts):
            kind = roles[position]['type'] if position < len(roles) else classification['type']
            if kind == 'UNKNOWN':
                continue
            output_kind = 'meeting_note' if kind == 'MEETING_NOTE' else 'email_body'
            with transaction.atomic():
                contribution, _ = EmailContribution.objects.get_or_create(
                    email_account_id=run.email.email_account_id, fingerprint=part.fingerprint,
                    defaults={'text': part.text, 'headers': part.headers})
                occurrence, _ = EmailContributionOccurrence.objects.get_or_create(run=run, source_key=f'body:{position}',
                    defaults={'position': position, 'contribution': contribution,
                              'metadata': {'start': part.start, 'end': part.end, 'diagnostics': part.diagnostics}})
                link, _ = EmailEvidenceLink.objects.get_or_create(email_account_id=run.email.email_account_id,
                    deal=deal, source_key='body:' + part.fingerprint, kind=output_kind)
                link = EmailEvidenceLink.objects.select_for_update().get(pk=link.pk)
                provenance = cls.provenance(run, headers=part.headers, source_span=[part.start, part.end])
                title = (run.source.get('subject') or 'Email') + f' · message {position + 1}'
                if output_kind == 'meeting_note' and not link.meeting_note_id:
                    # Adopt an unambiguous legacy note only for an equivalent body.
                    existing = list(MeetingNote.objects.filter(source_email_id=run.email_id, body=part.text)[:2])
                    note = existing[0] if len(existing) == 1 else MeetingNote.objects.create(
                        title=title[:255], body=part.text, source='email', source_email_id=run.email_id,
                        meeting_at=run.email.date_sent or run.email.date_received, metadata=provenance)
                    note.deals.add(deal)
                    link.meeting_note = note
                # Every contribution is also a DealDocument. MeetingNote remains
                # the calendar-facing representation; DealDocument is the common
                # artifact/chunk/report substrate used by email, uploads and VDR.
                if not link.document_id:
                    link.document = DealDocument.objects.create(deal=deal, title=title,
                        extracted_text=part.text, normalized_text=part.text,
                        source_map_json={'email': provenance, 'source_kind': output_kind},
                        transcription_status='complete')
                link.provenance = provenance
                link.active = True
                link.save()
                occurrence.contribution = contribution
                occurrence.evidence = link
                occurrence.status = 'saved'
                occurrence.save()
                saved.append(link)
        Email.objects.filter(pk=run.email_id).update(deal=deal, extracted_text='\n\n'.join(p.text for p in parts),
            is_processed=True, processed_at=timezone.now())
        return saved

    @classmethod
    def save_attachments(cls, run, deal=None):
        """Capture bytes even when matching is pending. Downloads are outside locks."""
        from .graph_service import GraphAPIService
        failures = []
        for position, attachment in enumerate(run.source.get('attachments') or []):
            identifier = attachment.get('id')
            key = 'attachment:' + str(identifier or position)
            occurrence, _ = EmailContributionOccurrence.objects.get_or_create(run=run, source_key=key,
                defaults={'position': 100000 + position, 'metadata': dict(attachment)})
            try:
                if not identifier:
                    raise ValueError('Attachment has no Graph identity.')
                if not occurrence.blob_id:
                    limit = int(getattr(settings, 'EMAIL_EVIDENCE_MAX_ATTACHMENT_BYTES', 25 * 1024 * 1024))
                    if int(attachment.get('size') or 0) > limit:
                        raise ValueError('Attachment exceeds configured capture limit; review required.')
                    response = GraphAPIService().get_attachment_content(run.email.email_account.email,
                        run.source['graph_id'], identifier)
                    encoded = response.get('contentBytes') or ''
                    if len(encoded) > (limit * 4 // 3 + 8):
                        raise ValueError('Attachment exceeds configured capture limit.')
                    content = base64.b64decode(encoded, validate=True)
                    if not content:
                        raise ValueError('Attachment bytes unavailable; embedded/reference attachment requires review.')
                    sha = hashlib.sha256(content).hexdigest()
                    blob, _ = EmailPrivateBlob.objects.get_or_create(email_account_id=run.email.email_account_id,
                        sha256=sha, defaults={'size': len(content)})
                    # Unique blob row serializes writers of the same attachment.
                    with transaction.atomic():
                        blob = EmailPrivateBlob.objects.select_for_update().get(pk=blob.pk)
                        if not blob.file:
                            blob.file.save(f'{run.email.email_account_id}/{sha}', ContentFile(content), save=True)
                    occurrence.blob = blob
                    occurrence.save(update_fields=['blob'])
                if deal:
                    with transaction.atomic():
                        link, _ = EmailEvidenceLink.objects.get_or_create(email_account_id=run.email.email_account_id,
                            deal=deal, source_key='blob:' + occurrence.blob.sha256, kind='email_attachment')
                        link = EmailEvidenceLink.objects.select_for_update().get(pk=link.pk)
                        name = PurePath(str(attachment.get('name') or 'Attachment')).name
                        if not link.document_id:
                            link.document = DealDocument.objects.create(deal=deal, title=name,
                                source_map_json={'email': cls.provenance(run, attachment_id=identifier, filename=name)},
                                transcription_status='pending')
                        link.blob = occurrence.blob
                        link.provenance = cls.provenance(run, attachment_id=identifier, filename=name)
                        link.active = True
                        link.save()
                        occurrence.evidence = link
                occurrence.status = 'saved' if deal else 'captured'
                occurrence.error = ''
                occurrence.save()
            except Exception as exc:
                occurrence.status = 'failed'
                occurrence.error = str(exc)[:1500]
                occurrence.save(update_fields=['status', 'error'])
                failures.append(str(occurrence.id))
        return failures
