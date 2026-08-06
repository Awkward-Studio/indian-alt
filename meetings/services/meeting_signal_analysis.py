import json
import logging
import re
import time
from typing import Any

from django.conf import settings

from ai_orchestrator.models import AIAuditLog
from ai_orchestrator.services.prompt_catalog import PromptCatalogService
from ai_orchestrator.services.runtime import AIRuntimeService
from ai_orchestrator.services.realtime import broadcast_audit_log_update
from deals.models import Deal
from meetings.models import MeetingNote

logger = logging.getLogger(__name__)


class MeetingSignalAnalysisService:
    """Cross-meeting signal extraction using the configured VM text model."""

    def __init__(self):
        self.base_url = getattr(settings, "VLLM_BASE_URL", "").rstrip("/")
        self.model = AIRuntimeService.get_text_model()

    def analyze_deal(self, deal: Deal, notes: list[MeetingNote]) -> dict[str, Any]:
        if not notes:
            return {
                "deal_id": str(deal.id),
                "deal_title": deal.title,
                "provider": "vllm",
                "model": self.model,
                "notes_analyzed": 0,
                "green_signals": [],
                "red_signals": [],
                "open_questions": [],
                "executive_summary": "No meeting notes are available for this deal.",
            }

        prompt = self._build_prompt(deal, notes)
        system_prompt = PromptCatalogService.get("meeting_signal_system")
        started_at = time.monotonic()
        audit_log = AIAuditLog.objects.create(
            source_type="meeting_signal_analysis",
            source_id=str(deal.id),
            context_label=f"Cross-meeting signals: {deal.title}",
            model_provider="vllm",
            model_used=self.model,
            system_prompt=system_prompt,
            user_prompt=prompt,
            raw_response="",
            status="PROCESSING",
            is_success=False,
            source_metadata={
                "deal_id": str(deal.id),
                "deal_title": deal.title,
                "meeting_note_ids": [str(note.id) for note in notes],
                "notes_analyzed": len(notes),
                "workflow": "cross_meeting_signal_analysis",
            },
        )
        self._broadcast_audit(audit_log)

        def complete(content: str, *, provider: str, base_url: str, model: str) -> dict[str, Any]:
            parsed = self._normalize_result(self._parse_json(content))
            result = {
                "deal_id": str(deal.id),
                "deal_title": deal.title,
                "provider": provider,
                "model": model,
                "base_url": base_url,
                "audit_log_id": str(audit_log.id),
                "notes_analyzed": len(notes),
                **parsed,
            }
            audit_log.raw_response = content
            audit_log.parsed_json = result
            audit_log.model_provider = provider
            audit_log.model_used = model
            audit_log.request_duration_ms = round((time.monotonic() - started_at) * 1000)
            audit_log.status = "COMPLETED"
            audit_log.is_success = True
            audit_log.error_message = ""
            audit_log.source_metadata = {
                **(audit_log.source_metadata or {}),
                "base_url": base_url,
            }
            audit_log.save(
                update_fields=[
                    "raw_response", "parsed_json", "model_provider", "model_used",
                    "request_duration_ms", "status", "is_success", "error_message",
                    "source_metadata",
                ]
            )
            self._broadcast_audit(audit_log, done=True)
            return result

        try:
            from ai_orchestrator.services.llm_providers import VLLMProviderService

            vm_result = VLLMProviderService().execute_standard(
                {
                    "model": self.model,
                    "system": system_prompt,
                    "prompt": f"/no_think\n{prompt}",
                    "response_format": self._response_format(),
                    "chat_template_kwargs": {"enable_thinking": False},
                    "options": {"temperature": 0.1, "max_tokens": 16000},
                },
                timeout=300,
            )
            content = str(vm_result.get("response") or "")
            if not content.strip():
                raise ValueError("The VM returned no final meeting-analysis content.")
            return complete(content, provider="vllm", base_url=self.base_url, model=self.model)
        except Exception as exc:
            last_error = exc
            logger.warning("VM meeting signal analysis failed: %s", exc)

        error_message = (
            "VM meeting signal analysis failed. "
            f"Endpoint: {self.base_url or 'not configured'}. "
            f"Error: {last_error}."
        )
        audit_log.status = "FAILED"
        audit_log.is_success = False
        audit_log.error_message = error_message
        audit_log.request_duration_ms = round((time.monotonic() - started_at) * 1000)
        audit_log.save(
            update_fields=["status", "is_success", "error_message", "request_duration_ms"]
        )
        self._broadcast_audit(audit_log, done=True)
        raise RuntimeError(error_message)

    def _broadcast_audit(self, audit_log: AIAuditLog, *, done: bool = False) -> None:
        try:
            broadcast_audit_log_update(audit_log, done=done)
        except Exception as exc:
            logger.warning("Meeting signal audit broadcast failed: %s", exc)

    def _build_prompt(self, deal: Deal, notes: list[MeetingNote]) -> str:
        note_blocks = []
        for index, note in enumerate(notes, start=1):
            note_text = "\n".join(
                part
                for part in [
                    f"Title: {note.title or 'Meeting Note'}",
                    f"Meeting Date: {note.meeting_at.isoformat() if note.meeting_at else ''}",
                    f"Summary:\n{note.summary or ''}",
                    f"Transcript:\n{note.body or ''}",
                ]
                if part.strip()
            )
            note_blocks.append(f"[NOTE {index} | id={note.id}]\n{note_text}")

        return PromptCatalogService.render(
            "meeting_signal_user",
            deal_title=deal.title,
            meeting_notes="\n".join(note_blocks),
        ).strip()

    def _parse_json(self, content: str) -> dict[str, Any]:
        raw = (content or "").strip()
        if not raw:
            raise ValueError("The VM returned an empty response.")
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", raw, flags=re.DOTALL)
            if not match:
                raise
            candidate = match.group(0)
            try:
                return json.loads(candidate)
            except json.JSONDecodeError:
                repaired = self._repair_truncated_json(candidate)
                return json.loads(repaired)

    def _repair_truncated_json(self, raw: str) -> str:
        text = raw.strip()
        text = re.sub(r",\s*([}\]])", r"\1", text)
        in_string = False
        escape = False
        stack: list[str] = []
        for char in text:
            if escape:
                escape = False
                continue
            if char == "\\":
                escape = True
                continue
            if char == '"':
                in_string = not in_string
                continue
            if in_string:
                continue
            if char in "{[":
                stack.append("}" if char == "{" else "]")
            elif char in "}]":
                if stack and stack[-1] == char:
                    stack.pop()
        if in_string:
            text += '"'
        text = re.sub(r",\s*$", "", text)
        while stack:
            text += stack.pop()
        return text

    def _normalize_result(self, parsed: dict[str, Any]) -> dict[str, Any]:
        def normalize_signal(item: Any) -> dict[str, Any]:
            if isinstance(item, dict):
                title = str(item.get("title") or item.get("signal") or "Signal")
                detail = str(item.get("detail") or item.get("description") or title)
                return {
                    "title": title,
                    "detail": detail,
                    "evidence": item.get("evidence") if isinstance(item.get("evidence"), list) else [],
                    "confidence": item.get("confidence") or "medium",
                }
            text = str(item or "").strip()
            return {"title": text[:80] or "Signal", "detail": text, "evidence": [], "confidence": "medium"}

        return {
            "executive_summary": str(parsed.get("executive_summary") or ""),
            "green_signals": [normalize_signal(item) for item in (parsed.get("green_signals") or [])][:8],
            "red_signals": [normalize_signal(item) for item in (parsed.get("red_signals") or [])][:8],
            "open_questions": [str(item) for item in (parsed.get("open_questions") or [])][:10],
        }

    def _response_format(self) -> dict[str, Any]:
        signal_schema = {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "detail": {"type": "string"},
                "evidence": {"type": "array", "items": {"type": "string"}},
                "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
            },
            "required": ["title", "detail", "evidence", "confidence"],
        }
        return {
            "type": "json_schema",
            "json_schema": {
                "name": "meeting_signal_analysis",
                "strict": True,
                "schema": {
                    "type": "object",
                    "properties": {
                        "executive_summary": {"type": "string"},
                        "green_signals": {
                            "type": "array",
                            "items": signal_schema,
                            "maxItems": 8,
                        },
                        "red_signals": {
                            "type": "array",
                            "items": signal_schema,
                            "maxItems": 8,
                        },
                        "open_questions": {
                            "type": "array",
                            "items": {"type": "string"},
                            "maxItems": 10,
                        },
                    },
                    "required": [
                        "executive_summary",
                        "green_signals",
                        "red_signals",
                        "open_questions",
                    ],
                },
            },
        }
