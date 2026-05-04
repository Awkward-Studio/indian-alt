from __future__ import annotations

import json
from typing import Any

from django.core.management.base import BaseCommand, CommandError

from ai_orchestrator.models import AIAuditLog, DocumentChunk
from ai_orchestrator.services.ai_processor import AIProcessorService
from ai_orchestrator.services.universal_chat import UniversalChatService
from ai_orchestrator.tasks import _deal_comparison_context, _extract_markdown_report
from deals.models import Deal, DealGeneratedDocument


class Command(BaseCommand):
    help = (
        "Trace a deal-helper generated document/audit end to end: audit state, "
        "selected deals/docs/chunks, saved content formatting, and comparison context."
    )

    def add_arguments(self, parser):
        parser.add_argument("--audit-id", help="Audit id or unique audit id prefix, e.g. 5b5f2901.")
        parser.add_argument("--document-id", help="Generated document id or unique generated document id prefix.")
        parser.add_argument("--deal-id", help="Deal id/prefix. Used with --latest when no document/audit is supplied.")
        parser.add_argument("--message", help="Run the deal-helper start/planning stage for this deal/message without queuing generation.")
        parser.add_argument("--conversation-id", default="trace-deal-helper", help="Conversation id passed to the planner in --message mode.")
        parser.add_argument("--latest", action="store_true", help="Inspect the latest generated document for --deal-id, or latest overall.")
        parser.add_argument("--max-chars", type=int, default=2500, help="Preview length for long text fields.")
        parser.add_argument("--show-raw", action="store_true", help="Print raw stored response/content previews.")
        parser.add_argument("--show-context", action="store_true", help="Print reconstructed selected evidence and pipeline comparison context.")
        parser.add_argument("--json", action="store_true", dest="as_json", help="Emit a machine-readable JSON diagnostic payload.")
        parser.add_argument(
            "--apply-clean-content",
            action="store_true",
            help="If generated_document.content is JSON-wrapped, replace it with extracted markdown.",
        )

    def handle(self, *args, **options):
        max_chars = max(int(options["max_chars"] or 2500), 200)
        document = self._resolve_document(options)
        audit = self._resolve_audit(options, document)
        deal = self._resolve_deal(options, document, audit)

        selected_deal_ids = self._selected_deal_ids(document, audit)
        selected_document_ids = self._selected_document_ids(document, audit)
        selected_chunk_ids = self._selected_chunk_ids(document, audit)
        chunks = self._selected_chunks(selected_chunk_ids)
        competitors = self._selected_competitors(deal, selected_deal_ids)

        content = document.content if document else ""
        raw_response = audit.raw_response if audit else ""
        cleaned_from_doc = _extract_markdown_report({"response": content or ""})
        cleaned_from_audit = _extract_markdown_report({
            "response": raw_response or "",
            "parsed_json": audit.parsed_json if audit else None,
            "thinking": audit.raw_thinking if audit else "",
        })
        effective_cleaned = cleaned_from_doc or cleaned_from_audit
        content_is_json_wrapped = bool(content) and content.strip().startswith("{") and effective_cleaned != content.strip()

        diagnostic = {
            "start_simulation": self._simulate_start(options, deal),
            "audit": self._audit_payload(audit),
            "generated_document": self._document_payload(document, content_is_json_wrapped),
            "deal": self._deal_payload(deal, role="current_deal"),
            "selected_deal_ids": selected_deal_ids,
            "selected_document_ids": selected_document_ids,
            "selected_chunk_ids": selected_chunk_ids,
            "selected_competitors": [self._deal_payload(item, role="selected_pipeline_competitor") for item in competitors],
            "selected_chunks": [self._chunk_payload(chunk) for chunk in chunks],
            "formatting": {
                "content_is_json_wrapped": content_is_json_wrapped,
                "stored_content_preview": self._preview(content, max_chars),
                "cleaned_markdown_preview": self._preview(effective_cleaned, max_chars),
            },
            "context": {
                "selected_evidence_preview": self._preview(self._selected_evidence_context(chunks), max_chars),
                "pipeline_comparison_context_preview": self._preview(
                    _deal_comparison_context(deal, selected_deal_ids) if deal else "",
                    max_chars,
                ),
            },
        }

        if options["apply_clean_content"]:
            if not document:
                raise CommandError("--apply-clean-content requires a generated document.")
            if not content_is_json_wrapped:
                self.stdout.write(self.style.WARNING("Generated document content is not JSON-wrapped; no update made."))
            elif not effective_cleaned:
                self.stdout.write(self.style.ERROR("Could not extract markdown content; no update made."))
            else:
                document.content = effective_cleaned
                document.save(update_fields=["content"])
                diagnostic["generated_document"]["content_repaired"] = True
                self.stdout.write(self.style.SUCCESS(f"Repaired generated document content: {document.id}"))

        if options["as_json"]:
            self.stdout.write(json.dumps(diagnostic, indent=2, default=str))
            return

        self._print_report(diagnostic, show_raw=options["show_raw"], show_context=options["show_context"])

    def _resolve_document(self, options) -> DealGeneratedDocument | None:
        document_id = options.get("document_id")
        audit_id = options.get("audit_id")
        deal_id = options.get("deal_id")

        if document_id:
            return self._get_by_id_or_prefix(DealGeneratedDocument.objects.all(), document_id, "generated document")

        if audit_id:
            queryset = DealGeneratedDocument.objects.filter(audit_log_id__startswith=audit_id).order_by("-created_at")
            if queryset.count() > 1:
                self.stdout.write(self.style.WARNING(f"Multiple generated documents matched audit prefix {audit_id}; using latest."))
            document = queryset.first()
            if document:
                return document

        if options.get("latest"):
            queryset = DealGeneratedDocument.objects.all().order_by("-created_at")
            if deal_id:
                deal = self._get_by_id_or_prefix(Deal.objects.all(), deal_id, "deal")
                queryset = queryset.filter(deal=deal)
            return queryset.first()

        return None

    def _resolve_audit(self, options, document: DealGeneratedDocument | None) -> AIAuditLog | None:
        audit_id = options.get("audit_id") or (document.audit_log_id if document else None)
        if not audit_id:
            return None
        return self._get_by_id_or_prefix(AIAuditLog.objects.all(), audit_id, "audit log", required=False)

    def _resolve_deal(self, options, document: DealGeneratedDocument | None, audit: AIAuditLog | None) -> Deal | None:
        if document:
            return document.deal
        if options.get("deal_id"):
            return self._get_by_id_or_prefix(Deal.objects.all(), options["deal_id"], "deal")
        if audit and audit.source_id:
            return self._get_by_id_or_prefix(Deal.objects.all(), audit.source_id, "deal", required=False)
        return None

    def _get_by_id_or_prefix(self, queryset, value: str, label: str, *, required: bool = True):
        if not value:
            if required:
                raise CommandError(f"{label} id is required.")
            return None
        matches = list(queryset.filter(id__startswith=value)[:3])
        if not matches:
            if required:
                raise CommandError(f"No {label} matched id/prefix {value}.")
            return None
        if len(matches) > 1:
            raise CommandError(f"{label} prefix {value} is ambiguous; use the full id.")
        return matches[0]

    def _selected_deal_ids(self, document, audit) -> list[str]:
        if document and isinstance(document.selected_deal_ids, list):
            return [str(item) for item in document.selected_deal_ids if item]
        metadata = audit.source_metadata if audit and isinstance(audit.source_metadata, dict) else {}
        return [str(item) for item in metadata.get("selected_deal_ids", []) if item]

    def _selected_document_ids(self, document, audit) -> list[str]:
        if document and isinstance(document.selected_document_ids, list):
            return [str(item) for item in document.selected_document_ids if item]
        metadata = audit.source_metadata if audit and isinstance(audit.source_metadata, dict) else {}
        return [str(item) for item in metadata.get("selected_document_ids", []) if item]

    def _selected_chunk_ids(self, document, audit) -> list[str]:
        if document and isinstance(document.selected_chunk_ids, list):
            return [str(item) for item in document.selected_chunk_ids if item]
        metadata = audit.source_metadata if audit and isinstance(audit.source_metadata, dict) else {}
        return [str(item) for item in metadata.get("selected_chunk_ids", []) if item]

    def _selected_chunks(self, chunk_ids: list[str]) -> list[DocumentChunk]:
        if not chunk_ids:
            return []
        ordering = {chunk_id: index for index, chunk_id in enumerate(chunk_ids)}
        chunks = list(DocumentChunk.objects.filter(id__in=chunk_ids).select_related("deal"))
        chunks.sort(key=lambda chunk: ordering.get(str(chunk.id), 9999))
        return chunks

    def _selected_competitors(self, deal: Deal | None, selected_deal_ids: list[str]) -> list[Deal]:
        if not deal or not selected_deal_ids:
            return []
        return list(Deal.objects.filter(id__in=selected_deal_ids).exclude(id=deal.id).order_by("title"))

    def _selected_evidence_context(self, chunks: list[DocumentChunk]) -> str:
        parts = []
        for chunk in chunks:
            metadata = chunk.metadata or {}
            title = metadata.get("title") or metadata.get("filename") or chunk.source_type
            parts.append(f"[{chunk.deal.title if chunk.deal else 'Unknown Deal'} | {title}]\n{chunk.content or ''}")
        return "\n\n".join(parts)

    def _simulate_start(self, options, deal: Deal | None) -> dict[str, Any] | None:
        message = str(options.get("message") or "").strip()
        if not message:
            return None
        if not deal:
            raise CommandError("--message requires --deal-id, --document-id, or --audit-id resolving to a deal.")

        service = UniversalChatService(AIProcessorService())
        helper = service.start_deal_helper_session(
            deal_id=str(deal.id),
            user_message=message,
            conversation_id=options.get("conversation_id") or "trace-deal-helper",
            history_context="",
        )
        return {
            "message": message,
            "route": helper.get("route"),
            "query_plan": helper.get("query_plan"),
            "candidate_deals": helper.get("candidate_deals") or [],
            "documents": helper.get("documents") or [],
            "saved_context": helper.get("saved_context") or "",
        }

    def _audit_payload(self, audit: AIAuditLog | None) -> dict[str, Any] | None:
        if not audit:
            return None
        return {
            "id": str(audit.id),
            "source_type": audit.source_type,
            "source_id": audit.source_id,
            "context_label": audit.context_label,
            "status": audit.status,
            "is_success": audit.is_success,
            "error_message": audit.error_message,
            "celery_task_id": audit.celery_task_id,
            "created_at": audit.created_at.isoformat() if audit.created_at else None,
            "source_metadata": audit.source_metadata,
            "raw_response_len": len(audit.raw_response or ""),
            "raw_thinking_len": len(audit.raw_thinking or ""),
        }

    def _document_payload(self, document: DealGeneratedDocument | None, content_is_json_wrapped: bool) -> dict[str, Any] | None:
        if not document:
            return None
        return {
            "id": str(document.id),
            "deal_id": str(document.deal_id),
            "title": document.title,
            "kind": document.kind,
            "audit_log_id": document.audit_log_id,
            "created_at": document.created_at.isoformat() if document.created_at else None,
            "content_len": len(document.content or ""),
            "content_is_json_wrapped": content_is_json_wrapped,
            "selected_deal_count": len(document.selected_deal_ids or []),
            "selected_document_count": len(document.selected_document_ids or []),
            "selected_chunk_count": len(document.selected_chunk_ids or []),
        }

    def _deal_payload(self, deal: Deal | None, *, role: str) -> dict[str, Any] | None:
        if not deal:
            return None
        current_analysis = deal.current_analysis if isinstance(deal.current_analysis, dict) else {}
        deal_model_data = current_analysis.get("deal_model_data") if isinstance(current_analysis, dict) else {}
        return {
            "role": role,
            "id": str(deal.id),
            "title": deal.title,
            "industry": deal.industry,
            "sector": deal.sector,
            "current_phase": deal.current_phase,
            "priority": deal.priority,
            "funding_ask": deal.funding_ask,
            "funding_ask_for": deal.funding_ask_for,
            "has_deal_summary": bool(deal.deal_summary),
            "deal_model_data_keys": sorted((deal_model_data or {}).keys()) if isinstance(deal_model_data, dict) else [],
        }

    def _chunk_payload(self, chunk: DocumentChunk) -> dict[str, Any]:
        metadata = chunk.metadata or {}
        return {
            "id": str(chunk.id),
            "deal_id": str(chunk.deal_id),
            "deal_title": chunk.deal.title if chunk.deal else None,
            "source_type": chunk.source_type,
            "source_id": chunk.source_id,
            "source_title": metadata.get("title") or metadata.get("filename"),
            "content_len": len(chunk.content or ""),
            "metadata_keys": sorted(metadata.keys()),
            "content_preview": self._preview(chunk.content, 500),
        }

    def _preview(self, value: Any, max_chars: int) -> str:
        text = "" if value is None else str(value)
        if len(text) <= max_chars:
            return text
        return text[: max_chars - 20].rstrip() + "\n...[truncated]..."

    def _print_report(self, diagnostic: dict[str, Any], *, show_raw: bool, show_context: bool) -> None:
        audit = diagnostic["audit"] or {}
        document = diagnostic["generated_document"] or {}
        deal = diagnostic["deal"] or {}
        formatting = diagnostic["formatting"]
        start_simulation = diagnostic.get("start_simulation")

        self.stdout.write(self.style.MIGRATE_HEADING("Deal Helper Analysis Trace"))
        if start_simulation:
            self.stdout.write(self.style.HTTP_INFO("Start/planning simulation"))
            self.stdout.write(f"Message: {start_simulation['message']}")
            self.stdout.write(f"Route: {start_simulation['route']}")
            self.stdout.write(f"Candidate deals: {len(start_simulation['candidate_deals'])}")
            for item in start_simulation["candidate_deals"][:10]:
                self.stdout.write(
                    f"- {item.get('title') or 'Untitled'} | suggested={item.get('suggested')} | "
                    f"score={item.get('suggested_score') or item.get('retrieval_score') or 'N/A'} | "
                    f"reason={item.get('rank_reason') or 'N/A'}"
                )
            self.stdout.write(f"Candidate documents: {len(start_simulation['documents'])}")
            for item in start_simulation["documents"][:10]:
                self.stdout.write(
                    f"- {item.get('title') or 'Untitled'} | indexed={item.get('is_indexed')} | "
                    f"suggested={item.get('suggested')} | reason={item.get('rank_reason') or 'N/A'}"
                )
            self.stdout.write("")

        self.stdout.write(f"Audit: {audit.get('id') or 'N/A'} status={audit.get('status') or 'N/A'} task={audit.get('celery_task_id') or 'N/A'}")
        if audit.get("error_message"):
            self.stdout.write(self.style.ERROR(f"Audit error: {audit['error_message']}"))

        self.stdout.write(f"Generated document: {document.get('id') or 'N/A'} title={document.get('title') or 'N/A'}")
        self.stdout.write(f"Current deal: {deal.get('title') or 'N/A'} ({deal.get('id') or 'N/A'})")
        self.stdout.write(
            "Selections: "
            f"deals={len(diagnostic['selected_deal_ids'])} "
            f"documents={len(diagnostic['selected_document_ids'])} "
            f"chunks={len(diagnostic['selected_chunk_ids'])}"
        )

        self.stdout.write("")
        self.stdout.write(self.style.HTTP_INFO("Selected pipeline competitors"))
        competitors = diagnostic["selected_competitors"]
        if not competitors:
            self.stdout.write(self.style.WARNING("No selected competitor deals found on the document/audit."))
        for competitor in competitors:
            self.stdout.write(
                f"- {competitor['title']} | {competitor.get('industry') or 'N/A'} / "
                f"{competitor.get('sector') or 'N/A'} | phase={competitor.get('current_phase') or 'N/A'} | "
                f"funding_ask={competitor.get('funding_ask') or 'N/A'} | model_keys={competitor.get('deal_model_data_keys') or []}"
            )

        self.stdout.write("")
        self.stdout.write(self.style.HTTP_INFO("Selected evidence chunks by deal"))
        chunk_counts: dict[str, int] = {}
        for chunk in diagnostic["selected_chunks"]:
            title = chunk.get("deal_title") or "Unknown Deal"
            chunk_counts[title] = chunk_counts.get(title, 0) + 1
        if not chunk_counts:
            self.stdout.write(self.style.WARNING("No selected chunks resolved to DocumentChunk rows."))
        for title, count in sorted(chunk_counts.items()):
            self.stdout.write(f"- {title}: {count} chunk(s)")

        self.stdout.write("")
        self.stdout.write(self.style.HTTP_INFO("Formatting diagnosis"))
        if formatting["content_is_json_wrapped"]:
            self.stdout.write(self.style.WARNING("Stored generated document content is JSON-wrapped; ReactMarkdown will render it poorly."))
            self.stdout.write("Run again with --apply-clean-content to replace it with extracted markdown.")
        else:
            self.stdout.write(self.style.SUCCESS("Stored generated document content appears markdown-ready."))
        self.stdout.write(f"Cleaned markdown preview:\n{formatting['cleaned_markdown_preview'] or 'N/A'}")

        if show_raw:
            self.stdout.write("")
            self.stdout.write(self.style.HTTP_INFO("Stored raw content preview"))
            self.stdout.write(formatting["stored_content_preview"] or "N/A")

        if show_context:
            self.stdout.write("")
            self.stdout.write(self.style.HTTP_INFO("Selected evidence context preview"))
            self.stdout.write(diagnostic["context"]["selected_evidence_preview"] or "N/A")
            self.stdout.write("")
            self.stdout.write(self.style.HTTP_INFO("Pipeline comparison context preview"))
            self.stdout.write(diagnostic["context"]["pipeline_comparison_context_preview"] or "N/A")
