"""Persist source evidence before any synthesis or embedding work."""
import base64
import hashlib
import json
import uuid
from pathlib import PurePath
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from bs4 import BeautifulSoup

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from deals.models import DealDocument
from meetings.models import MeetingNote
from microsoft.models import (Email, EmailIngestionRun, EmailContribution,
    EmailContributionOccurrence, EmailEvidenceLink, EmailPrivateBlob)
from .email_contributions import EmailContributionParser, digest
from .email_thread_unfolder import EmailThreadUnfolder


class EmailEvidenceService:
    MIME_SUFFIXES = {
        'application/pdf': '.pdf',
        'text/plain': '.txt',
        'text/html': '.html',
        'application/vnd.openxmlformats-officedocument.wordprocessingml.document': '.docx',
        'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet': '.xlsx',
        'application/vnd.openxmlformats-officedocument.presentationml.presentation': '.pptx',
    }

    @staticmethod
    def _provider_download_url(url):
        parsed = urlparse(url)
        host = (parsed.hostname or '').casefold()
        parts = [part for part in parsed.path.split('/') if part]
        if host == 'drive.google.com':
            file_id = parts[parts.index('d') + 1] if 'd' in parts and len(parts) > parts.index('d') + 1 else parse_qs(parsed.query).get('id', [None])[0]
            if file_id:
                return f'https://drive.usercontent.google.com/download?{urlencode({"id": file_id, "export": "download", "confirm": "t"})}'
        if host == 'docs.google.com' and 'd' in parts and len(parts) > parts.index('d') + 1:
            file_id = parts[parts.index('d') + 1]
            export_format = {'document': 'docx', 'spreadsheets': 'xlsx', 'presentation': 'pptx'}.get(parts[0])
            if export_format:
                return urlunparse((parsed.scheme, parsed.netloc, f'/{parts[0]}/d/{file_id}/export', '', urlencode({'format': export_format}), ''))
        return url

    @classmethod
    def _linked_filename(cls, occurrence, access):
        name = PurePath(str(access.get('filename') or occurrence.metadata.get('name') or '')).name
        suffix = PurePath(name).suffix
        if not suffix:
            suffix = cls.MIME_SUFFIXES.get(str(access.get('content_type') or '').casefold(), '')
            name = f'{name or "Linked document"}{suffix}'
        return name

    @staticmethod
    def snapshot(email, *, force_new=False):
        source = {field: getattr(email, field) for field in (
            'subject', 'body_html', 'body_text', 'body_preview', 'from_email',
            'to_emails', 'cc_emails', 'attachments', 'graph_id', 'internet_message_id', 'conversation_id')}
        for field in ('date_sent', 'date_received'):
            value = getattr(email, field)
            source[field] = value.isoformat() if value else None
        version = digest(json.dumps(source, sort_keys=True, default=str))
        if force_new:
            version = digest(f'{version}:manual-rerun:{uuid.uuid4().hex}')
        run, _ = EmailIngestionRun.objects.get_or_create(email=email, input_version=version,
            defaults={'source': source, 'stages': {'classification': 'pending', 'match': 'pending', 'save': 'pending', 'index': 'pending'}})
        return run

    @staticmethod
    def source_for_email(email):
        """Build the same immutable source shape for any message in a thread."""
        source = {field: getattr(email, field) for field in (
            'subject', 'body_html', 'body_text', 'body_preview', 'from_email',
            'to_emails', 'cc_emails', 'attachments', 'graph_id',
            'internet_message_id', 'conversation_id')}
        for field in ('date_sent', 'date_received'):
            value = getattr(email, field)
            source[field] = value.isoformat() if value else None
        source['source_email_id'] = str(email.id)
        source['source_graph_id'] = email.graph_id
        return source

    @classmethod
    def thread_emails(cls, run):
        """Return every stored Graph message in this mailbox conversation."""
        conversation_id = run.email.conversation_id
        if not conversation_id:
            return [run.email]
        messages = list(Email.objects.filter(
            email_account_id=run.email.email_account_id,
            conversation_id=conversation_id,
        ))
        return messages or [run.email]

    @classmethod
    def parts(cls, run, deal=None):
        known = EmailContribution.objects.none()
        if deal:
            known = EmailContribution.objects.filter(
                email_account_id=run.email.email_account_id,
                emailcontributionoccurrence__evidence__deal=deal,
                emailcontributionoccurrence__evidence__active=True,
                emailcontributionoccurrence__status='saved').distinct()
        messages = cls.thread_emails(run)
        deltas = EmailThreadUnfolder.unfold(messages)
        by_id = {str(message.id): message for message in messages}
        result = []
        for delta in deltas:
            message = by_id.get(str(delta.email_id))
            if not message or not delta.text.strip():
                continue
            source = cls.source_for_email(message)
            # The unfolder has already removed quoted history from later
            # replies. The contribution parser then handles embedded forward
            # headers inside each newly introduced delta.
            source['body_text'] = delta.text
            source['body_html'] = ''
            result.extend(EmailContributionParser.parse(
                source, email_id=message.id, known=known,
            ))
        if result:
            return result
        # Preserve the selected snapshot as a fallback for an incompletely
        # synchronized thread or a message with only a preview body.
        return EmailContributionParser.parse(run.source, email_id=run.email_id, known=known)

    @staticmethod
    def provenance(run, **extra):
        source_email_id = extra.get('source_email_id') or run.email_id
        graph_id = extra.get('source_graph_id') or run.source.get('graph_id')
        subject = extra.get('source_subject') or run.source.get('subject')
        return {'email_id': str(source_email_id), 'email_account_id': str(run.email.email_account_id),
            'graph_id': graph_id, 'subject': subject,
            'from': extra.get('source_from') or run.source.get('from_email'),
            'date': extra.get('source_date') or run.source.get('date_sent') or run.source.get('date_received'),
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
                origin_email_id = part.headers.get('email_id') or run.email_id
                origin = Email.objects.filter(pk=origin_email_id).first() or run.email
                source = cls.source_for_email(origin)
                provenance = cls.provenance(run, headers=part.headers, source_span=[part.start, part.end],
                    source_email_id=str(origin.id), source_graph_id=origin.graph_id,
                    source_subject=origin.subject, source_from=origin.from_email,
                    source_date=(origin.date_sent or origin.date_received).isoformat()
                    if (origin.date_sent or origin.date_received) else None)
                title = (part.headers.get('subject') or source.get('subject') or 'Email') + f' · message {position + 1}'
                if output_kind == 'meeting_note' and not link.meeting_note_id:
                    # Adopt an unambiguous legacy note only for an equivalent body.
                    existing = list(MeetingNote.objects.filter(source_email_id=run.email_id, body=part.text)[:2])
                    note = existing[0] if len(existing) == 1 else MeetingNote.objects.create(
                        title=title[:255], body=part.text, source='email', source_email_id=origin.id,
                        meeting_at=origin.date_sent or origin.date_received, metadata=provenance)
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
        origin_ids = {str(part.headers.get('email_id') or run.email_id) for part in parts} or {str(run.email_id)}
        for origin_id in origin_ids:
            Email.objects.filter(pk=origin_id).update(
                deal=deal,
                extracted_text='\n\n'.join(p.text for p in parts if (p.headers.get('email_id') or run.email_id) == origin_id),
                is_processed=True,
                processed_at=timezone.now(),
            )
        return saved

    @classmethod
    def save_attachments(cls, run, deal=None):
        """Capture bytes even when matching is pending. Downloads are outside locks."""
        from .graph_service import GraphAPIService
        failures = []
        messages = cls.thread_emails(run)
        seen = set()
        position = 0
        for message in messages:
            source = cls.source_for_email(message)
            for attachment in source.get('attachments') or []:
                position += 1
                identifier = attachment.get('id')
                if identifier in seen:
                    continue
                seen.add(identifier)
                attachment = {**attachment, 'source_email_id': str(message.id), 'source_graph_id': message.graph_id}
                key = 'attachment:' + str(identifier or position)
                occurrence, _ = EmailContributionOccurrence.objects.get_or_create(run=run, source_key=key,
                    defaults={'position': 100000 + position, 'metadata': dict(attachment)})
                if attachment.get('isInline') is True:
                    # Graph exposes email logos and signature artwork as file
                    # attachments. They are body decoration, not durable deal
                    # evidence, and unsupported images must not block indexing
                    # or deal synthesis forever.
                    occurrence.metadata = dict(attachment)
                    occurrence.evidence = None
                    occurrence.status = 'skipped'
                    occurrence.error = ''
                    occurrence.save(update_fields=['metadata', 'evidence', 'status', 'error'])
                    continue
                try:
                    if not identifier:
                        raise ValueError('Attachment has no Graph identity.')
                    blob_is_durable = False
                    if occurrence.blob_id:
                        try:
                            existing_content = occurrence.blob.read_bytes()
                            blob_is_durable = bool(existing_content)
                            if blob_is_durable and not occurrence.blob.payload:
                                occurrence.blob.payload = existing_content
                                occurrence.blob.save(update_fields=['payload'])
                        except (OSError, ValueError):
                            blob_is_durable = False
                    if not blob_is_durable:
                        limit = int(getattr(settings, 'EMAIL_EVIDENCE_MAX_ATTACHMENT_BYTES', 25 * 1024 * 1024))
                        if int(attachment.get('size') or 0) > limit:
                            raise ValueError('Attachment exceeds configured capture limit; review required.')
                        response = GraphAPIService().get_attachment_content(run.email.email_account.email,
                            message.graph_id, identifier)
                        encoded = response.get('contentBytes') or ''
                        if len(encoded) > (limit * 4 // 3 + 8):
                            raise ValueError('Attachment exceeds configured capture limit.')
                        if encoded:
                            content = base64.b64decode(encoded, validate=True)
                        elif response.get('item'):
                            # Graph itemAttachment responses expose the
                            # attached message body rather than contentBytes.
                            # Preserve that message as durable text evidence
                            # instead of dropping it as an unsupported blob.
                            item = response['item']
                            item_body = (item.get('body') or {}).get('content') or item.get('bodyPreview') or ''
                            item_headers = '\n'.join(filter(None, (
                                f"From: {(item.get('from') or {}).get('emailAddress', {}).get('address', '')}",
                                f"Sent: {item.get('sentDateTime') or ''}",
                                f"Subject: {item.get('subject') or ''}",
                            )))
                            content = f"{item_headers}\n\n{item_body}".encode('utf-8', errors='replace')
                        else:
                            content = b''
                        if not content:
                            raise ValueError('Attachment bytes unavailable; embedded/reference attachment requires review.')
                        sha = hashlib.sha256(content).hexdigest()
                        blob, _ = EmailPrivateBlob.objects.get_or_create(email_account_id=run.email.email_account_id,
                            sha256=sha, defaults={'size': len(content), 'payload': content})
                        with transaction.atomic():
                            blob = EmailPrivateBlob.objects.select_for_update().get(pk=blob.pk)
                            if not blob.payload:
                                blob.payload = content
                                blob.size = len(content)
                                blob.save(update_fields=['payload', 'size'])
                        occurrence.blob = blob
                        occurrence.save(update_fields=['blob'])
                    if deal:
                        with transaction.atomic():
                            link, _ = EmailEvidenceLink.objects.get_or_create(email_account_id=run.email.email_account_id,
                                deal=deal, source_key='blob:' + occurrence.blob.sha256, kind='email_attachment')
                            link = EmailEvidenceLink.objects.select_for_update().get(pk=link.pk)
                            name = PurePath(str(attachment.get('name') or 'Attachment')).name
                            provenance = cls.provenance(run, attachment_id=identifier, filename=name,
                                source_email_id=str(message.id), source_graph_id=message.graph_id,
                                source_subject=message.subject)
                            if not link.document_id:
                                link.document = DealDocument.objects.create(deal=deal, title=name,
                                    source_map_json={'email': provenance}, transcription_status='pending')
                            link.blob = occurrence.blob
                            link.provenance = provenance
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
                    if deal:
                        EmailEvidenceLink.objects.filter(
                            email_account_id=run.email.email_account_id,
                            deal=deal, source_key=key, kind='email_link',
                        ).update(active=False, index_status='waiting_service', error=str(exc)[:1500])
                    failures.append(str(occurrence.id))
        return failures

    @classmethod
    def save_links(cls, run, deal=None):
        """Capture safe document links embedded in HTML as durable evidence."""
        from deals.services.research_acquisition import ResearchAcquisitionService

        soup = BeautifulSoup(str(run.source.get('body_html') or ''), 'html.parser')
        links = []
        for anchor in soup.find_all('a'):
            url = str(anchor.get('href') or '').strip()
            parsed = urlparse(url)
            if parsed.scheme not in ('http', 'https'):
                continue
            host = (parsed.hostname or '').casefold()
            suffix = PurePath(parsed.path).suffix.casefold()
            supported_host = any(token in host for token in (
                'sharepoint.com', 'onedrive.live.com', '1drv.ms',
                'drive.google.com', 'docs.google.com',
            ))
            if not supported_host and suffix not in ('.pdf', '.txt', '.html', '.htm'):
                continue
            links.append((url, anchor.get_text(' ', strip=True)))

        failures = []
        for position, (url, label) in enumerate(dict.fromkeys(links)):
            url_hash = hashlib.sha256(url.encode('utf-8')).hexdigest()
            key = 'link:' + url_hash
            occurrence, _ = EmailContributionOccurrence.objects.get_or_create(
                run=run, source_key=key,
                defaults={
                    'position': 200000 + position,
                    'metadata': {'name': label or PurePath(urlparse(url).path).name or 'Linked document', 'url': url},
                },
            )
            try:
                download_url = cls._provider_download_url(url)
                prior_access = occurrence.metadata.get('access') or {}
                if (
                    occurrence.blob_id
                    and download_url != url
                    and prior_access.get('content_type') == 'text/html'
                ):
                    occurrence.blob = None
                    occurrence.save(update_fields=['blob'])
                blob_is_durable = False
                if occurrence.blob_id:
                    try:
                        existing_content = occurrence.blob.read_bytes()
                        blob_is_durable = bool(existing_content)
                        if blob_is_durable and not occurrence.blob.payload:
                            occurrence.blob.payload = existing_content
                            occurrence.blob.save(update_fields=['payload'])
                    except (OSError, ValueError):
                        blob_is_durable = False
                if not blob_is_durable:
                    content, access = ResearchAcquisitionService().download(download_url)
                    if not content:
                        raise ValueError('Linked document returned no content.')
                    provider_host = (urlparse(url).hostname or '').casefold()
                    is_provider_link = any(token in provider_host for token in (
                        'sharepoint.com', 'onedrive.live.com', '1drv.ms',
                        'drive.google.com', 'docs.google.com',
                    ))
                    if is_provider_link and access.get('content_type') == 'text/html':
                        raise ValueError(
                            'Linked document requires provider authentication or an exportable public file URL.'
                        )
                    sha = hashlib.sha256(content).hexdigest()
                    blob, _ = EmailPrivateBlob.objects.get_or_create(
                        email_account_id=run.email.email_account_id,
                        sha256=sha, defaults={'size': len(content), 'payload': content},
                    )
                    with transaction.atomic():
                        blob = EmailPrivateBlob.objects.select_for_update().get(pk=blob.pk)
                        if not blob.payload:
                            blob.payload = content
                            blob.size = len(content)
                            blob.save(update_fields=['payload', 'size'])
                    occurrence.blob = blob
                    occurrence.metadata = {
                        **occurrence.metadata, 'access': access,
                        'download_url': download_url,
                        'name': cls._linked_filename(occurrence, access),
                    }
                    occurrence.save(update_fields=['blob', 'metadata'])
                if deal:
                    with transaction.atomic():
                        link, _ = EmailEvidenceLink.objects.get_or_create(
                            email_account_id=run.email.email_account_id,
                            deal=deal, source_key=key, kind='email_link',
                        )
                        link = EmailEvidenceLink.objects.select_for_update().get(pk=link.pk)
                        name = cls._linked_filename(occurrence, occurrence.metadata.get('access') or {})
                        if not link.document_id:
                            link.document = DealDocument.objects.create(
                                deal=deal, title=name, file_url=url if len(url) <= 200 else None,
                                source_map_json={'email': cls.provenance(run, source_url=url)},
                                transcription_status='pending',
                            )
                        link.blob = occurrence.blob
                        link.provenance = cls.provenance(run, source_url=url, access=occurrence.metadata.get('access'))
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
