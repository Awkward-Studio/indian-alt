from __future__ import annotations

import re
import json
from django.conf import settings


def large_context_enabled() -> bool:
    return getattr(settings, 'AI_INFERENCE_TARGET', '') == 'h100'


def pipeline_input_budget(output_tokens: int = 16384) -> int:
    """Reserve response and request framing inside the selected model window."""
    window = int(getattr(settings, 'CHAT_MODEL_CONTEXT_TOKENS', 65536))
    configured = int(getattr(settings, 'AI_PIPELINE_INPUT_TOKENS', 229376))
    return max(1000, min(configured, window - int(output_tokens) - 4096))


def pipeline_evidence_budget(output_tokens: int = 16384) -> int:
    return max(1000, pipeline_input_budget(output_tokens) - 16384)


def expanded_context_chars(legacy_limit: int) -> int:
    # Character caps bound retrieval work; the provider checks actual estimated
    # tokens of the complete request, including history and instructions.
    return pipeline_evidence_budget() * 2 if large_context_enabled() else legacy_limit


class ContextBudgetExceeded(ValueError):
    """Raised when a complete serialized model request cannot fit its window."""

    def __init__(
        self,
        *,
        estimated_input_tokens: int,
        max_output_tokens: int,
        reserve_tokens: int,
        context_window_tokens: int,
    ) -> None:
        self.estimated_input_tokens = int(estimated_input_tokens)
        self.max_output_tokens = int(max_output_tokens)
        self.reserve_tokens = int(reserve_tokens)
        self.context_window_tokens = int(context_window_tokens)
        super().__init__(
            "The complete chat request exceeds the safe model context budget. "
            "Please shorten the question/history or remove extra context; "
            "no document text was silently discarded. "
            f"Estimated input={self.estimated_input_tokens}, "
            f"output allowance={self.max_output_tokens}, reserve={self.reserve_tokens}, "
            f"window={self.context_window_tokens}."
        )


class ModelOutputTruncated(ValueError):
    """Raised when a document evidence response exhausts its output allowance."""

    def __init__(self, *, finish_reason: str, response_chars: int) -> None:
        self.finish_reason = str(finish_reason)
        self.response_chars = int(response_chars)
        super().__init__(
            f"Incomplete segment response: finish_reason={self.finish_reason}; "
            f"response_chars={self.response_chars}."
        )


def estimate_tokens(value: str) -> int:
    """Conservative token estimate for prose, tables, numbers, and JSON."""
    text = str(value or "")
    lexical = len(re.findall(r"\w+|[^\w\s]", text, flags=re.UNICODE))
    return max((len(text) + 2) // 3, int(lexical * 1.2))


def estimate_message_tokens(value: str) -> int:
    """Include string escaping used by the complete-request safety check."""
    return estimate_tokens(json.dumps(str(value or ''), ensure_ascii=False))


def report_evidence_budget(*, context_window: int, input_budget: int, output_budget: int,
                           evidence_budget: int, extra_context: str = '') -> int:
    # Instructions, rendered deal data, calculator turns and request framing
    # need space beyond the retrieved source blocks. The provider still checks
    # the complete request, including every calculator continuation.
    available = min(evidence_budget, input_budget - 16_384,
                    context_window - output_budget - 4096 - 24_576)
    return max(4000, available - (estimate_message_tokens(extra_context) if extra_context else 0))
