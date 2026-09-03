from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse
from uuid import UUID


@dataclass(frozen=True)
class ChatScope:
    model_provider: str
    web_search_enabled: bool
    document_ids: list[str]
    transcript_ids: list[str]

    @property
    def has_private_scope(self) -> bool:
        return bool(self.document_ids or self.transcript_ids)

    @property
    def evidence_mode(self) -> str:
        if self.web_search_enabled and self.has_private_scope:
            return "mixed"
        if self.web_search_enabled:
            return "web"
        if self.has_private_scope:
            return "internal"
        return "general"


class ChatScopeValidationError(ValueError):
    pass


def _identifier_list(value: Any, field: str, *, limit: int = 25) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ChatScopeValidationError(f"{field} must be an array.")
    normalized: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ChatScopeValidationError(f"{field} must contain non-empty string identifiers.")
        identifier = item.strip()
        try:
            UUID(identifier)
        except ValueError as exc:
            raise ChatScopeValidationError(f"{field} contains an invalid UUID.") from exc
        if identifier not in normalized:
            normalized.append(identifier)
    if len(normalized) > limit:
        raise ChatScopeValidationError(f"{field} accepts at most {limit} identifiers.")
    return normalized


def parse_chat_scope(data: dict[str, Any]) -> ChatScope:
    provider = str(data.get("model_provider") or "vllm").strip().lower()
    if provider not in {"vllm", "anthropic"}:
        raise ChatScopeValidationError("model_provider must be either vllm or anthropic.")

    web_search_enabled = data.get("web_search_enabled", False)
    if not isinstance(web_search_enabled, bool):
        raise ChatScopeValidationError("web_search_enabled must be a boolean.")

    scope = ChatScope(
        model_provider=provider,
        web_search_enabled=web_search_enabled,
        document_ids=_identifier_list(data.get("document_ids"), "document_ids"),
        transcript_ids=_identifier_list(data.get("transcript_ids"), "transcript_ids"),
    )
    if scope.has_private_scope and scope.model_provider == "anthropic":
        raise ChatScopeValidationError(
            "Private documents and transcripts cannot be sent to the external Anthropic provider."
        )
    return scope


def internal_citation(chunk: dict[str, Any]) -> dict[str, Any]:
    metadata = chunk.get("metadata") if isinstance(chunk.get("metadata"), dict) else {}
    source_type = str(chunk.get("source_type") or "")
    kind = "transcript" if source_type == "meeting_note" else "internal_document"
    return {
        "kind": kind,
        "source_id": str(chunk.get("source_id") or ""),
        "chunk_id": str(chunk.get("chunk_id") or ""),
        "title": str(
            chunk.get("source_title")
            or metadata.get("citation_label")
            or metadata.get("title")
            or ("Meeting transcript" if kind == "transcript" else "Deal document")
        ),
        "excerpt": str(chunk.get("text") or chunk.get("excerpt") or "")[:500],
        "meeting_at": metadata.get("meeting_at") if kind == "transcript" else None,
        "timestamp_seconds": metadata.get("timestamp_seconds") if kind == "transcript" else None,
    }


def normalize_web_citation(value: Any, *, retrieved_at: str) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    url = str(value.get("url") or value.get("source_url") or "").strip()
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return {
        "kind": "web",
        "source_label": str(value.get("source_label") or "")[:20],
        "url": url,
        "title": str(value.get("title") or value.get("source_title") or parsed.netloc),
        "cited_text": str(value.get("cited_text") or value.get("snippet") or "")[:1000],
        "query": str(value.get("query") or "")[:500],
        "published_date": str(value.get("published_date") or "")[:100],
        "engine": str(value.get("engine") or "")[:100],
        "engines": [str(engine)[:100] for engine in (value.get("engines") or [])[:10]],
        "retrieved_at": retrieved_at,
    }
