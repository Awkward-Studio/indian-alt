import json
import logging
import re
from typing import Any

import requests
from django.conf import settings

from deals.models import Deal
from meetings.models import MeetingNote

logger = logging.getLogger(__name__)


class MeetingSignalAnalysisService:
    """Demo-oriented meeting signal extraction using an LM Studio local model."""

    DEFAULT_MODEL = "local-model"

    def __init__(self):
        self.base_urls = self._candidate_base_urls()
        self.model = getattr(settings, "LM_STUDIO_MODEL", "") or self.DEFAULT_MODEL
        self.api_key = getattr(settings, "LM_STUDIO_API_KEY", "") or "lm-studio"

    def analyze_deal(self, deal: Deal, notes: list[MeetingNote]) -> dict[str, Any]:
        if not notes:
            return {
                "deal_id": str(deal.id),
                "deal_title": deal.title,
                "provider": "lm_studio",
                "model": self.model,
                "notes_analyzed": 0,
                "green_signals": [],
                "red_signals": [],
                "open_questions": [],
                "executive_summary": "No meeting notes are available for this deal.",
            }

        prompt = self._build_prompt(deal, notes)
        payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You are an investment diligence analyst. Extract concrete red and green signals "
                        "from meeting notes. Use only the supplied notes. Return valid JSON only. "
                        "Do not think step by step. Do not include reasoning."
                    ),
                },
                {"role": "user", "content": f"/no_think\n{prompt}"},
            ],
            "temperature": 0.1,
            "max_tokens": 16000,
            "response_format": self._response_format(),
            # Not part of LM Studio's documented OpenAI-compatible params, but
            # some Qwen-serving backends accept one of these switches. If LM
            # Studio rejects them, _post_chat_completion retries without them.
            "thinking": False,
            "enable_thinking": False,
            "chat_template_kwargs": {"enable_thinking": False},
            "reasoning": {"effort": "none"},
        }

        last_error = None
        errors = []
        for base_url in self.base_urls:
            try:
                data = self._post_chat_completion(base_url, payload)
                choice = (data.get("choices") or [{}])[0]
                message = choice.get("message") or {}
                content = message.get("content") or ""
                if not content.strip():
                    finish_reason = choice.get("finish_reason")
                    reasoning = message.get("reasoning_content") or message.get("reasoning") or ""
                    raise ValueError(
                        "LM Studio returned no final content. "
                        f"finish_reason={finish_reason!r}; "
                        f"reasoning_chars={len(str(reasoning))}. "
                        "For Qwen reasoning models, enable /no_think support or use a non-reasoning instruct model."
                    )
                parsed = self._parse_json(content)
                parsed = self._normalize_result(parsed)
                return {
                    "deal_id": str(deal.id),
                    "deal_title": deal.title,
                    "provider": "lm_studio",
                    "model": self.model,
                    "base_url": base_url,
                    "notes_analyzed": len(notes),
                    **parsed,
                }
            except Exception as exc:
                last_error = exc
                errors.append(f"{base_url}: {exc}")
                logger.warning("LM Studio meeting signal analysis failed via %s: %s", base_url, exc)

        raise RuntimeError(
            "LM Studio analysis failed. "
            f"Tried {len(self.base_urls)} URL(s). "
            f"Last error: {last_error}. "
            f"All errors: {' | '.join(errors)}"
        )

    def _candidate_base_urls(self) -> list[str]:
        configured = getattr(settings, "LM_STUDIO_BASE_URL", "") or ""
        if configured.strip():
            normalized = configured.strip().rstrip("/")
            if not normalized.endswith("/v1"):
                normalized = f"{normalized}/v1"
            return [normalized]

        candidates = [
            "http://host.docker.internal:1234/v1",
            "http://localhost:1234/v1",
            "http://127.0.0.1:1234/v1",
        ]
        seen = set()
        urls = []
        for url in candidates:
            normalized = str(url or "").strip().rstrip("/")
            if not normalized:
                continue
            if not normalized.endswith("/v1"):
                normalized = f"{normalized}/v1"
            if normalized not in seen:
                seen.add(normalized)
                urls.append(normalized)
        return urls

    def _post_chat_completion(self, base_url: str, payload: dict[str, Any]) -> dict[str, Any]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        response = self._post(base_url, headers, payload)
        if response.status_code == 400:
            fallback_payload = dict(payload)
            for key in ("thinking", "enable_thinking", "chat_template_kwargs", "reasoning", "response_format"):
                fallback_payload.pop(key, None)
            fallback_response = self._post(base_url, headers, fallback_payload)
            if fallback_response.status_code < 400:
                response = fallback_response
        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            detail = (response.text or "").strip()
            if len(detail) > 1000:
                detail = f"{detail[:1000]}..."
            raise requests.HTTPError(f"{exc}. Response body: {detail}", response=response) from exc
        return response.json()

    def _post(self, base_url: str, headers: dict[str, str], payload: dict[str, Any]) -> requests.Response:
        return requests.post(
            f"{base_url}/chat/completions",
            headers=headers,
            json=payload,
            timeout=(2.0, 180.0),
        )

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

        return f"""
Deal: {deal.title}

Analyze the meeting notes below and produce an investment signal summary for the deal page.

Return JSON with this exact shape:
{{
  "executive_summary": "3-5 sentence synthesis across all meetings",
  "green_signals": [
    {{
      "title": "short signal title",
      "detail": "specific fact pattern with numbers where available",
      "evidence": ["note title or note id references"],
      "confidence": "high|medium|low"
    }}
  ],
  "red_signals": [
    {{
      "title": "short signal title",
      "detail": "specific risk or concern with numbers where available",
      "evidence": ["note title or note id references"],
      "confidence": "high|medium|low"
    }}
  ],
  "open_questions": [
    "specific diligence question or missing data request"
  ]
}}

Rules:
- Use only the meeting notes.
- Prefer concrete metrics and repeated points across meetings.
- Return complete signals with concise but specific detail.
- Return at most 8 green signals and at most 8 red signals.
- Put positive diligence findings under green_signals.
- Put risks, contradictions, missing evidence, and diligence gaps under red_signals.
- Do not include markdown fences.

Meeting notes:
{chr(10).join(note_blocks)}
""".strip()

    def _parse_json(self, content: str) -> dict[str, Any]:
        raw = (content or "").strip()
        if not raw:
            raise ValueError("LM Studio returned an empty response.")
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
