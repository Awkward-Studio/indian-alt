"""Shared post-index deal-field synthesis for email and OneDrive ingestion."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator

from django.db import transaction

from ai_orchestrator.models import AIAuditLog
from ai_orchestrator.prompt_contracts import DEAL_FIELD_SYNTHESIS_JSON_SCHEMA
from ai_orchestrator.services.ai_processor import AIProcessorService
from ai_orchestrator.services.embedding_processor import EmbeddingService
from ai_orchestrator.services.token_budget import ContextBudgetExceeded, estimate_tokens
from deals.models import AnalysisKind, Deal, DealAnalysis, DealDocument, DealFieldProvenance
from deals.services.deal_creation import DealCreationService
from deals.services.document_artifacts import DocumentArtifactService
from deals.services.field_provenance import record_deal_field_changes


class DealFieldSynthesisService:
    # The provider enforces a 65,536-token context window across the complete
    # request. Dense artifact JSON consumes materially more tokens than a
    # simple chars/4 estimate, so leave ample room for the skill prompt,
    # response schema, output, and provider reserve.
    MAX_CONTEXT_CHARS = 72_000
    MAX_CONTEXT_TOKENS = 30_000
    MAX_FRAGMENT_CHARS = 24_000
    STRING_FRAGMENT_CHARS = 6_000
    TEXT_PER_DOCUMENT = 16_000
    MAX_OUTPUT_TOKENS = 4_096
    PLACEHOLDER_TITLES = {"", "deal", "new deal", "unknown", "untitled", "tbd"}
    PROJECT_TITLE_RE = re.compile(r"^project(?:\s|[_-])+", re.IGNORECASE)
    FILENAME_RE = re.compile(
        r"\.(?:csv|doc|docx|eml|msg|pdf|ppt|pptx|xls|xlsb|xlsm|xlsx)$",
        re.IGNORECASE,
    )

    @classmethod
    def _is_placeholder_title(cls, title: str | None, document_titles: set[str]) -> bool:
        normalized = str(title or "").strip()
        if normalized.casefold() in cls.PLACEHOLDER_TITLES:
            return True
        if cls.PROJECT_TITLE_RE.match(normalized) or cls.FILENAME_RE.search(normalized):
            return True

        normalized_folded = normalized.casefold()
        return any(
            normalized_folded in {document_title.casefold(), Path(document_title).stem.casefold()}
            for document_title in document_titles
        )

    @classmethod
    def validated_title_replacement(
        cls,
        deal: Deal,
        model_data: dict,
        metadata: dict,
        document_titles: set[str],
    ) -> str | None:
        if not cls._is_placeholder_title(deal.title, document_titles):
            return None

        latest_title_source = deal.field_provenance.filter(
            field_name="title",
        ).order_by("-created_at", "-id").first()
        initial_email_title = bool(
            latest_title_source
            and latest_title_source.source_type == DealFieldProvenance.SourceType.HUMAN
            and latest_title_source.source_id.startswith("email-ingestion:")
            and latest_title_source.previous_value in (None, "")
        )
        if (
            latest_title_source
            and latest_title_source.source_type == DealFieldProvenance.SourceType.HUMAN
            and not initial_email_title
        ):
            return None

        candidate = str(model_data.get("title") or "").strip()
        title_evidence = metadata.get("title_evidence")
        if not candidate or not isinstance(title_evidence, dict):
            return None
        if title_evidence.get("confidence") != "High":
            return None
        if str(title_evidence.get("title") or "").strip().casefold() != candidate.casefold():
            return None
        if candidate.casefold() == str(deal.title or "").strip().casefold():
            return None
        if cls._is_placeholder_title(candidate, document_titles):
            return None

        source_documents = title_evidence.get("source_documents")
        if not isinstance(source_documents, list):
            return None
        cited_documents = {
            str(value).strip().casefold()
            for value in source_documents
            if str(value).strip()
        }
        known_documents = {title.casefold() for title in document_titles}
        if not cited_documents or not cited_documents.issubset(known_documents):
            return None
        return candidate

    @staticmethod
    def apply_title_replacement(deal: Deal, title: str, *, source_id: str) -> None:
        previous_title = deal.title
        if previous_title == title:
            return
        deal.title = title
        deal.save(update_fields=["title"])
        record_deal_field_changes(
            deal,
            {"title": (previous_title, title)},
            source_type=DealFieldProvenance.SourceType.AI,
            source_id=source_id,
        )

    @classmethod
    def _document_payload(cls, document: DealDocument) -> dict:
        artifact = DocumentArtifactService.artifact_from_document(document)
        # These fields are intentionally excluded from synthesis input: the
        # normalized source is represented by text_excerpt, while reasoning is
        # model-generated scratch work rather than source evidence. The stored
        # artifact itself is never changed.
        artifact.pop("normalized_text", None)
        artifact.pop("reasoning", None)
        text = (document.normalized_text or document.extracted_text or "").strip()
        return {
            "document_id": str(document.id),
            "name": document.title,
            "document_type": document.document_type,
            "transcription_status": document.transcription_status,
            "artifact": artifact,
            "text_excerpt": text[:cls.TEXT_PER_DOCUMENT],
            "text_truncated": len(text) > cls.TEXT_PER_DOCUMENT,
        }

    @classmethod
    def _value_fragments(cls, value: Any, path: str) -> Iterator[dict]:
        serialized = json.dumps(value, ensure_ascii=False, default=str)
        if len(serialized) <= cls.MAX_FRAGMENT_CHARS:
            yield {"evidence_path": path, "value": value}
            return

        if isinstance(value, dict):
            if not value:
                yield {"evidence_path": path, "value": {}}
                return
            for key, nested_value in value.items():
                yield from cls._value_fragments(nested_value, f"{path}.{key}")
            return

        if isinstance(value, list):
            if not value:
                yield {"evidence_path": path, "value": []}
                return
            for index, nested_value in enumerate(value):
                yield from cls._value_fragments(nested_value, f"{path}[{index}]")
            return

        source = str(value)
        parts = [
            source[offset:offset + cls.STRING_FRAGMENT_CHARS]
            for offset in range(0, len(source), cls.STRING_FRAGMENT_CHARS)
        ] or [""]
        for index, part in enumerate(parts):
            yield {
                "evidence_path": path,
                "value": part,
                "string_part": index + 1,
                "string_part_count": len(parts),
            }

    @classmethod
    def _document_fragments(cls, document: DealDocument) -> Iterator[dict]:
        payload = cls._document_payload(document)
        descriptor = {
            "document_id": payload.pop("document_id"),
            "name": payload.pop("name"),
            "document_type": payload.pop("document_type"),
            "transcription_status": payload.pop("transcription_status"),
        }
        for fragment in cls._value_fragments(payload, "document"):
            yield {**descriptor, **fragment}

    @staticmethod
    def _serialize(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))

    @classmethod
    def _pack_contexts(
        cls,
        items: Iterable[dict],
        *,
        phase: str,
        instructions: str,
        item_key: str,
        transform: Callable[[dict], dict] | None = None,
    ) -> list[str]:
        """Pack JSON items in linear time while respecting the exact character cap."""
        batches: list[str] = []
        current: list[dict] = []
        base_context = {"phase": phase, "instructions": instructions, item_key: []}
        base_chars = len(cls._serialize(base_context))
        base_tokens = estimate_tokens(cls._serialize(base_context))
        current_chars = base_chars
        current_tokens = base_tokens

        for source_item in items:
            item = transform(source_item) if transform else source_item
            serialized_item = cls._serialize(item)
            item_chars = len(serialized_item)
            item_tokens = estimate_tokens(serialized_item)
            separator_chars = 1 if current else 0
            separator_tokens = 1 if current else 0
            if current and (
                current_chars + separator_chars + item_chars > cls.MAX_CONTEXT_CHARS
                or current_tokens + separator_tokens + item_tokens > cls.MAX_CONTEXT_TOKENS
            ):
                batches.append(cls._serialize({**base_context, item_key: current}))
                current = [item]
                current_chars = base_chars + item_chars
                current_tokens = base_tokens + item_tokens
            else:
                current.append(item)
                current_chars += separator_chars + item_chars
                current_tokens += separator_tokens + item_tokens
        if current:
            batches.append(cls._serialize({**base_context, item_key: current}))
        return batches

    @classmethod
    def _evidence_batches(cls, documents: list[DealDocument]) -> list[str]:
        """Return bounded contexts without silently dropping evidence or documents."""
        return cls._pack_contexts(
            (
                fragment
                for document in documents
                for fragment in cls._document_fragments(document)
            ),
            phase="evidence_map",
            instructions=(
                "Extract candidate deal fields from these ordered evidence fragments. "
                "Paths and part numbers reconstruct values split across fragments. "
                "Preserve conflicts and do not infer unsupported values."
            ),
            item_key="evidence_fragments",
        )

    @classmethod
    def _candidate_batches(cls, candidates: list[dict]) -> list[str]:
        return cls._pack_contexts(
            candidates,
            phase="candidate_reduce",
            instructions=(
                "Merge these candidate syntheses into one evidence-only result. "
                "Resolve agreement, preserve conflicts as ambiguities, do not invent values, "
                "and list every source document represented by the candidates."
            ),
            item_key="candidates",
            transform=lambda candidate: {
                # Only these structured fields can affect the next merge. The
                # transport response and model scratch work can be very large,
                # and carrying them into another reduction round can overflow
                # the provider context even when the evidence was batched.
                key: candidate.get(key)
                for key in ("deal_model_data", "source_relationships", "metadata")
                if candidate.get(key) is not None
            },
        )

    @classmethod
    def _run_model_adaptive(
        cls,
        processor: AIProcessorService,
        *,
        deal: Deal,
        content: str,
        batch_key: str,
        source_type: str,
        phase: str,
        phase_index: int,
    ) -> list[dict]:
        """Retry an oversized serialized request as smaller logical batches."""
        try:
            return [cls._run_model(
                processor,
                deal=deal,
                content=content,
                batch_key=batch_key,
                source_type=source_type,
                phase=phase,
                phase_index=phase_index,
            )]
        except ContextBudgetExceeded:
            try:
                payload = json.loads(content)
            except (TypeError, ValueError, json.JSONDecodeError):
                raise

            item_key = "evidence_fragments" if phase == "evidence_map" else "candidates"
            items = payload.get(item_key) if isinstance(payload, dict) else None
            if not isinstance(items, list) or len(items) < 2:
                raise

            midpoint = max(1, len(items) // 2)
            child_payloads = (
                {**payload, item_key: items[:midpoint]},
                {**payload, item_key: items[midpoint:]},
            )
            results: list[dict] = []
            for child_payload in child_payloads:
                results.extend(cls._run_model_adaptive(
                    processor,
                    deal=deal,
                    content=cls._serialize(child_payload),
                    batch_key=batch_key,
                    source_type=source_type,
                    phase=phase,
                    phase_index=phase_index,
                ))
            return results

    @classmethod
    def _run_model(
        cls,
        processor: AIProcessorService,
        *,
        deal: Deal,
        content: str,
        batch_key: str,
        source_type: str,
        phase: str,
        phase_index: int,
    ) -> dict:
        existing_deal_json = cls._serialize(cls._existing_deal_payload(deal))
        request_sha = hashlib.sha256(cls._serialize({
            "batch_key": batch_key,
            "phase": phase,
            "phase_index": phase_index,
            "existing_deal_json": existing_deal_json,
            "content": content,
        }).encode("utf-8")).hexdigest()
        completed = AIAuditLog.objects.filter(
            source_type="deal_field_synthesis",
            source_id=str(deal.id),
            status="COMPLETED",
            is_success=True,
            source_metadata__field_synthesis_request_sha=request_sha,
        ).order_by("-created_at").first()
        if completed and isinstance(completed.parsed_json, dict) and not completed.parsed_json.get("error"):
            return dict(completed.parsed_json)

        result = processor.process_content(
            content=content,
            skill_name="deal_field_synthesis",
            source_type="deal_field_synthesis",
            source_id=str(deal.id),
            metadata={
                "existing_deal_json": existing_deal_json,
                "batch_key": batch_key,
                "ingestion_source_type": source_type,
                "temperature": 0.0,
                "max_tokens": cls.MAX_OUTPUT_TOKENS,
                "lossless_input": True,
                "enforce_context_budget": True,
                "chat_template_kwargs": {"enable_thinking": False},
                "context_label": f"{deal.title}: field synthesis {phase} {phase_index + 1}",
                "_source_metadata": {
                    "field_synthesis_key": batch_key,
                    "field_synthesis_phase": phase,
                    "field_synthesis_phase_index": phase_index,
                    "field_synthesis_request_sha": request_sha,
                },
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
        return dict(result)

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

        processor = AIProcessorService()
        evidence_batches = cls._evidence_batches(documents)
        results = [
            result
            for index, content in enumerate(evidence_batches)
            for result in cls._run_model_adaptive(
                processor,
                deal=deal,
                content=content,
                batch_key=batch_key,
                source_type=source_type,
                phase="evidence_map",
                phase_index=index,
            )
        ]
        reduction_round = 0
        while len(results) > 1:
            candidate_batches = cls._candidate_batches(results)
            results = [
                result
                for index, content in enumerate(candidate_batches)
                for result in cls._run_model_adaptive(
                    processor,
                    deal=deal,
                    content=content,
                    batch_key=batch_key,
                    source_type=source_type,
                    phase=f"candidate_reduce_{reduction_round}",
                    phase_index=index,
                )
            ]
            reduction_round += 1
        result = dict(results[0])
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
            replacement_title = cls.validated_title_replacement(
                deal,
                model_data,
                result.get("metadata") or {},
                {document.title for document in documents},
            )
            if replacement_title:
                cls.apply_title_replacement(
                    deal,
                    replacement_title,
                    source_id=f"field-synthesis:{analysis.id}",
                )
            deal.documents.filter(id__in=[document.id for document in documents]).update(
                is_ai_analyzed=True,
            )

        embedder = EmbeddingService()
        embedder.vectorize_deal(deal)
        embedder.refresh_deal_profile(deal)
        return analysis
