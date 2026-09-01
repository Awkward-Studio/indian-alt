from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

from deals.services.document_artifacts import DocumentArtifactService


class ChatDocumentEvidenceService:
    """Build bounded, conversation-owned evidence from the existing chat upload path."""

    MAX_TEXT_CHARS = 120_000
    MAX_CONTEXT_CHARS = 70_000
    MAX_ITEMS_PER_FIELD = 80
    MAX_ITEM_CHARS = 4_000
    EVIDENCE_FIELDS = (
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
        "source_map",
        "source_metadata",
        "spreadsheet_profile",
    )

    @classmethod
    def build(
        cls,
        *,
        file_name: str,
        extracted_text: str,
        extraction_mode: str | None,
        source_id: str,
        quality_flags: list[str] | None = None,
    ) -> dict[str, Any]:
        text = str(extracted_text or "").strip()
        artifact = DocumentArtifactService.build_document_artifact(
            file_name=file_name,
            extracted_text=text,
            extraction_mode=extraction_mode,
            source_metadata={"source_id": source_id, "source_type": "chat_upload"},
        )
        evidence = {
            key: cls._bound_value(artifact.get(key))
            for key in cls.EVIDENCE_FIELDS
            if artifact.get(key) not in (None, "", [], {})
        }
        combined_flags = list(dict.fromkeys([
            *(quality_flags or []),
            *(artifact.get("quality_flags") or []),
        ]))
        evidence["quality_flags"] = combined_flags
        normalized_text = str(artifact.get("normalized_text") or text).strip()
        return {
            "text": normalized_text[: cls.MAX_TEXT_CHARS],
            "truncated": len(normalized_text) > cls.MAX_TEXT_CHARS,
            "evidence": evidence,
            "artifact_status": DocumentArtifactService.artifact_status(artifact),
        }

    @classmethod
    def build_context(cls, documents: list[dict[str, Any]]) -> tuple[str, int]:
        sections: list[str] = []
        for document in documents:
            if not isinstance(document, dict):
                continue
            text = str(document.get("text") or "").strip()
            evidence = document.get("evidence") if isinstance(document.get("evidence"), dict) else {}
            if not text and not evidence:
                continue
            name = str(document.get("name") or "Untitled")
            payload = {
                "document_id": str(document.get("id") or ""),
                "document_name": name,
                "structured_evidence": evidence,
                "normalized_text": text,
            }
            sections.append(
                f"[UPLOADED DOCUMENT EVIDENCE: {name}]\n"
                f"{json.dumps(payload, default=str, ensure_ascii=True, indent=2)}"
            )
        context = "\n\n".join(sections)
        return context[: cls.MAX_CONTEXT_CHARS], len(sections)

    @classmethod
    def public_metadata(cls, document: dict[str, Any]) -> dict[str, Any]:
        public = deepcopy(document)
        public.pop("text", None)
        evidence = public.pop("evidence", None)
        if isinstance(evidence, dict):
            public["evidence_summary"] = {
                "document_summary": evidence.get("document_summary") or "",
                "claim_count": len(evidence.get("claims") or []),
                "metric_count": len(evidence.get("metrics") or []),
                "table_count": len(evidence.get("table_definitions") or evidence.get("tables_summary") or []),
                "risk_count": len(evidence.get("risks") or []),
                "citation_count": len(evidence.get("citations") or []),
            }
        return public

    @classmethod
    def _bound_value(cls, value: Any) -> Any:
        if isinstance(value, list):
            return [cls._bound_value(item) for item in value[: cls.MAX_ITEMS_PER_FIELD]]
        if isinstance(value, dict):
            return {str(key): cls._bound_value(item) for key, item in value.items()}
        if isinstance(value, str):
            return value[: cls.MAX_ITEM_CHARS]
        return value
