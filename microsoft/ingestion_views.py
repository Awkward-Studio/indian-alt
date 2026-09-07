"""Actions shared by the existing authenticated email viewset."""
import io
from pathlib import PurePath

from django.conf import settings
from django.core.exceptions import ValidationError
from django.http import FileResponse
from django.shortcuts import get_object_or_404
from rest_framework.decorators import action
from rest_framework.response import Response

from deals.models import Deal
from .models import EmailIngestionRun
from .services.email_ingestion import EmailIngestionService
from .services.email_ingestion_review import confirm_decision, run_status, StaleEmailDecision


class EmailIngestionActions:
    @action(detail=True, methods=['get', 'post'])
    def ingestion(self, request, pk=None):
        email = self.get_object()
        enabled = getattr(settings, 'EMAIL_INGESTION_ENABLED', False)
        if request.method == 'POST':
            if not enabled:
                return Response({'error': 'Email ingestion is not enabled.'}, status=503)
            run = EmailIngestionService.enqueue(email)
            # Process inline so embedding search, reranking, and LLM deal selection execute immediately
            EmailIngestionService.process(run.id)
            run.refresh_from_db()
        else:
            run = email.ingestion_runs.first()
        return Response({**run_status(run, include_content=True), 'enabled': enabled})

    @action(detail=True, methods=['post'], url_path='ingestion-confirm')
    def ingestion_confirm(self, request, pk=None):
        if not getattr(settings, 'EMAIL_INGESTION_ENABLED', False):
            return Response({'error': 'Email ingestion is not enabled.'}, status=503)
        email = self.get_object()
        try:
            deal_id = request.data.get('deal_id')
            create_new_deal = request.data.get('create_new_deal', False)
            new_deal_title = (request.data.get('new_deal_title') or '').strip()

            if create_new_deal or (new_deal_title and not deal_id):
                title = new_deal_title
                if not title:
                    clean_subject = (email.subject or 'New Deal').strip()
                    for prefix in ['re:', 'fwd:', 'fw:']:
                        if clean_subject.lower().startswith(prefix):
                            clean_subject = clean_subject[len(prefix):].strip()
                    title = clean_subject[:200] or 'New Deal from Email'
                deal = Deal.objects.create(title=title)
            elif deal_id:
                deal = get_object_or_404(Deal, pk=deal_id)
            else:
                return Response({'error': 'Either deal_id or create_new_deal with new_deal_title is required.'}, status=400)

            run_id = request.data.get('run_id')
            revision = request.data.get('expected_revision')
            if not run_id:
                run = email.ingestion_runs.first() or EmailIngestionService.enqueue(email)
                run_id = run.id
                revision = run.revision
            elif not isinstance(revision, int):
                return Response({'error': 'run_id and expected_revision are required.'}, status=400)
            else:
                get_object_or_404(EmailIngestionRun, pk=run_id, email=email)

            run = confirm_decision(run_id, email=email, deal=deal, expected_revision=revision,
                kind=request.data.get('classification'), actor=request.user)
        except StaleEmailDecision as exc:
            return Response({'error': str(exc)}, status=409)
        except (ValueError, TypeError, ValidationError) as exc:
            return Response({'error': str(exc)}, status=400)
        return Response({**run_status(run, include_content=True), 'enabled': True})

    @action(detail=True, methods=['get'], url_path='evidence-source')
    def evidence_source(self, request, pk=None):
        email = self.get_object()
        run = get_object_or_404(EmailIngestionRun, pk=request.query_params.get('run_id'), email=email)
        occurrence = get_object_or_404(
            run.occurrences.select_related('blob', 'contribution'),
            pk=request.query_params.get('occurrence_id'))
        if occurrence.blob_id:
            stream = occurrence.blob.file.open('rb')
            filename = PurePath(occurrence.metadata.get('name') or 'attachment').name
        elif occurrence.contribution_id:
            stream = io.BytesIO(occurrence.contribution.text.encode('utf-8'))
            filename = 'email-message.txt'
        else:
            return Response({'error': 'Source capture is pending.'}, status=409)
        response = FileResponse(stream, as_attachment=True, filename=filename, content_type='application/octet-stream')
        response['Cache-Control'] = 'private, no-store'
        response['X-Content-Type-Options'] = 'nosniff'
        return response
