"""Actions shared by the existing authenticated email viewset."""
import io
from pathlib import PurePath

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.http import FileResponse
from django.shortcuts import get_object_or_404
from rest_framework.decorators import action
from rest_framework.response import Response

from ai_orchestrator.models import AIAuditLog
from deals.models import AnalysisKind, Deal, DealAnalysis
from deals.services.deal_creation import DealCreationService
from .models import Email, EmailIngestionRun
from .services.email_ingestion import EmailIngestionService
from .services.email_ingestion_review import confirm_decision, run_status, StaleEmailDecision


class EmailIngestionActions:
    @staticmethod
    def _hydrate_new_deal_from_latest_analysis(deal, email):
        audit_log = (
            AIAuditLog.objects.filter(
                source_type="email",
                source_id=str(email.id),
                status="COMPLETED",
                is_success=True,
            )
            .exclude(parsed_json=None)
            .order_by("-created_at")
            .first()
        )
        payload = audit_log.parsed_json if audit_log and isinstance(audit_log.parsed_json, dict) else None
        if not payload or not isinstance(payload.get("deal_model_data"), dict):
            return None
        normalized = DealCreationService.normalize_analysis_payload(
            payload,
            analysis_kind=AnalysisKind.INITIAL,
        )
        normalized.setdefault("metadata", {})["audit_log_id"] = str(audit_log.id)
        analysis, _ = DealAnalysis.objects.update_or_create(
            deal=deal,
            version=1,
            defaults={
                "analysis_kind": AnalysisKind.INITIAL,
                "thinking": payload.get("thinking", ""),
                "ambiguities": normalized.get("metadata", {}).get("ambiguous_points", []),
                "analysis_json": normalized,
            },
        )
        DealCreationService.apply_analysis_to_deal(deal, normalized)
        if email.graph_id and not deal.source_email_id:
            deal.source_email_id = email.graph_id
            deal.save(update_fields=["source_email_id"])
        return analysis

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
        try:
            with transaction.atomic():
                email = Email.objects.select_for_update().get(pk=self.get_object().pk)
                deal_id = request.data.get('deal_id')
                create_new_deal = request.data.get('create_new_deal', False)
                new_deal_title = (request.data.get('new_deal_title') or '').strip()

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

                if create_new_deal or (new_deal_title and not deal_id):
                    # Treat a repeated create request for the same email as an
                    # idempotent confirmation instead of leaving an orphan deal.
                    deal = email.deal
                    if not deal:
                        title = new_deal_title
                        if not title:
                            clean_subject = (email.subject or 'New Deal').strip()
                            for prefix in ['re:', 'fwd:', 'fw:']:
                                if clean_subject.lower().startswith(prefix):
                                    clean_subject = clean_subject[len(prefix):].strip()
                            title = clean_subject[:200] or 'New Deal from Email'
                        deal = Deal.objects.create(title=title)
                    if not deal.deal_summary and not deal.analyses.exists():
                        self._hydrate_new_deal_from_latest_analysis(deal, email)
                elif deal_id:
                    deal = get_object_or_404(Deal, pk=deal_id)
                else:
                    return Response({'error': 'Either deal_id or create_new_deal with new_deal_title is required.'}, status=400)

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
