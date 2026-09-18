"""Shared post-index deal-field synthesis for email and OneDrive ingestion."""

from __future__ import annotations

import json

from django.db import transaction

from ai_orchestrator.prompt_contracts import DEAL_FIELD_SYNTHESIS_JSON_SCHEMA
from ai_orchestrator.services.ai_processor import AIProcessorService
from ai_orchestrator.services.embedding_processor import EmbeddingService
from deals.models import AnalysisKind, Deal, DealAnalysis, DealDocument
from deals.services.deal_creation import DealCreationService
from deals.services.document_artifacts import DocumentArtifactService


class DealFieldSynthesisService:
    MAX_CONTEXT_CHARS = 160_000
    TEXT_PER_DOCUMENT = 16_000

    @classmethod
    def _document_payload(cls, document: DealDocument, remaining: int) -> dict:
        artifact = DocumentArtifactService.artifact_from_document(document)
        artifact.pop("normalized_text", None)
        artifact.pop("reasoning", None)
        text = (document.normalized_text or document.extracted_text or "").strip()
        excerpt_limit = max(0, min(cls.TEXT_PER_DOCUMENT, remaining))
        return {
            "document_id": str(document.id),
            "name": document.title,
            "document_type": document.document_type,
            "transcription_status": document.transcription_status,
            "artifact": artifact,
            "text_excerpt": text[:excerpt_limit],
            "text_truncated": len(text) > excerpt_limit,
        }

    @classmethod
    def _evidence_context(cls, documents: list[DealDocument]) -> str:
        payload = []
        remaining = cls.MAX_CONTEXT_CHARS
        for document in documents:
            item = cls._document_payload(document, remaining)
            serialized = json.dumps(item, ensure_ascii=False, default=str)
            if len(serialized) > remaining and payload:
                break
            payload.append(item)
            remaining -= min(len(serialized), remaining)
        return json.dumps(payload, ensure_ascii=False, default=str)

    @staticmethod
    def _existing_deal_payload(deal: Deal) -> dict:
        return {
            "title": deal.title,
            "industry": deal.industry,
            "sector": deal.sector,
            "funding_ask": deal.funding_ask,
            "funding_ask_for": deal.funding_ask_for,
            "priority": deal.priority,
            "city": deal.city,
            "state": deal.state,
            "country": deal.country,
            "themes": deal.themes,
            "bank": deal.bank.name if deal.bank_id else deal.bank_name,
            "primary_contact": deal.primary_contact.email if deal.primary_contact_id else deal.primary_contact_name,
            "ia_team": list(deal.responsibility.values_list("email", flat=True)),
        }

    @classmethod
    def synthesize(
        cls,
        deal: Deal,
        *,
        batch_key: str,
        source_type: str,
        required_document_ids: list[str] | None = None,
    ) -> DealAnalysis:
        """Fill supported fields once a source batch has durable indexed evidence."""
        existing = DealAnalysis.objects.filter(
            deal=deal,
            analysis_json__metadata__field_synthesis_key=batch_key,
        ).order_by("-created_at").first()
        if existing:
            return existing

        required_ids = {str(value) for value in required_document_ids or [] if value}
        required_documents = list(deal.documents.filter(id__in=required_ids)) if required_ids else []
        found_ids = {str(document.id) for document in required_documents}
        if required_ids != found_ids:
            raise ValueError("Deal field synthesis cannot start because a source document is missing.")
        not_indexed = [document.title for document in required_documents if not document.is_indexed]
        if not_indexed:
            raise ValueError(
                "Deal field synthesis cannot start before indexing completes: "
                + ", ".join(not_indexed)
            )

        documents = list(deal.documents.filter(is_indexed=True).order_by("created_at", "id"))
        if not documents:
            raise ValueError("Deal field synthesis requires at least one indexed document.")

        result = AIProcessorService().process_content(
            content=cls._evidence_context(documents),
            skill_name="deal_field_synthesis",
            source_type="deal_field_synthesis",
            source_id=str(deal.id),
            metadata={
                "existing_deal_json": json.dumps(
                    cls._existing_deal_payload(deal), ensure_ascii=False, default=str,
                ),
                "batch_key": batch_key,
                "ingestion_source_type": source_type,
                "temperature": 0.0,
                "max_tokens": 8192,
                "enforce_context_budget": True,
                "chat_template_kwargs": {"enable_thinking": False},
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {
                        "name": "deal_field_synthesis",
                        "schema": DEAL_FIELD_SYNTHESIS_JSON_SCHEMA,
                        "strict": True,
                    },
                },
            },
        )
        if not isinstance(result, dict) or result.get("error"):
            raise ValueError(
                (result or {}).get("error", "Deal field synthesis returned no structured result.")
                if isinstance(result, dict)
                else "Deal field synthesis returned no structured result."
            )

        result = dict(result)
        model_data = dict(result.get("deal_model_data") or {})
        if not deal.deal_summary and str(model_data.get("deal_summary") or "").strip():
            result["analyst_report"] = str(model_data["deal_summary"]).strip()
        result.setdefault("metadata", {}).update({
            "field_synthesis_key": batch_key,
            "field_synthesis_source_type": source_type,
            "documents_analyzed": [document.title for document in documents],
            "analysis_input_files": [
                {"document_id": str(document.id), "file_name": document.title}
                for document in documents
            ],
        })

        with transaction.atomic():
            deal = Deal.objects.select_for_update().get(pk=deal.pk)
            existing = DealAnalysis.objects.filter(
                deal=deal,
                analysis_json__metadata__field_synthesis_key=batch_key,
            ).order_by("-created_at").first()
            if existing:
                return existing
            latest = deal.analyses.order_by("-version", "-created_at").first()
            analysis_kind = AnalysisKind.SUPPLEMENTAL if latest else AnalysisKind.INITIAL
            previous_snapshot = (
                (latest.analysis_json or {}).get("canonical_snapshot", {})
                if latest and isinstance(latest.analysis_json, dict)
                else {}
            )
            normalized = DealCreationService.normalize_analysis_payload(
                result,
                previous_snapshot=previous_snapshot,
                analysis_kind=analysis_kind,
                documents_analyzed=[document.title for document in documents],
                analysis_input_files=result["metadata"]["analysis_input_files"],
                failed_files=[],
            )
            analysis = DealAnalysis.objects.create(
                deal=deal,
                version=(latest.version + 1) if latest else 1,
                analysis_kind=analysis_kind,
                thinking=str(result.get("thinking") or ""),
                ambiguities=normalized.get("metadata", {}).get("ambiguous_points", []),
                analysis_json=normalized,
            )
            DealCreationService.apply_analysis_to_deal(
                deal,
                normalized,
                overwrite=False,
                overwrite_themes=True,
                source_id=f"field-synthesis:{analysis.id}",
                overwrite_ai_owned=True,
            )
            deal.documents.filter(id__in=[document.id for document in documents]).update(
                is_ai_analyzed=True,
            )

        embedder = EmbeddingService()
        embedder.vectorize_deal(deal)
        embedder.refresh_deal_profile(deal)
        return analysis
