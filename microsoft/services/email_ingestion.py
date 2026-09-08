"""Versioned email jobs with durable pending state and independent evidence indexing."""
from datetime import timedelta
import logging
from pathlib import Path
import uuid

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from deals.models import Deal
from microsoft.models import Email, EmailIngestionRun, EmailEvidenceLink
from .email_evidence import EmailEvidenceService as Evidence
from .email_matching import EmailDecisionService as Decisions

logger = logging.getLogger(__name__)


class EmailIngestionService:
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
            extraction = processor.get_chat_extraction_result(content, title)
            text = extraction.get('normalized_text') or extraction.get('text') or ''
            if not text.strip() and allow_remote:
                extraction = processor.get_extraction_result(content, title)
                text = extraction.get('normalized_text') or extraction.get('text') or ''
        return text, extraction

    @staticmethod
    def enqueue(email):
        run = Evidence.snapshot(email)
        if run.status in ('pending', 'waiting_service', 'failed'):
            transaction.on_commit(lambda: EmailIngestionService.dispatch(run.id))
        return run

    @staticmethod
    def dispatch(run_id):
        from microsoft.tasks import ingest_email_evidence
        try:
            ingest_email_evidence.apply_async(args=[str(run_id)], queue='low_priority', retry=False)
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
            if run.status in ('completed', 'needs_review', 'superseded'):
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
            run.save()
            return run

    @classmethod
    def process(cls, run_id, *, use_ai=None):
        run = cls.claim(run_id)
        if run is None:
            return {'status': 'not_claimed'}
        try:
            available = cls.text_available() if use_ai is None else use_ai
            # Capture files before decisions so review or VM outages cannot lose originals.
            attachment_failures = Evidence.save_attachments(run)
            parts = Evidence.parts(run)
            if not run.classification or run.classification.get('status') == 'waiting_service':
                run.classification = Decisions.classify(parts, source_id=run.email_id, use_ai=available)
            run.stages['classification'] = run.classification.get('status', 'completed')
            text = (run.source.get('subject') or '') + '\n' + '\n\n'.join(p.text for p in parts)
            if run.match.get('status') != 'matched':
                run.match = Decisions.match(run.email, text, Deal.objects.all(), use_ai=available)
            run.stages['match'] = run.match['status']
            run.save(update_fields=['classification', 'match', 'stages', 'updated_at'])
            if run.match['status'] != 'matched':
                run.status = 'needs_review' if available or run.match.get('candidates') else 'waiting_service'
                return cls.release(run)
            if run.classification.get('status') == 'waiting_service':
                run.status = 'waiting_service'
                return cls.release(run)
            deal = Deal.objects.get(pk=run.match['deal_id'])
            # Reclassify the canonical segmentation only when not manually overridden.
            parts = Evidence.parts(run, deal)
            if run.classification.get('method') == 'manual':
                classification = {**run.classification, 'segment_roles': []}
            else:
                classification = Decisions.classify(parts, source_id=run.email_id, use_ai=available)
            if classification.get('status') == 'waiting_service':
                run.status = 'waiting_service'
                return cls.release(run)
            run.classification = classification
            Evidence.save_parts(run, deal, parts, classification)
            attachment_failures = Evidence.save_attachments(run, deal)
            run.stages['save'] = 'partial' if attachment_failures else 'completed'
            run.save(update_fields=['classification', 'stages', 'updated_at'])
            indexing = cls.index_outputs(run, allow_remote=available)
            run.stages['index'] = 'completed' if indexing else 'waiting_service'
            run.status = 'completed' if indexing and not attachment_failures else 'waiting_service'
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
        Email.objects.filter(pk=run.email_id).update(
            is_indexed=run.stages.get('index') == 'completed',
            processing_status='completed' if run.status == 'completed' else ('failed' if run.status == 'failed' else 'pending'),
            processing_error=run.error or None)
        return {'run_id': str(run.id), 'status': run.status}

    @staticmethod
    def index_outputs(run, *, allow_remote):
        from ai_orchestrator.models import DocumentChunk
        from ai_orchestrator.services.embedding_processor import EmbeddingService
        service = EmbeddingService()
        embedding_ready = service.is_embedding_available(timeout=1)
        ready = True
        links = EmailEvidenceLink.objects.filter(occurrences__run=run, active=True).select_related('document', 'meeting_note', 'blob').distinct()
        # Canonical outputs replace the legacy whole-email vector. Keeping both
        # would double-count the same body in global and deal chat.
        if links.exists():
            DocumentChunk.objects.filter(source_type='email', source_id=str(run.email_id)).delete()
        for link in links:
            if link.index_status == 'completed':
                continue
            try:
                doc = link.document
                if doc and link.blob_id and not (doc.normalized_text or '').strip():
                    with link.blob.file.open('rb') as stream:
                        content = stream.read()
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
                if not embedding_ready:
                    raise RuntimeError('Embedding service unavailable; saved evidence will be retried.')
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
            except Exception as exc:
                link.index_status = 'waiting_service'
                link.error = str(exc)[:1500]
                ready = False
            link.save(update_fields=['index_status', 'error'])
        return ready

    @classmethod
    def reconcile(cls, limit=50):
        if not getattr(settings, 'EMAIL_INGESTION_ENABLED', False):
            return 0
        now = timezone.now()
        ids = list(EmailIngestionRun.objects.filter(status__in=['pending', 'running', 'failed', 'waiting_service'])
            .filter(Q(lease_until=None) | Q(lease_until__lte=now))
            .filter(Q(next_attempt_at=None) | Q(next_attempt_at__lte=now))
            .order_by('updated_at').values_list('id', flat=True)[:limit])
        for run_id in ids:
            cls.dispatch(run_id)
        return len(ids)
