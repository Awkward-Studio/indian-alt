import json
import logging
from copy import deepcopy
from typing import Any, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from ai_orchestrator.services.ai_processor import AIProcessorService

logger = logging.getLogger(__name__)


class DocumentArtifactService:
    """
    Builds and persists normalized document artifacts so deal synthesis can
    operate on structured evidence instead of one giant combined prompt.
    """

    DEFAULT_EVIDENCE_KEYS = {
        "document_name": "",
        "document_type": "Other",
        "document_summary": "",
        "claims": [],
        "metrics": [],
        "tables_summary": [],
        "contacts_found": [],
        "risks": [],
        "open_questions": [],
        "citations": [],
        "reasoning": "",
        "quality_flags": [],
        "normalized_text": "",
        "source_map": {},
    }
    REQUIRED_ARTIFACT_KEYS = (
        "document_name",
        "document_type",
        "document_summary",
        "claims",
        "metrics",
        "tables_summary",
        "contacts_found",
        "risks",
        "open_questions",
        "citations",
        "quality_flags",
        "normalized_text",
        "source_map",
    )
    STATUS_COMPLETE = "complete"
    STATUS_DEGRADED = "degraded"
    STATUS_MISSING = "missing"

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
    ) -> dict[str, Any]:
        normalized_text = (extracted_text or "").strip()
        source_metadata = source_metadata or {}
        fallback = cls._fallback_artifact(
            file_name=file_name,
            extracted_text=normalized_text,
            document_type=document_type,
            extraction_mode=extraction_mode,
        )

        if not normalized_text:
            return fallback

        if ai_service is None:
            from ai_orchestrator.services.ai_processor import AIProcessorService
            service = AIProcessorService()
        else:
            service = ai_service
        metadata = {
            "document_name": file_name,
            "document_type": document_type,
            "source_metadata_json": json.dumps(source_metadata, default=str),
            "context_label": f"Document Evidence: {file_name}",
        }

        try:
            result = service.process_content(
                content=normalized_text,
                skill_name="document_evidence_extraction",
                source_type="document_evidence",
                source_id=str(source_metadata.get("source_id") or file_name),
                metadata=metadata,
            )
            parsed = result.get("parsed_json") if isinstance(result, dict) and "parsed_json" in result else result
            artifact = cls._normalize_artifact(parsed, fallback=fallback)
            artifact["reasoning"] = result.get("thinking") or artifact.get("reasoning") or ""
            artifact["normalized_text"] = artifact.get("normalized_text") or normalized_text
            artifact["source_map"] = artifact.get("source_map") or cls._default_source_map(file_name, extraction_mode, normalized_text)
            return artifact
        except Exception as e:
            logger.warning("Document evidence extraction failed for %s: %s", file_name, e)
            return fallback

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
        document.table_json = normalized_artifact.get("tables_summary") or []
        document.key_metrics_json = normalized_artifact.get("metrics") or []
        document.reasoning = normalized_artifact.get("reasoning") or ""
        document.save(
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
        analysis_document.table_json = normalized_artifact.get("tables_summary") or []
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
            source_metadata={"source_id": getattr(document, "id", None)},
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
        normalized_text = (artifact.get("normalized_text") or "").strip()
        if normalized_text:
            chunks.append(
                {
                    "text": normalized_text[:text_excerpt_chars],
                    "metadata": {
                        **base_metadata,
                        "chunk_kind": "normalized_text",
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

        for table in artifact.get("tables_summary") or []:
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

        return chunks

    @classmethod
    def artifact_status(cls, artifact_or_document: Any) -> str:
        artifact = cls._coerce_artifact(artifact_or_document)
        normalized_text = (artifact.get("normalized_text") or "").strip()
        if not normalized_text:
            return cls.STATUS_MISSING

        if any(flag in {"fallback_artifact", "artifact_missing_text"} for flag in artifact.get("quality_flags") or []):
            return cls.STATUS_DEGRADED

        missing_required = [key for key in cls.REQUIRED_ARTIFACT_KEYS if key not in artifact]
        if missing_required:
            return cls.STATUS_DEGRADED

        source_map = artifact.get("source_map") or {}
        if not isinstance(source_map, dict) or not source_map.get("document_name"):
            return cls.STATUS_DEGRADED

        return cls.STATUS_COMPLETE

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
        normalized["quality_flags"] = cls._normalize_string_list(normalized.get("quality_flags"))
        normalized["citations"] = cls._normalize_string_list(normalized.get("citations"))
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
            "document_summary": excerpt[:500],
            "claims": [],
            "metrics": [],
            "tables_summary": [],
            "contacts_found": [],
            "risks": [],
            "open_questions": [],
            "citations": [file_name] if file_name else [],
            "reasoning": "",
            "quality_flags": ["fallback_artifact"],
            "normalized_text": excerpt,
            "source_map": cls._default_source_map(file_name, extraction_mode, excerpt),
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
