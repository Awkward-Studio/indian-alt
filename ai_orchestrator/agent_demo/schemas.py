from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any


ALLOWED_ACTIONS = {
    "search_deals",
    "retrieve_chunks",
    "verify_evidence",
    "final_answer",
}


class AgentActionError(ValueError):
    pass


@dataclass
class AgentAction:
    action: str
    arguments: dict[str, Any] = field(default_factory=dict)
    reason: str = ""
    confidence: float = 0.0

    @classmethod
    def from_model_text(cls, text: str) -> "AgentAction":
        payload = _extract_json_object(text)
        if not isinstance(payload, dict):
            raise AgentActionError("Model output must be a JSON object.")

        action = str(payload.get("action") or "").strip()
        if action not in ALLOWED_ACTIONS:
            raise AgentActionError(
                f"Unknown action '{action}'. Allowed actions: {', '.join(sorted(ALLOWED_ACTIONS))}."
            )

        arguments = payload.get("arguments") or {}
        if not isinstance(arguments, dict):
            raise AgentActionError("'arguments' must be an object.")

        confidence = payload.get("confidence", 0.0)
        try:
            confidence = float(confidence)
        except (TypeError, ValueError):
            confidence = 0.0

        return cls(
            action=action,
            arguments=arguments,
            reason=str(payload.get("reason") or "").strip(),
            confidence=max(0.0, min(confidence, 1.0)),
        )


def _extract_json_object(text: str) -> dict[str, Any]:
    raw = (text or "").strip()
    if not raw:
        raise AgentActionError("Model returned an empty response.")

    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, flags=re.DOTALL | re.IGNORECASE)
    if fenced:
        raw = fenced.group(1).strip()

    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass

    start = raw.find("{")
    end = raw.rfind("}")
    if start < 0 or end <= start:
        raise AgentActionError("Could not find a JSON object in model output.")

    try:
        return json.loads(raw[start : end + 1])
    except json.JSONDecodeError as exc:
        raise AgentActionError(f"Invalid JSON: {exc}") from exc

