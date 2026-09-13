from __future__ import annotations

import json
import hashlib
import logging
import math
from copy import deepcopy
from datetime import date, datetime
from typing import Any, Callable, Optional, TYPE_CHECKING

from langchain_text_splitters import RecursiveCharacterTextSplitter
from django.conf import settings
from django.core.cache import cache

from ai_orchestrator.services.bulk_prompt_contracts import (
    BULK2_INTEL_SYSTEM_PROMPT,
    build_bulk2_segment_prompt,
)
from ai_orchestrator.services.token_budget import estimate_tokens

if TYPE_CHECKING:
    from ai_orchestrator.services.ai_processor import AIProcessorService

logger = logging.getLogger(__name__)


class DocumentArtifactCancelled(RuntimeError):
    """Raised when a caller cancels a segmented artifact build."""


class DocumentArtifactYielded(RuntimeError):
    """Raised between segments when interactive AI work is waiting."""


class DocumentArtifactService:
    """
    Builds and persists normalized document artifacts so deal synthesis can
    operate on structured evidence instead of one giant combined prompt.
    """

    DEFAULT_EVIDENCE_KEYS = {
        "document_name": "",
        "document_type": "Other",
        "document_type_suggestion": {},
        "document_summary": "",
        "claims": [],
        "metrics": [],
        "numeric_evidence": [],
        "table_definitions": [],
        "tables_summary": [],
        "contacts_found": [],
        "risks": [],
        "open_questions": [],
        "diligence_gaps": [],
        "citations": [],
        "industry_overview": {
            "findings": [],
            "market_figures": [],
            "citations": [],
        },
        "reasoning": "",
        "quality_flags": [],
        "normalized_text": "",
        "source_map": {},
        "source_metadata": {},
        "spreadsheet_profile": {},
    }
    REQUIRED_ARTIFACT_KEYS = (
        "document_name",
        "document_type",
        "document_type_suggestion",
        "document_summary",
        "claims",
        "metrics",
        "numeric_evidence",
        "table_definitions",
        "tables_summary",
        "contacts_found",
        "risks",
        "open_questions",
        "diligence_gaps",
        "citations",
        "industry_overview",
        "quality_flags",
        "normalized_text",
        "source_map",
    )
    STATUS_COMPLETE = "complete"
    STATUS_PARTIAL = "partial"
    STATUS_DEGRADED = "degraded"
    STATUS_FAILED = "failed"
    STATUS_MISSING = "missing"
    ARTIFACT_PIPELINE_VERSION = "vdr-bulk2-segment-artifact-v2"

    @staticmethod
    def _segment_artifact_usable(artifact: Any) -> bool:
        if not isinstance(artifact, dict) or not str(artifact.get("document_summary") or "").strip():
            return False
        invalid_flags = {
            "fallback_artifact",
            "artifact_missing_text",
            "artifact_segment_processing_incomplete",
        }
        return not invalid_flags.intersection(artifact.get("quality_flags") or [])

    @classmethod
    def _normalize_segment_artifact(
        cls,
        artifact: dict[str, Any],
        *,
        fallback: dict[str, Any],
    ) -> dict[str, Any]:
        """Normalize segment evidence without requiring duplicated source text."""
        normalized = cls._normalize_artifact(artifact, fallback=fallback)
        normalized["normalized_text"] = ""
        normalized["quality_flags"] = [
            flag
            for flag in normalized.get("quality_flags") or []
            if flag != "artifact_missing_text"
        ]
        return normalized

    @staticmethod
    def _process_segment(service, **kwargs):
        try:
            return service.process_content(**kwargs)
        except Exception as exc:
            # Prompt preparation can fail after audit creation but before the
            # model response handler takes responsibility for terminal state.
            metadata = kwargs["metadata"]["_source_metadata"]
            if metadata.get("artifact_run_id"):
                from ai_orchestrator.models import AIAuditLog
                from django.utils import timezone
                AIAuditLog.objects.filter(
                    source_type="document_evidence_segment", status__in=["PENDING", "PROCESSING"],
                    source_metadata__artifact_segment_cache_key=metadata["artifact_segment_cache_key"],
                ).update(status="FAILED", is_success=False, error_message=str(exc), completed_at=timezone.now())
            raise

    @classmethod
    def prepare_artifact_run_metadata(
        cls,
        extracted_text: str,
        source_metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Freeze every input that determines segment identity for one artifact run."""
        raw_text = (extracted_text or "").strip()
        prepared = json.loads(json.dumps(source_metadata or {}, default=str))
        try:
            from ai_orchestrator.services.runtime import AIRuntimeService

            artifact_model = str(
                prepared.get("artifact_model")
                or AIRuntimeService.get_text_model(AIRuntimeService.get_default_personality())
            )
        except Exception:
            artifact_model = str(
                prepared.get("artifact_model")
                or getattr(settings, "VLLM_MODEL", "default")
            )

        source_token_budget = max(4_000, int(
            prepared.get("artifact_segment_source_token_budget")
            or getattr(settings, "VDR_ARTIFACT_SEGMENT_SOURCE_TOKENS", 10_000)
        ))
        stored_overlap_token_budget = prepared.get("artifact_segment_overlap_token_budget")
        overlap_token_budget = max(0, min(
            int(
                stored_overlap_token_budget
                if stored_overlap_token_budget is not None
                else getattr(settings, "VDR_ARTIFACT_SEGMENT_OVERLAP_TOKENS", 768)
            ),
            source_token_budget // 4,
        ))
        prepared.update({
            "artifact_pipeline_version": cls.ARTIFACT_PIPELINE_VERSION,
            "artifact_model": artifact_model,
            "artifact_source_sha256": hashlib.sha256(raw_text.encode("utf-8")).hexdigest(),
            "artifact_segment_source_token_budget": source_token_budget,
            "artifact_segment_overlap_token_budget": overlap_token_budget,
        })
        return prepared

    @classmethod
    def begin_document_artifact_run(
        cls,
        document: Any,
        source_metadata: dict[str, Any],
    ) -> dict[str, Any]:
        """Persist the exact extraction and splitter contract before inference starts."""
        text = (document.normalized_text or document.extracted_text or "").strip()
        prepared = cls.prepare_artifact_run_metadata(text, source_metadata)
        segments = cls._split_for_artifact(
            text,
            source_tokens=prepared["artifact_segment_source_token_budget"],
            overlap_tokens=prepared["artifact_segment_overlap_token_budget"],
        )
        checkpoint = cls._fallback_artifact(
            file_name=document.title,
            extracted_text=text,
            document_type=document.document_type,
            extraction_mode=document.extraction_mode,
        )
        checkpoint["quality_flags"] = [
            "fallback_artifact",
            "artifact_segment_processing_incomplete",
        ]
        checkpoint["source_metadata"] = {
            **prepared,
            "artifact_segment_count": len(segments),
            "artifact_segments_completed": 0,
        }
        cls.persist_artifact(document, checkpoint)
        return prepared

    @classmethod
    def build_document_artifact(
        cls,
        *,
        file_name: str,
        extracted_text: str,
        document_type: str = "Other",
        extraction_mode: str | None = None,
        ai_service: Optional["AIProcessorService"] = None,
        source_metadata: dict[str, Any] | None = None,
        cancel_check: Callable[[], bool] | None = None,
        yield_check: Callable[[], bool] | None = None,
        force_fresh: bool = False,
    ) -> dict[str, Any]:
        raw_text = (extracted_text or "").strip()
        source_metadata = cls.prepare_artifact_run_metadata(raw_text, source_metadata)
        artifact_model = source_metadata["artifact_model"]
        source_token_budget = source_metadata["artifact_segment_source_token_budget"]
        overlap_token_budget = source_metadata["artifact_segment_overlap_token_budget"]
        fallback = cls._fallback_artifact(
            file_name=file_name,
            extracted_text=raw_text,
            document_type=document_type,
            extraction_mode=extraction_mode,
        )
        fallback["source_metadata"] = deepcopy(source_metadata)

        if not raw_text:
            return fallback

        if ai_service is None:
            from ai_orchestrator.services.ai_processor import AIProcessorService
            service = AIProcessorService()
        else:
            service = ai_service

        # Match bulk_2's lossless map/merge contract: every bounded segment is
        # analyzed, cached by content, and merged. The full extracted source is
        # retained separately and is never replaced by an LLM-cleaned excerpt.
        segments = cls._split_for_artifact(
            raw_text,
            source_tokens=source_token_budget,
            overlap_tokens=overlap_token_budget,
        )
        segment_artifacts: list[dict[str, Any] | None] = [None] * len(segments)
        failures: list[str] = []

        def analyze_segment(index: int, segment: str) -> tuple[int, dict[str, Any], bool]:
            if cancel_check and cancel_check():
                raise DocumentArtifactCancelled("Document artifact processing was cancelled.")
            cache_key = cls._segment_cache_key(
                file_name=file_name,
                segment=segment,
                index=index,
                total=len(segments),
                model=artifact_model,
                run_scope=str(source_metadata.get("artifact_run_id") or ""),
            )
            cached = None
            if not force_fresh:
                try:
                    cached = cache.get(cache_key)
                except Exception:
                    cached = None
                if cls._segment_artifact_usable(cached):
                    return index, cached, True
                if cached:
                    try:
                        cache.delete(cache_key)
                    except Exception:
                        pass

            # A worker can exit after saving the completed model audit but
            # before writing Redis. Recover that response using the same
            # content/model/run checksum instead of sending it again.
            if not force_fresh and source_metadata.get("artifact_run_id"):
                from ai_orchestrator.models import AIAuditLog
                completed = AIAuditLog.objects.filter(
                    source_type="document_evidence_segment", status="COMPLETED", is_success=True,
                    source_metadata__artifact_segment_cache_key=cache_key,
                ).order_by("-completed_at").values_list("parsed_json", flat=True).first()
                if isinstance(completed, dict):
                    recovered = cls._normalize_segment_artifact(completed, fallback=fallback)
                    if cls._segment_artifact_usable(recovered):
                        return index, recovered, True

            # Yield only after all durable recovery checks. This ensures a
            # resumed document walks past completed checkpoints without
            # blocking interactive work or repeating model inference.
            if yield_check and yield_check():
                raise DocumentArtifactYielded(
                    "Higher-priority AI work is waiting; yielding before the next VDR segment."
                )

            segment_context = {
                "document_name": file_name,
                "heuristic_document_type": document_type,
                "spreadsheet_profile": source_metadata.get("spreadsheet_profile") or {},
                "source_metadata": source_metadata,
                "phase1_quality_flags": source_metadata.get("quality_flags") or [],
                "segment_index": index + 1,
                "segment_count": len(segments),
                "source_location": f"{file_name} | segment {index + 1}/{len(segments)}",
                "segment_metadata": {},
                "segment_estimated_tokens": estimate_tokens(segment),
                "segment_source_token_budget": int(
                    getattr(settings, "VDR_ARTIFACT_SEGMENT_SOURCE_TOKENS", 10_000)
                ),
            }
            metadata = {
                "document_name": file_name,
                "document_type": document_type,
                "source_metadata_json": json.dumps(source_metadata, default=str),
                "_source_metadata": {
                    **source_metadata,
                    "segment_index": index,
                    "segment_count": len(segments),
                    "segment_estimated_tokens": estimate_tokens(segment),
                    "segment_input_token_budget": int(
                        getattr(settings, "VDR_ARTIFACT_SEGMENT_INPUT_TOKENS", 14_336)
                    ),
                    "segment_output_token_budget": int(
                        getattr(settings, "VDR_ARTIFACT_SEGMENT_MAX_TOKENS", 32_768)
                    ),
                    "artifact_pipeline_version": cls.ARTIFACT_PIPELINE_VERSION,
                    "artifact_segment_cache_key": cache_key,
                },
                "context_label": f"Document Evidence: {file_name} [{index + 1}/{len(segments)}]",
                "segment_index": index,
                "segment_count": len(segments),
                "chat_template_kwargs": {"enable_thinking": False},
                "max_input_tokens": int(
                    getattr(settings, "VDR_ARTIFACT_SEGMENT_INPUT_TOKENS", 14_336)
                ),
                "max_tokens": int(getattr(settings, "VDR_ARTIFACT_SEGMENT_MAX_TOKENS", 32_768)),
                "request_timeout": int(getattr(settings, "VDR_ARTIFACT_SEGMENT_TIMEOUT", 1800)),
                "enforce_context_budget": True,
                "serialize_inference": True,
                "celery_task_id": source_metadata.get("celery_task_id"),
                "vdr_parent_audit_id": source_metadata.get("vdr_parent_audit_id"),
            }
            def run_model(*, compact: bool = False):
                attempt_metadata = deepcopy(metadata)
                if compact:
                    attempt_metadata["_source_metadata"]["compact_retry"] = True
                    attempt_metadata["context_label"] += " [compact retry]"
                return cls._process_segment(
                    service,
                    content=(
                        f"{BULK2_INTEL_SYSTEM_PROMPT}\n\n"
                        f"{build_bulk2_segment_prompt(segment=segment, context=segment_context, compact=compact)}"
                    ),
                    skill_name="document_evidence_extraction",
                    source_type="document_evidence_segment",
                    source_id=str(source_metadata.get("source_id") or file_name),
                    metadata=attempt_metadata,
                    model_override=artifact_model,
                )

            try:
                result = run_model()
            except Exception as exc:
                if "finish_reason=length" not in str(exc):
                    raise
                result = run_model(compact=True)
            parsed = result.get("parsed_json") if isinstance(result, dict) and "parsed_json" in result else result
            if not isinstance(parsed, dict) or parsed.get("error"):
                raise RuntimeError(
                    str(parsed.get("error") if isinstance(parsed, dict) else "AI segment response was invalid.")
                )
            artifact = cls._normalize_segment_artifact(parsed, fallback=fallback)
            artifact["reasoning"] = result.get("thinking") or artifact.get("reasoning") or "" if isinstance(result, dict) else ""
            if not cls._segment_artifact_usable(artifact):
                raise RuntimeError("AI segment response did not produce a complete evidence artifact.")
            try:
                cache.set(
                    cache_key,
                    artifact,
                    timeout=int(getattr(settings, "VDR_ARTIFACT_CACHE_TTL", 30 * 24 * 60 * 60)),
                )
            except Exception:
                pass
            return index, artifact, False

        # Keep Celery context and database connections on the document thread.
        # Checkpoint each success before submitting the next segment. On a
        # failure the document retry reuses these completed checkpoints.
        for index, segment in enumerate(segments):
            try:
                result_index, segment_artifact, _cache_hit = analyze_segment(index, segment)
                segment_artifacts[result_index] = segment_artifact
            except (DocumentArtifactCancelled, DocumentArtifactYielded):
                raise
            except Exception as exc:
                failures.append(f"segment {index + 1}/{len(segments)}: {exc}")
                break

        if cancel_check and cancel_check():
            raise DocumentArtifactCancelled("Document artifact processing was cancelled.")

        if failures or any(item is None for item in segment_artifacts):
            fallback["quality_flags"] = list(dict.fromkeys([
                *(fallback.get("quality_flags") or []),
                "artifact_segment_processing_incomplete",
                *failures,
            ]))
            fallback["source_metadata"] = {
                **source_metadata,
                "artifact_segment_count": len(segments),
                "artifact_segments_completed": len([item for item in segment_artifacts if item]),
            }
            return fallback

        artifact = cls._merge_segment_artifacts(
            [item for item in segment_artifacts if item],
            fallback=fallback,
        )
        artifact["normalized_text"] = raw_text
        artifact["source_map"] = artifact.get("source_map") or cls._default_source_map(
            file_name,
            extraction_mode,
            raw_text,
        )
        artifact["source_metadata"] = {
            **source_metadata,
            "artifact_pipeline_version": cls.ARTIFACT_PIPELINE_VERSION,
            "artifact_segment_count": len(segments),
            "artifact_segments_completed": len(segments),
            "artifact_model": artifact_model,
            "artifact_segment_input_token_budget": int(
                getattr(settings, "VDR_ARTIFACT_SEGMENT_INPUT_TOKENS", 14_336)
            ),
            "artifact_segment_output_token_budget": int(
                getattr(settings, "VDR_ARTIFACT_SEGMENT_MAX_TOKENS", 32_768)
            ),
            "artifact_segment_source_token_budget": int(
                getattr(settings, "VDR_ARTIFACT_SEGMENT_SOURCE_TOKENS", 10_000)
            ),
        }
        return artifact

    @classmethod
    def _split_for_artifact(
        cls,
        text: str,
        *,
        source_tokens: int | None = None,
        overlap_tokens: int | None = None,
    ) -> list[str]:
        source_tokens = max(4_000, int(
            source_tokens
            if source_tokens is not None
            else getattr(settings, "VDR_ARTIFACT_SEGMENT_SOURCE_TOKENS", 10_000)
        ))
        overlap_tokens = max(0, min(
            int(
                overlap_tokens
                if overlap_tokens is not None
                else getattr(settings, "VDR_ARTIFACT_SEGMENT_OVERLAP_TOKENS", 768)
            ),
            source_tokens // 4,
        ))
        splitter = RecursiveCharacterTextSplitter(
            chunk_size=source_tokens,
            chunk_overlap=overlap_tokens,
            length_function=estimate_tokens,
            separators=["\n\n", "\n", " ", ""],
        )
        return splitter.split_text(text) or [text]

    @classmethod
    def _segment_cache_key(
        cls,
        *,
        file_name: str,
        segment: str,
        index: int,
        total: int,
        model: str,
        run_scope: str = "",
    ) -> str:
        fingerprint = json.dumps(
            [cls.ARTIFACT_PIPELINE_VERSION, model, run_scope, file_name, index, total, segment],
            ensure_ascii=False,
        )
        return "vdr-document-artifact:" + hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()

    @classmethod
    def _merge_segment_artifacts(
        cls,
        artifacts: list[dict[str, Any]],
        *,
        fallback: dict[str, Any],
    ) -> dict[str, Any]:
        merged = deepcopy(fallback)
        list_fields = (
            "claims",
            "metrics",
            "numeric_evidence",
            "table_definitions",
            "tables_summary",
            "contacts_found",
            "risks",
            "open_questions",
            "diligence_gaps",
            "citations",
            "quality_flags",
        )
        for field in list_fields:
            merged[field] = []
        summaries = []
        reasoning = []
        for artifact in artifacts:
            summary = str(artifact.get("document_summary") or "").strip()
            if summary and summary != "No summary extracted":
                summaries.append(summary)
            thought = str(artifact.get("reasoning") or "").strip()
            if thought:
                reasoning.append(thought)
            for field in list_fields:
                merged[field] = cls._dedupe_values([
                    *(merged.get(field) or []),
                    *(artifact.get(field) or []),
                ])

        merged["industry_overview"] = {
            field: cls._dedupe_values([
                item
                for artifact in artifacts
                for item in ((artifact.get("industry_overview") or {}).get(field) or [])
            ])
            for field in ("findings", "market_figures", "citations")
        }

        if summaries:
            merged["document_summary"] = " ".join(cls._dedupe_values(summaries))
        if reasoning:
            merged["reasoning"] = "\n\n".join(cls._dedupe_values(reasoning))

        confidence = {"High": 3, "Medium": 2, "Low": 1}
        suggestions = [
            item.get("document_type_suggestion")
            for item in artifacts
            if isinstance(item.get("document_type_suggestion"), dict)
        ]
        if suggestions:
            merged["document_type_suggestion"] = max(
                suggestions,
                key=lambda item: confidence.get(item.get("confidence"), 0),
            )
            merged["document_type"] = (
                merged["document_type_suggestion"].get("display_label")
                or merged.get("document_type")
            )
        return merged

    @staticmethod
    def _dedupe_values(values: list[Any]) -> list[Any]:
        deduped = []
        seen = set()
        for value in values:
            if value in (None, "", [], {}):
                continue
            key = json.dumps(value, sort_keys=True, default=str)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(value)
        return deduped

    @classmethod
    def persist_artifact(cls, document, artifact: dict[str, Any]) -> None:
        normalized_artifact = cls._normalize_artifact(artifact, fallback=cls._fallback_artifact(
            file_name=document.title,
            extracted_text=document.extracted_text or "",
            document_type=document.document_type,
            extraction_mode=document.extraction_mode,
        ))
        document.normalized_text = normalized_artifact.get("normalized_text") or document.extracted_text
        document.evidence_json = normalized_artifact
        document.source_map_json = normalized_artifact.get("source_map") or {}
        document.table_json = normalized_artifact.get("table_definitions") or normalized_artifact.get("tables_summary") or []
        document.key_metrics_json = normalized_artifact.get("metrics") or []
        document.reasoning = normalized_artifact.get("reasoning") or ""
        document.is_ai_analyzed = cls.artifact_complete(normalized_artifact)
        document.save(
            update_fields=[
                "normalized_text",
                "evidence_json",
                "source_map_json",
                "table_json",
                "key_metrics_json",
                "reasoning",
                "is_ai_analyzed",
            ]
        )

    @classmethod
    def persist_analysis_artifact(cls, analysis_document, artifact: dict[str, Any]) -> None:
        normalized_artifact = cls._normalize_artifact(
            artifact,
            fallback=cls._fallback_artifact(
                file_name=analysis_document.file_name,
                extracted_text=analysis_document.raw_extracted_text or "",
                document_type=analysis_document.document_type,
                extraction_mode=analysis_document.extraction_mode,
            ),
        )
        analysis_document.normalized_text = normalized_artifact.get("normalized_text") or analysis_document.raw_extracted_text
        analysis_document.evidence_json = normalized_artifact
        analysis_document.source_map_json = normalized_artifact.get("source_map") or {}
        analysis_document.table_json = normalized_artifact.get("table_definitions") or normalized_artifact.get("tables_summary") or []
        analysis_document.key_metrics_json = normalized_artifact.get("metrics") or []
        analysis_document.reasoning = normalized_artifact.get("reasoning") or ""
        analysis_document.save(
            update_fields=[
                "normalized_text",
                "evidence_json",
                "source_map_json",
                "table_json",
                "key_metrics_json",
                "reasoning",
            ]
        )

    @classmethod
    def ensure_document_artifact(
        cls,
        document,
        *,
        ai_service: Optional["AIProcessorService"] = None,
        force: bool = False,
    ) -> dict[str, Any]:
        existing = cls.artifact_from_document(document)
        if not force and cls.artifact_status(existing) == cls.STATUS_COMPLETE:
            return existing

        text = (document.normalized_text or document.extracted_text or "").strip()
        artifact = cls.build_document_artifact(
            file_name=document.title,
            extracted_text=text,
            document_type=document.document_type,
            extraction_mode=document.extraction_mode,
            ai_service=ai_service,
            source_metadata={
                "source_id": getattr(document, "id", None),
                "source_url": getattr(document, "file_url", None),
            },
        )
        cls.persist_artifact(document, artifact)
        return cls.artifact_from_document(document)

    @classmethod
    def artifact_from_document(cls, document) -> dict[str, Any]:
        stored = document.evidence_json if isinstance(document.evidence_json, dict) and document.evidence_json else None
        fallback = cls._fallback_artifact(
            file_name=document.title,
            extracted_text=document.normalized_text or document.extracted_text or "",
            document_type=document.document_type,
            extraction_mode=document.extraction_mode,
        )
        artifact = cls._normalize_artifact(stored, fallback=fallback)
        artifact["reasoning"] = document.reasoning or artifact.get("reasoning") or ""
        artifact["normalized_text"] = document.normalized_text or document.extracted_text or artifact.get("normalized_text") or ""
        artifact["source_map"] = artifact.get("source_map") or document.source_map_json or fallback.get("source_map") or {}
        return artifact

    @classmethod
    def artifact_from_file_record(cls, file_record: dict[str, Any]) -> dict[str, Any]:
        return cls._normalize_artifact(
            file_record.get("document_artifact"),
            fallback=cls._fallback_artifact(
                file_name=file_record.get("file_name") or "unknown_file",
                extracted_text=file_record.get("extracted_text") or "",
                document_type=file_record.get("document_type") or "Other",
                extraction_mode=file_record.get("extraction_mode"),
            ),
        )

    @classmethod
    def artifact_from_analysis_document(cls, analysis_document) -> dict[str, Any]:
        stored = analysis_document.evidence_json if isinstance(analysis_document.evidence_json, dict) and analysis_document.evidence_json else None
        fallback = cls._fallback_artifact(
            file_name=analysis_document.file_name,
            extracted_text=analysis_document.normalized_text or analysis_document.raw_extracted_text or "",
            document_type=analysis_document.document_type,
            extraction_mode=analysis_document.extraction_mode,
        )
        artifact = cls._normalize_artifact(stored, fallback=fallback)
        artifact["reasoning"] = analysis_document.reasoning or artifact.get("reasoning") or ""
        artifact["normalized_text"] = (
            analysis_document.normalized_text
            or analysis_document.raw_extracted_text
            or artifact.get("normalized_text")
            or ""
        )
        artifact["source_map"] = (
            artifact.get("source_map")
            or analysis_document.source_map_json
            or fallback.get("source_map")
            or {}
        )
        return artifact

    @classmethod
    def build_supporting_raw_chunks(
        cls,
        documents: list[dict[str, Any]],
        *,
        max_chunks: int = 18,
        excerpt_chars: int = 1600,
    ) -> list[dict[str, Any]]:
        chunks = []
        for doc in documents:
            if len(chunks) >= max_chunks:
                break
            artifact = cls._normalize_artifact(doc, fallback=doc)
            normalized_text = (artifact.get("normalized_text") or "").strip()
            if not normalized_text:
                continue
            chunks.append(
                {
                    "document_name": artifact.get("document_name") or doc.get("document_name") or "Unknown Document",
                    "excerpt": normalized_text[:excerpt_chars],
                    "citation_label": cls._citation_label(artifact),
                    "chunk_kind": "normalized_text",
                }
            )
            for metric in artifact.get("metrics") or []:
                if len(chunks) >= max_chunks:
                    break
                if isinstance(metric, dict):
                    chunks.append(
                        {
                            "document_name": artifact.get("document_name") or "Unknown Document",
                            "excerpt": json.dumps(metric, ensure_ascii=True, default=str),
                            "citation_label": cls._citation_label(artifact),
                            "chunk_kind": "metric",
                        }
                    )
        return chunks

    @classmethod
    def build_embedding_chunks(
        cls,
        artifact_or_document: Any,
        *,
        text_excerpt_chars: int = 2400,
        table_excerpt_chars: int = 1800,
        claim_excerpt_chars: int = 900,
    ) -> list[dict[str, Any]]:
        artifact = cls._coerce_artifact(artifact_or_document)
        if not artifact:
            return []

        document_name = artifact.get("document_name") or "Unknown Document"
        base_metadata = {
            "document_name": document_name,
            "document_type": artifact.get("document_type") or "Other",
            "citation_label": cls._citation_label(artifact),
            "source_map": artifact.get("source_map") or {},
            "artifact_status": cls.artifact_status(artifact),
            "metric_names": [
                metric.get("name")
                for metric in (artifact.get("metrics") or [])
                if isinstance(metric, dict) and metric.get("name")
            ],
        }

        chunks: list[dict[str, Any]] = []
        manifest = getattr(artifact_or_document, "extraction_manifest", None)
        if isinstance(manifest, dict):
            for manifest_index, item in enumerate(manifest.get("chunks") or []):
                if not isinstance(item, dict) or not str(item.get("text") or "").strip():
                    continue
                chunks.append({
                    "text": str(item["text"]),
                    "metadata": {
                        **base_metadata,
                        **(item.get("metadata") or {}),
                        "chunk_kind": (item.get("metadata") or {}).get("chunk_kind", "structured_range"),
                        "manifest_chunk_index": manifest_index,
                        "extraction_schema_version": manifest.get("schema_version", "1"),
                    },
                })
        normalized_text = (artifact.get("normalized_text") or "").strip()
        manifest_kind = manifest.get("kind") if isinstance(manifest, dict) else None
        has_structured_chunks = bool(manifest.get("chunks")) if isinstance(manifest, dict) else False
        # Spreadsheet ranges are already purpose-built retrieval units. Splitting the
        # rendered workbook text again duplicates the same cells in the vector index.
        if manifest_kind == "spreadsheet" and has_structured_chunks:
            normalized_text = ""
        if normalized_text:
            splitter = RecursiveCharacterTextSplitter(
                chunk_size=text_excerpt_chars,
                chunk_overlap=max(200, text_excerpt_chars // 10),
                length_function=len,
                separators=["\n\n", "\n", " ", ""],
            )
            normalized_parts = splitter.split_text(normalized_text)
            total_normalized_parts = len(normalized_parts)
            for part_index, normalized_part in enumerate(normalized_parts):
                if not normalized_part.strip():
                    continue
                chunks.append(
                    {
                        "text": normalized_part,
                        "metadata": {
                            **base_metadata,
                            "chunk_kind": "normalized_text",
                            "normalized_text_part_index": part_index,
                            "normalized_text_part_count": total_normalized_parts,
                        },
                    }
                )

        for metric in artifact.get("metrics") or []:
            serialized = cls._serialize_component(metric)
            if not serialized:
                continue
            chunks.append(
                {
                    "text": serialized,
                    "metadata": {
                        **base_metadata,
                        "chunk_kind": "metric",
                    },
                }
            )

        for table in artifact.get("table_definitions") or artifact.get("tables_summary") or []:
            serialized = cls._serialize_component(table, max_chars=table_excerpt_chars)
            if not serialized:
                continue
            chunks.append(
                {
                    "text": serialized,
                    "metadata": {
                        **base_metadata,
                        "chunk_kind": "table_summary",
                    },
                }
            )

        for claim in artifact.get("claims") or []:
            serialized = cls._serialize_component(claim, max_chars=claim_excerpt_chars)
            if not serialized:
                continue
            chunks.append(
                {
                    "text": serialized,
                    "metadata": {
                        **base_metadata,
                        "chunk_kind": "claim",
                    },
                }
            )

        for risk in artifact.get("risks") or []:
            serialized = cls._serialize_component(risk, max_chars=claim_excerpt_chars)
            if not serialized:
                continue
            chunks.append(
                {
                    "text": serialized,
                    "metadata": {
                        **base_metadata,
                        "chunk_kind": "risk",
                    },
                }
            )

        industry_overview = artifact.get("industry_overview") or {}
        for finding in industry_overview.get("findings") or []:
            serialized = cls._serialize_component(finding, max_chars=claim_excerpt_chars)
            if not serialized:
                continue
            chunks.append(
                {
                    "text": serialized,
                    "metadata": {
                        **base_metadata,
                        "chunk_kind": "industry_finding",
                        "source_ids": finding.get("source_ids") if isinstance(finding, dict) else [],
                    },
                }
            )

        for figure in industry_overview.get("market_figures") or []:
            serialized = cls._serialize_component(figure)
            if not serialized:
                continue
            chunks.append(
                {
                    "text": serialized,
                    "metadata": {
                        **base_metadata,
                        "chunk_kind": "industry_market_figure",
                        "source_ids": figure.get("source_ids") if isinstance(figure, dict) else [],
                    },
                }
            )

        return chunks

    @classmethod
    def artifact_status(cls, artifact_or_document: Any) -> str:
        transcription_status = getattr(artifact_or_document, "transcription_status", None)
        chunking_status = getattr(artifact_or_document, "chunking_status", None)
        if transcription_status == "failed":
            return cls.STATUS_FAILED

        artifact = cls._coerce_artifact(artifact_or_document)
        if transcription_status == "partial" and not cls._legacy_native_extraction_complete(
            artifact_or_document,
            artifact,
        ):
            return cls.STATUS_PARTIAL
        normalized_text = (artifact.get("normalized_text") or "").strip()
        if not normalized_text:
            return cls.STATUS_MISSING

        incomplete_flags = {
            "fallback_artifact",
            "artifact_missing_text",
            "artifact_segment_processing_incomplete",
        }
        if any(flag in incomplete_flags for flag in artifact.get("quality_flags") or []):
            return cls.STATUS_DEGRADED

        source_metadata = artifact.get("source_metadata") or {}
        segment_count = source_metadata.get("artifact_segment_count")
        segments_completed = source_metadata.get("artifact_segments_completed")
        if segment_count is not None and segments_completed != segment_count:
            return cls.STATUS_DEGRADED

        missing_required = [key for key in cls.REQUIRED_ARTIFACT_KEYS if key not in artifact]
        if missing_required:
            return cls.STATUS_DEGRADED

        source_map = artifact.get("source_map") or {}
        if not isinstance(source_map, dict) or not source_map.get("document_name"):
            return cls.STATUS_DEGRADED
        if chunking_status == "failed":
            return cls.STATUS_DEGRADED

        return cls.STATUS_COMPLETE

    @classmethod
    def _legacy_native_extraction_complete(cls, document: Any, artifact: dict[str, Any]) -> bool:
        """Accept old native extractions whose partial state came from provenance flags only."""
        if not getattr(document, "is_indexed", False):
            return False
        if getattr(document, "extraction_mode", None) != "chat_native_text":
            return False
        if not (artifact.get("normalized_text") or "").strip():
            return False

        source_metadata = artifact.get("source_metadata") or {}
        render_metadata = source_metadata.get("render_metadata") or {}
        if render_metadata.get("failed_pages"):
            return False

        quality_flags = set(cls._normalize_string_list(
            source_metadata.get("quality_flags") or artifact.get("quality_flags") or []
        ))
        informational_flags = {
            "chat_direct_extraction",
            "backend_fallback_extraction",
            "calamine",
            "partial_extraction",
        }
        return "backend_fallback_extraction" in quality_flags and quality_flags <= informational_flags

    @classmethod
    def artifact_complete(cls, artifact_or_document: Any) -> bool:
        return cls.artifact_status(artifact_or_document) == cls.STATUS_COMPLETE

    @classmethod
    def _normalize_artifact(cls, artifact: Any, *, fallback: dict[str, Any]) -> dict[str, Any]:
        normalized = deepcopy(fallback)
        if isinstance(artifact, dict):
            for key, default_value in cls.DEFAULT_EVIDENCE_KEYS.items():
                value = artifact.get(key, normalized.get(key, default_value))
                if isinstance(default_value, list):
                    normalized[key] = value if isinstance(value, list) else deepcopy(default_value)
                elif isinstance(default_value, dict):
                    normalized[key] = value if isinstance(value, dict) else deepcopy(default_value)
                elif isinstance(default_value, str):
                    normalized[key] = value.strip() if isinstance(value, str) else normalized.get(key, default_value)
                else:
                    normalized[key] = value if value is not None else normalized.get(key, default_value)
        if not normalized.get("table_definitions") and normalized.get("tables_summary"):
            normalized["table_definitions"] = deepcopy(normalized["tables_summary"])
        if not normalized.get("tables_summary") and normalized.get("table_definitions"):
            normalized["tables_summary"] = deepcopy(normalized["table_definitions"])
        normalized["quality_flags"] = cls._normalize_string_list(normalized.get("quality_flags"))
        normalized["citations"] = cls._normalize_string_list(normalized.get("citations"))
        normalized["industry_overview"] = cls._normalize_industry_overview(
            normalized.get("industry_overview"),
        )
        normalized["document_name"] = normalized.get("document_name") or fallback.get("document_name") or ""
        normalized["document_type"] = normalized.get("document_type") or fallback.get("document_type") or "Other"
        normalized["source_map"] = cls._normalize_source_map(
            normalized.get("source_map"),
            fallback.get("source_map") or {},
        )
        if not normalized["normalized_text"]:
            if "artifact_missing_text" not in normalized["quality_flags"]:
                normalized["quality_flags"].append("artifact_missing_text")
        return normalized

    @classmethod
    def _fallback_artifact(
        cls,
        *,
        file_name: str,
        extracted_text: str,
        document_type: str,
        extraction_mode: str | None,
    ) -> dict[str, Any]:
        excerpt = (extracted_text or "").strip()
        return {
            "document_name": file_name,
            "document_type": document_type,
            "document_type_suggestion": {
                "label": "Other",
                "display_label": document_type or "Other",
                "confidence": "Low",
                "rationale": "Fallback artifact generated without model evidence extraction.",
            },
            "document_summary": excerpt[:500],
            "claims": [],
            "metrics": [],
            "numeric_evidence": [],
            "table_definitions": [],
            "tables_summary": [],
            "contacts_found": [],
            "risks": [],
            "open_questions": [],
            "diligence_gaps": [],
            "citations": [file_name] if file_name else [],
            "industry_overview": {
                "findings": [],
                "market_figures": [],
                "citations": [],
            },
            "reasoning": "",
            "quality_flags": ["fallback_artifact"],
            "normalized_text": excerpt,
            "source_map": cls._default_source_map(file_name, extraction_mode, excerpt),
            "source_metadata": {},
            "spreadsheet_profile": {},
        }

    @staticmethod
    def _coerce_artifact(artifact_or_document: Any) -> dict[str, Any]:
        if isinstance(artifact_or_document, dict):
            return artifact_or_document
        if hasattr(artifact_or_document, "file_name") and hasattr(artifact_or_document, "raw_extracted_text"):
            return DocumentArtifactService.artifact_from_analysis_document(artifact_or_document)
        if hasattr(artifact_or_document, "evidence_json"):
            return DocumentArtifactService.artifact_from_document(artifact_or_document)
        return {}

    @staticmethod
    def _normalize_string_list(value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        seen: set[str] = set()
        normalized: list[str] = []
        for item in value:
            if not isinstance(item, str):
                continue
            cleaned = item.strip()
            if cleaned and cleaned not in seen:
                seen.add(cleaned)
                normalized.append(cleaned)
        return normalized

    @staticmethod
    def _normalize_source_map(value: Any, fallback: dict[str, Any]) -> dict[str, Any]:
        normalized = deepcopy(fallback) if isinstance(fallback, dict) else {}
        if isinstance(value, dict):
            normalized.update(value)
        if not normalized.get("document_name"):
            normalized["document_name"] = fallback.get("document_name") if isinstance(fallback, dict) else ""
        return normalized

    @classmethod
    def _normalize_industry_overview(cls, value: Any) -> dict[str, list[dict[str, Any]]]:
        overview = value if isinstance(value, dict) else {}
        citations: list[dict[str, Any]] = []
        citation_ids: set[str] = set()
        for index, item in enumerate(overview.get("citations") or []):
            if not isinstance(item, dict):
                continue
            citation_id = cls._clean_string(item.get("id")) or f"industry-source-{index + 1}"
            label = cls._clean_string(item.get("label"))
            page = cls._clean_string(item.get("page"))
            section = cls._clean_string(item.get("section"))
            passage = cls._clean_string(item.get("passage"))
            if not label or (not page and not section) or not passage:
                continue
            citation_ids.add(citation_id)
            citations.append(
                {
                    "id": citation_id,
                    "label": label,
                    "page": page,
                    "section": section,
                    "passage": passage,
                }
            )

        findings: list[dict[str, Any]] = []
        for item in overview.get("findings") or []:
            if not isinstance(item, dict):
                continue
            title = cls._clean_string(item.get("title"))
            summary = cls._clean_string(item.get("summary"))
            source_ids = cls._valid_source_ids(item.get("source_ids"), citation_ids)
            if not title or not summary or not source_ids:
                continue
            findings.append(
                {
                    "title": title,
                    "summary": summary,
                    "period": cls._clean_string(item.get("period")),
                    "as_of_date": cls._clean_date(item.get("as_of_date")),
                    "source_ids": source_ids,
                }
            )

        market_figures: list[dict[str, Any]] = []
        for item in overview.get("market_figures") or []:
            if not isinstance(item, dict):
                continue
            label = cls._clean_string(item.get("label"))
            unit = cls._clean_string(item.get("unit"))
            period = cls._clean_string(item.get("period"))
            calculation_method = cls._clean_string(item.get("calculation_method"))
            as_of_date = cls._clean_date(item.get("as_of_date"))
            source_ids = cls._valid_source_ids(item.get("source_ids"), citation_ids)
            try:
                numeric_value = float(item.get("value"))
            except (TypeError, ValueError):
                numeric_value = math.nan
            if (
                not label
                or not unit
                or not period
                or not calculation_method
                or not as_of_date
                or not source_ids
                or not math.isfinite(numeric_value)
            ):
                continue
            market_figures.append(
                {
                    "label": label,
                    "value": numeric_value,
                    "unit": unit,
                    "period": period,
                    "as_of_date": as_of_date,
                    "calculation_method": calculation_method,
                    "source_ids": source_ids,
                }
            )

        return {
            "findings": findings,
            "market_figures": market_figures,
            "citations": citations,
        }

    @staticmethod
    def _clean_string(value: Any) -> str | None:
        if not isinstance(value, (str, int, float)):
            return None
        cleaned = str(value).strip()
        return cleaned or None

    @staticmethod
    def _clean_date(value: Any) -> str | None:
        if isinstance(value, datetime):
            return value.date().isoformat()
        if isinstance(value, date):
            return value.isoformat()
        if not isinstance(value, str):
            return None
        try:
            return date.fromisoformat(value.strip()).isoformat()
        except ValueError:
            return None

    @staticmethod
    def _valid_source_ids(value: Any, citation_ids: set[str]) -> list[str]:
        if not isinstance(value, list):
            return []
        return list(dict.fromkeys(
            source_id.strip()
            for source_id in value
            if isinstance(source_id, str)
            and source_id.strip() in citation_ids
        ))

    @staticmethod
    def _serialize_component(value: Any, *, max_chars: int = 1200) -> str:
        if isinstance(value, str):
            return value.strip()[:max_chars]
        if isinstance(value, (dict, list)):
            return json.dumps(value, ensure_ascii=True, default=str)[:max_chars]
        if value is None:
            return ""
        return str(value).strip()[:max_chars]

    @staticmethod
    def _default_source_map(file_name: str, extraction_mode: str | None, text: str) -> dict[str, Any]:
        return {
            "document_name": file_name,
            "extraction_mode": extraction_mode,
            "text_length": len(text or ""),
        }

    @staticmethod
    def _citation_label(artifact: dict[str, Any]) -> str:
        source_map = artifact.get("source_map") if isinstance(artifact.get("source_map"), dict) else {}
        doc_name = artifact.get("document_name") or source_map.get("document_name") or "Unknown Document"
        section = source_map.get("section")
        page = source_map.get("page")
        parts = [doc_name]
        if section:
            parts.append(f"section={section}")
        if page is not None:
            parts.append(f"page={page}")
        return " | ".join(parts)
