"""Versioned email jobs with durable pending state and independent evidence indexing."""
from datetime import timedelta
import logging
import os
import time
from pathlib import Path
from pathlib import PurePath
from urllib.parse import unquote, urlparse
import uuid

from bs4 import BeautifulSoup
from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from deals.models import Deal
from microsoft.models import Email, EmailIngestionRun, EmailEvidenceLink
from .email_evidence import EmailEvidenceService as Evidence
from .email_matching import EmailDecisionService as Decisions, EmailDecisionUnavailable

logger = logging.getLogger(__name__)


class EmailIngestionCancelled(Exception):
    pass


class EmailIngestionService:
    @classmethod
    def _raise_if_cancelled(cls, run):
        current_status = EmailIngestionRun.objects.filter(pk=run.pk).values_list('status', flat=True).first()
        audit = cls._audit_log_for_run(run)
        if current_status == 'cancelled' or (
            audit and (audit.source_metadata or {}).get('cancel_requested')
        ):
            raise EmailIngestionCancelled('Email ingestion cancelled from the live queue.')

    @staticmethod
    def decision_source_metadata_context(run):
        """Return cheap source metadata for the routing/name decision.

        This deliberately reads only the email snapshot. Attachment bytes are
        still deferred until a route is confirmed, so the first pass cannot
        invoke DocProc, OCR, or remote file downloads.
        """
        source = run.source if isinstance(run.source, dict) else {}
        sections = [
            'SOURCE METADATA (filenames and link labels only; no document contents were downloaded).',
            'Treat a descriptive attachment filename or link label as identity evidence. '
            'When it names a company or project, prefer it over a generic forwarded email subject.',
        ]
        for attachment in source.get('attachments') or []:
            name = PurePath(str(attachment.get('name') or '')).name
            if name:
                sections.append(f'--- ATTACHMENT FILENAME: {name} ---')

        soup = BeautifulSoup(str(source.get('body_html') or ''), 'html.parser')
        for anchor in soup.find_all('a'):
            label = ' '.join(anchor.stripped_strings)
            href = str(anchor.get('href') or '').strip()
            if not label and not href:
                continue
            # Keep link metadata bounded and avoid sending query-string tokens.
            parsed = urlparse(href)
            link_name = PurePath(unquote(parsed.path)).name if parsed.path else ''
            details = [f'label={label[:300]}'] if label else []
            if link_name:
                details.append(f'filename={link_name[:200]}')
            if parsed.hostname:
                details.append(f'host={parsed.hostname[:200]}')
            sections.append('--- LINK METADATA: ' + '; '.join(details) + ' ---')
        return '\n'.join(sections) if len(sections) > 2 else ''

    @staticmethod
    def attachment_title_hint(run):
        """Extract a conservative company/project hint from a filename."""
        source = run.source if isinstance(run.source, dict) else {}
        for attachment in source.get('attachments') or []:
            stem = PurePath(str(attachment.get('name') or '')).stem.strip()
            if not stem:
                continue
            # Most deal files use ``Company - document type - qualifier``.
            candidate = stem.split(' - ', 1)[0].strip(' _–—-')
            if len(candidate.split()) >= 2 and len(candidate) >= 4:
                return candidate[:200]
        return ''

    @classmethod
    def prefer_attachment_title(cls, run, initialization):
        """Guard against a generic subject overriding a stronger filename identity."""
        if not isinstance(initialization, dict):
            return initialization
        model_data = initialization.get('deal_model_data')
        if not isinstance(model_data, dict):
            return initialization
        hint = cls.attachment_title_hint(run)
        current = str(model_data.get('title') or '').strip()
        subject = Decisions._clean_deal_title(run.email.subject)
        if not hint or not current or current.casefold() != subject.casefold():
            return initialization
        return {
            **initialization,
            'deal_model_data': {**model_data, 'title': hint},
            'metadata': {
                **(initialization.get('metadata') or {}),
                'attachment_title_hint': hint,
            },
        }

    @staticmethod
    def _audit_log_for_run(run):
        audit_id = (run.source or {}).get('_audit_log_id') if isinstance(run.source, dict) else None
        if not audit_id:
            return None
        from ai_orchestrator.models import AIAuditLog
        return AIAuditLog.objects.filter(pk=audit_id).first()

    @classmethod
    def ensure_audit_log(cls, run, *, requested_by=None):
        """Create one durable, user-visible audit row for an ingestion run."""
        from django.db import transaction
        from ai_orchestrator.services.runtime import AIRuntimeService

        with transaction.atomic():
            locked_run = EmailIngestionRun.objects.select_for_update().select_related('email').get(pk=run.pk)
            existing = cls._audit_log_for_run(locked_run)
            if existing:
                run.source = locked_run.source
                return existing

            audit = AIRuntimeService.create_audit_log(
                source_type='email_ingestion',
                source_id=str(locked_run.email_id),
                context_label=f'Email ingestion: {locked_run.email.subject or "Untitled email"}',
                status='PENDING',
                is_success=False,
                system_prompt='Capture, classify, route, and index email evidence.',
                user_prompt=f'Queue evidence ingestion for email {locked_run.email_id}.',
                source_metadata={
                    'run_id': str(locked_run.id),
                    'input_version': locked_run.input_version,
                    'workflow': 'email_evidence',
                },
                requested_by=requested_by,
            )
            locked_run.source = {**(locked_run.source or {}), '_audit_log_id': str(audit.id)}
            locked_run.save(update_fields=['source', 'updated_at'])
            run.source = locked_run.source
            return audit

    @classmethod
    def _sync_audit_log(cls, run, *, task_id=None):
        """Mirror durable run state into the AI audit ledger without blocking ingestion."""
        audit = cls._audit_log_for_run(run)
        if not audit:
            return

        from ai_orchestrator.services.realtime import broadcast_audit_log_update

        terminal = run.status in ('completed', 'needs_review', 'failed', 'cancelled')
        if run.status in ('failed', 'cancelled'):
            status = 'FAILED'
        elif run.status in ('completed', 'needs_review'):
            status = 'COMPLETED'
        else:
            status = 'PROCESSING'
        now = timezone.now()
        audit.status = status
        audit.is_success = run.status == 'completed'
        audit.error_message = run.error or ('Manual review required.' if run.status == 'needs_review' else '')
        audit.source_metadata = {
            **(audit.source_metadata or {}),
            'run_id': str(run.id),
            'run_status': run.status,
            'stages': run.stages or {},
            'match': run.match or {},
        }
        if task_id:
            audit.celery_task_id = str(task_id)
        if terminal:
            audit.completed_at = now
            audit.parsed_json = {
                'run_id': str(run.id),
                'status': run.status,
                'stages': run.stages or {},
                'classification': run.classification or {},
                'match': run.match or {},
            }
        update_fields = [
            'status', 'is_success', 'error_message', 'source_metadata',
            'celery_task_id',
        ]
        if terminal:
            update_fields += ['completed_at', 'parsed_json']
        audit.save(update_fields=update_fields)
        try:
            broadcast_audit_log_update(audit, event_type='terminal' if terminal else 'snapshot', done=terminal)
        except Exception:
            # Redis is optional for correctness. The history API still exposes the row.
            logger.warning('Could not broadcast email ingestion audit update for %s', audit.id, exc_info=True)

    @classmethod
    def requeue_orphaned(cls, run_id):
        """Release a legacy inline run that has no live Celery task."""
        from django.db import transaction

        with transaction.atomic():
            run = EmailIngestionRun.objects.select_for_update().get(pk=run_id)
            if run.status != 'running':
                return run
            audit = cls._audit_log_for_run(run)
            if audit and audit.status in ('PENDING', 'PROCESSING') and audit.celery_task_id:
                return run
            run.status = 'pending'
            run.lease_until = None
            run.lease_token = None
            run.error = 'Recovered an interrupted web-request ingestion.'
            run.next_attempt_at = None
            run.save(update_fields=['status', 'lease_until', 'lease_token', 'error', 'next_attempt_at', 'updated_at'])
            return run

    @classmethod
    def start(cls, email, *, requested_by=None):
        """Start ingestion asynchronously and return its run and audit row."""
        run = cls.enqueue(email, dispatch=False)
        cls.requeue_orphaned(run.id)
        run.refresh_from_db()
        audit = cls.ensure_audit_log(run, requested_by=requested_by)
        if run.status in ('pending', 'waiting_service', 'failed'):
            cls.dispatch(run.id, audit_log_id=str(audit.id))
        else:
            cls._sync_audit_log(run)
        run.refresh_from_db()
        return run, audit

    @staticmethod
    def extract_attachment_text(content, title, *, allow_remote):
        """Extract the complete attachment locally, with remote OCR as fallback."""
        from ai_orchestrator.services.document_processor import DocumentProcessorService

        ext = Path(title).suffix.lower()
        extraction = None
        if ext in ('.txt', '.csv', '.md', '.json', '.xml'):
            text = content.decode('utf-8-sig', errors='replace')
        else:
            processor = DocumentProcessorService()
            extraction = processor.get_evidence_extraction_result(
                content,
                title,
                allow_remote_fallback=allow_remote,
            )
            text = extraction.get('normalized_text') or extraction.get('text') or ''
        return text, extraction

    @classmethod
    def decision_document_context(cls, run, *, allow_remote):
        """Extract captured attachment and linked-file content for routing and naming."""
        sections = []
        occurrences = (
            run.occurrences.filter(blob__isnull=False)
            .filter(Q(source_key__startswith='attachment:') | Q(source_key__startswith='link:'))
            .select_related('blob')
            .order_by('position', 'id')
        )
        for occurrence in occurrences:
            metadata = occurrence.metadata if isinstance(occurrence.metadata, dict) else {}
            title = metadata.get('name') or 'Attached document'
            try:
                content = occurrence.blob.read_bytes()
                text, _extraction = cls.extract_attachment_text(
                    content,
                    title,
                    allow_remote=allow_remote,
                )
            except Exception as exc:
                logger.warning('Could not prepare %s for email routing: %s', title, exc)
                text = ''
            sections.append(
                f'--- CAPTURED DOCUMENT: {title} ---\n{text.strip()}'
                if text.strip()
                else f'--- CAPTURED DOCUMENT: {title} ---'
            )
        return '\n\n'.join(sections)

    @staticmethod
    def enqueue(email, *, dispatch=True):
        run = Evidence.snapshot(email)
        if dispatch and run.status in ('pending', 'waiting_service', 'failed'):
            transaction.on_commit(lambda: EmailIngestionService.dispatch(run.id))
        return run

    @staticmethod
    def dispatch(run_id, *, audit_log_id=None, countdown=None):
        from microsoft.tasks import ingest_email_evidence
        try:
            result = ingest_email_evidence.apply_async(
                args=[str(run_id)], queue='low_priority', countdown=countdown, retry=False,
            )
            if audit_log_id:
                from ai_orchestrator.models import AIAuditLog
                AIAuditLog.objects.filter(pk=audit_log_id).update(celery_task_id=str(result.id))
            return result
        except Exception:
            # The run is the outbox. Reconciliation retries publication.
            logger.warning('Email ingestion dispatch pending for %s', run_id, exc_info=True)

    @staticmethod
    def text_available():
        from ai_orchestrator.services.llm_providers import VLLMProviderService
        return bool(VLLMProviderService().get_available_models())

    @staticmethod
    def claim(run_id):
        now = timezone.now()
        with transaction.atomic():
            run = EmailIngestionRun.objects.select_for_update().select_related('email__email_account').get(pk=run_id)
            # Serialize different versions of one email as well as duplicate jobs.
            Email.objects.select_for_update().get(pk=run.email_id)
            if run.status in ('completed', 'needs_review', 'superseded', 'cancelled'):
                return None
            if EmailIngestionRun.objects.filter(email_id=run.email_id, lease_until__gt=now).exists():
                return None
            if run.next_attempt_at and run.next_attempt_at > now:
                return None
            if EmailIngestionRun.objects.filter(email_id=run.email_id, created_at__gt=run.created_at).exists():
                run.status = 'superseded'
                run.save(update_fields=['status'])
                return None
            run.lease_token = uuid.uuid4()
            # Longer than the Celery hard limit; a killed worker cannot outlive its lease.
            run.lease_until = now + timedelta(minutes=35)
            run.status = 'running'
            run.attempts += 1
            run.source = {
                **(run.source or {}),
                '_worker_instance_id': os.getenv('RAILWAY_DEPLOYMENT_ID') or '',
            }
            run.save()
            return run

    @classmethod
    def process(cls, run_id, *, use_ai=None, task_id=None, stop_after_decision=False):
        run = cls.claim(run_id)
        if run is None:
            return {'status': 'not_claimed'}
        cls._sync_audit_log(run, task_id=task_id)
        try:
            cls._raise_if_cancelled(run)
            available = cls.text_available() if use_ai is None else use_ai
            parts = Evidence.parts(run)
            if (
                not run.classification
                or run.classification.get('status') == 'waiting_service'
                or not run.match
                or run.match.get('status') not in ('matched',)
            ):
                # The first pass gets only cheap filename/link metadata. Do not
                # read attachment bytes or call DocProc until the route is
                # confirmed and the artifact stage starts.
                decision_document_context = cls.decision_source_metadata_context(run)
                confirmed_classification = (
                    dict(run.classification)
                    if run.classification.get('method') == 'manual'
                    else None
                )
                confirmed_match = dict(run.match) if run.match.get('status') == 'matched' else None
                routing = Decisions.route(
                    run.email,
                    parts,
                    Deal.objects.all(),
                    use_ai=available,
                    supplemental_text=decision_document_context,
                )
                run.classification = confirmed_classification or routing['classification']
                run.match = confirmed_match or routing['match']
                if available and not run.match.get('deal_id') and not run.match.get('suggested_deal_id'):
                    try:
                        initialization = Decisions.initialize(
                            run.email,
                            parts,
                            supplemental_text=decision_document_context,
                        )
                    except ValueError as exc:
                        # Not every unresolved message is a deal. Preserve the
                        # review route instead of turning ambiguity into a job failure.
                        logger.info('No reviewable deal seed for email run %s: %s', run.id, exc)
                    else:
                        run.match = {**run.match, 'initialization': cls.prefer_attachment_title(run, initialization)}
            run.stages['classification'] = run.classification.get('status', 'completed')
            run.stages['match'] = run.match['status']
            run.save(update_fields=['classification', 'match', 'stages', 'updated_at'])
            if stop_after_decision and run.stages.get('review') == 'pending':
                run.status = 'needs_review' if available or run.match.get('candidates') else 'waiting_service'
                return cls.release(run)
            if run.match['status'] != 'matched':
                run.status = 'needs_review' if available or run.match.get('candidates') else 'waiting_service'
                return cls.release(run)
            # A fresh automatic route is the review checkpoint. Stop here even
            # for an existing-deal match so the first request never processes
            # every attachment just to discover the deal name.
            if stop_after_decision and run.stages.get('review') != 'confirmed':
                run.stages['review'] = 'pending'
                run.save(update_fields=['stages', 'updated_at'])
                run.status = 'needs_review' if available or run.match.get('candidates') or run.match.get('status') == 'matched' else 'waiting_service'
                return cls.release(run)
            if run.classification.get('status') == 'waiting_service':
                run.status = 'waiting_service'
                return cls.release(run)
            # Artifact processing starts only after the route has been confirmed.
            cls._raise_if_cancelled(run)
            attachment_failures = Evidence.save_attachments(run)
            link_failures = Evidence.save_links(run)
            deal = Deal.objects.get(pk=run.match['deal_id'])
            # The first routing pass is authoritative. Only align segment count
            # after known contributions have been unfolded for this deal.
            parts = Evidence.parts(run, deal)
            classification = dict(run.classification)
            roles = list(classification.get('segment_roles') or [])
            if len(roles) != len(parts):
                classification['segment_roles'] = [
                    {
                        'type': classification['type'],
                        'method': classification.get('method', 'confirmed_route'),
                        'evidence': classification.get('evidence', ''),
                    }
                    for _part in parts
                ]
            run.classification = classification
            Evidence.save_parts(run, deal, parts, classification)
            attachment_failures = Evidence.save_attachments(run, deal)
            link_failures = Evidence.save_links(run, deal)
            # External links are supplementary evidence. Record their failures,
            # but do not block a valid email body or attachment from advancing.
            capture_failures = attachment_failures
            run.stages['links'] = 'partial' if link_failures else 'completed'
            run.stages['save'] = 'partial' if capture_failures else 'completed'
            run.save(update_fields=['classification', 'stages', 'updated_at'])
            indexing = cls.index_outputs(run, allow_remote=available)
            run.stages['index'] = 'completed' if indexing else 'waiting_service'
            run.status = 'completed' if indexing and not capture_failures else 'waiting_service'
            return cls.release(run)
        except EmailIngestionCancelled as exc:
            run.error = str(exc)
            run.status = 'cancelled'
            return cls.release(run)
        except EmailDecisionUnavailable as exc:
            run.error = str(exc)[:1500]
            run.status = 'waiting_service'
            return cls.release(run)
        except ValueError as exc:
            run.error = str(exc)[:1500]
            run.status = 'needs_review'
            return cls.release(run)
        except Exception as exc:
            logger.exception('Email ingestion failed for run %s', run.id)
            run.error = str(exc)[:1500]
            run.status = 'failed'
            return cls.release(run)

    @staticmethod
    def release(run):
        run.lease_until = None
        run.lease_token = None
        delay = min(3600, 30 * 2 ** min(run.attempts, 7))
        run.next_attempt_at = timezone.now() + timedelta(seconds=delay) if run.status in ('waiting_service', 'failed') else None
        run.save()
        EmailIngestionService._sync_audit_log(run)
        Email.objects.filter(pk=run.email_id).update(
            is_indexed=run.stages.get('index') == 'completed',
            processing_status='completed' if run.status == 'completed' else ('failed' if run.status in ('failed', 'cancelled') else 'pending'),
            processing_error=run.error or None)
        # Do not depend solely on a separately deployed Celery Beat service.
        # A transient VM outage schedules its own durable retry; the run lease
        # and claim checks prevent overlapping processing.
        if run.status in ('waiting_service', 'failed'):
            try:
                from microsoft.tasks import ingest_email_evidence
                ingest_email_evidence.apply_async(
                    args=[str(run.id)],
                    queue='low_priority',
                    countdown=delay,
                    retry=False,
                )
            except Exception:
                logger.exception('Could not schedule recovery for email run %s', run.id)
        return {'run_id': str(run.id), 'status': run.status}

    @staticmethod
    def index_outputs(run, *, allow_remote):
        from ai_orchestrator.models import DocumentChunk
        from ai_orchestrator.services.embedding_processor import EmbeddingService
        from deals.services.document_artifacts import DocumentArtifactService
        service = EmbeddingService()
        embedding_ready = service.is_embedding_available(timeout=1)
        ready = True
        links = EmailEvidenceLink.objects.filter(occurrences__run=run, active=True).select_related('document', 'meeting_note', 'blob').distinct()
        # Canonical outputs replace the legacy whole-email vector. Keeping both
        # would double-count the same body in global and deal chat.
        if links.exists():
            DocumentChunk.objects.filter(source_type='email', source_id=str(run.email_id)).delete()
        for link in links:
            try:
                EmailIngestionService._raise_if_cancelled(run)
                doc = link.document
                if doc and link.blob_id and not (doc.normalized_text or '').strip():
                    content = link.blob.read_bytes()
                    text, extraction = EmailIngestionService.extract_attachment_text(
                        content, doc.title, allow_remote=allow_remote,
                    )
                    if not text.strip():
                        raise RuntimeError('Text extraction pending: OCR, unsupported format, or unreadable file.')
                    doc.extracted_text = text
                    doc.normalized_text = text
                    doc.transcription_status = (
                        extraction.get('transcription_status') if extraction else 'complete'
                    ) or 'complete'
                    if extraction:
                        doc.extraction_mode = extraction.get('mode') or doc.extraction_mode
                    doc.save(update_fields=['extracted_text', 'normalized_text', 'transcription_status', 'extraction_mode'])
                if doc:
                    link.index_status = 'creating_artifact'
                    link.error = ''
                    link.save(update_fields=['index_status', 'error'])
                    artifact = DocumentArtifactService.ensure_document_artifact(doc)
                    if DocumentArtifactService.artifact_status(artifact) != DocumentArtifactService.STATUS_COMPLETE:
                        raise RuntimeError('Detailed document artifact is incomplete and will be retried.')
                if not embedding_ready:
                    raise RuntimeError('Embedding service unavailable; saved evidence will be retried.')
                # A meeting contribution has both records; vectorize only the
                # canonical DealDocument to prevent duplicate retrieval hits.
                success = service.vectorize_document(doc) if doc else service.vectorize_meeting_note(link.meeting_note)
                source_type = 'document' if doc else 'meeting_note'
                source_id = str(doc.id if doc else link.meeting_note_id)
                chunks = DocumentChunk.objects.filter(source_type=source_type, source_id=source_id, deal_id=link.deal_id)
                # Existing vectorizers can create unembedded chunks. They are not success.
                if not success or not chunks.exists() or chunks.filter(embedding__isnull=True).exists():
                    if doc:
                        type(doc).objects.filter(pk=doc.pk).update(is_indexed=False)
                    raise RuntimeError('Embedding incomplete; output will be retried.')
                link.index_status = 'completed'
                link.error = ''
                link.provenance = {
                    **(link.provenance or {}),
                    'artifact_status': DocumentArtifactService.artifact_status(doc) if doc else 'not_applicable',
                    'transcription_status': doc.transcription_status if doc else 'complete',
                    'chunking_status': doc.chunking_status if doc else 'chunked',
                    'chunk_count': chunks.count(),
                }
            except Exception as exc:
                link.index_status = 'waiting_service'
                link.error = str(exc)[:1500]
                ready = False
            link.save(update_fields=['index_status', 'error', 'provenance'])
        run.stages['artifacts'] = 'completed' if ready else 'waiting_service'
        run.stages['chunks'] = 'completed' if ready else 'waiting_service'
        run.save(update_fields=['stages', 'updated_at'])
        return ready

    @classmethod
    def reconcile(cls, limit=50):
        if not getattr(settings, 'EMAIL_INGESTION_ENABLED', False):
            return 0
        now = timezone.now()
        cls._recover_replaced_worker_runs(now=now)
        ids = list(EmailIngestionRun.objects.filter(status__in=['pending', 'running', 'failed', 'waiting_service'])
            .filter(Q(lease_until=None) | Q(lease_until__lte=now))
            .filter(Q(next_attempt_at=None) | Q(next_attempt_at__lte=now))
            .order_by('updated_at').values_list('id', flat=True)[:limit])
        for run_id in ids:
            cls.dispatch(run_id)
        return len(ids)

    @staticmethod
    def _observed_task_ids() -> set[str]:
        try:
            from config.celery import app as celery_app

            inspector = celery_app.control.inspect(timeout=1)
            observed = set()
            for payload in (inspector.active() or {}, inspector.reserved() or {}, inspector.scheduled() or {}):
                for tasks in payload.values():
                    for task in tasks or []:
                        task_data = task.get('request') or task
                        if task_data.get('id'):
                            observed.add(str(task_data['id']))
            return observed
        except Exception:
            return set()

    @classmethod
    def _recover_replaced_worker_runs(cls, *, now=None) -> int:
        """Fence leases left by a replaced Railway worker and requeue them."""
        from ai_orchestrator.models import AIAuditLog

        now = now or timezone.now()
        current = cache.get('vdr:presence:worker:current')
        if not isinstance(current, dict) or not current.get('instance_id'):
            return 0
        current_worker_id = str(current['instance_id'])
        current_started_at = float(current.get('started_at') or 0)
        handoff_elapsed = (
            time.time() - current_started_at
            >= int(getattr(settings, 'VDR_DEPLOYMENT_HANDOFF_SECONDS', 90))
        )
        observed_task_ids = cls._observed_task_ids()
        recovered = 0
        candidates = EmailIngestionRun.objects.filter(status='running', lease_until__gt=now)
        for candidate in candidates:
            source = candidate.source or {}
            owner_worker_id = str(source.get('_worker_instance_id') or '')
            audit = cls._audit_log_for_run(candidate)
            task_id = str(audit.celery_task_id or '') if audit else ''
            replaced_owner = bool(
                owner_worker_id
                and owner_worker_id != current_worker_id
                and handoff_elapsed
                and task_id not in observed_task_ids
            )
            legacy_orphan = bool(
                not owner_worker_id
                and current_started_at > candidate.updated_at.timestamp()
                and task_id
                and task_id not in observed_task_ids
            )
            if not replaced_owner and not legacy_orphan:
                continue
            with transaction.atomic():
                run = EmailIngestionRun.objects.select_for_update().get(pk=candidate.pk)
                if run.status != 'running' or not run.lease_until or run.lease_until <= now:
                    continue
                previous_task_id = task_id
                run.status = 'pending'
                run.lease_until = None
                run.lease_token = None
                run.next_attempt_at = None
                run.error = 'Recovered email ingestion after worker redeploy.'
                run.source = {
                    **(run.source or {}),
                    '_worker_instance_id': '',
                    '_deployment_recovery_count': int((run.source or {}).get('_deployment_recovery_count') or 0) + 1,
                }
                run.save()
                if previous_task_id:
                    AIAuditLog.objects.filter(
                        source_type='document_evidence_segment',
                        celery_task_id=previous_task_id,
                        status__in=['PENDING', 'PROCESSING'],
                    ).update(
                        status='FAILED', is_success=False, completed_at=now,
                        error_message='Inference workflow was superseded after worker redeploy.',
                    )
                cls._sync_audit_log(run)
                recovered += 1
        return recovered
