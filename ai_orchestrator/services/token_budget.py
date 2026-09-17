from __future__ import annotations

import re


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
