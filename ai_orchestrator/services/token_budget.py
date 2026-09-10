from __future__ import annotations

import re


def estimate_tokens(value: str) -> int:
    """Conservative token estimate for prose, tables, numbers, and JSON."""
    text = str(value or "")
    lexical = len(re.findall(r"\w+|[^\w\s]", text, flags=re.UNICODE))
    return max((len(text) + 2) // 3, int(lexical * 1.2))
