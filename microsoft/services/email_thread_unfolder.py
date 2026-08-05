"""Deterministic extraction of the new text introduced by email replies."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Iterable

from bs4 import BeautifulSoup


@dataclass(frozen=True)
class ThreadMessageDelta:
    email_id: str
    position: int
    text: str
    source_length: int
    delta_length: int
    strategy: str
    warnings: tuple[str, ...] = ()

    def as_dict(self) -> dict:
        data = asdict(self)
        data["warnings"] = list(self.warnings)
        return data


class EmailThreadUnfolder:
    """Turn stored full reply bodies into chronological, non-overlapping deltas."""

    _PLAIN_REPLY_BOUNDARIES = (
        re.compile(r"(?im)^\s*On .{3,300} wrote:\s*$"),
        re.compile(r"(?im)^\s*-{2,}\s*Original Message\s*-{2,}\s*$"),
        re.compile(r"(?im)^\s*_{5,}\s*$\n\s*From:\s*.+$"),
        re.compile(
            r"(?im)^\s*From:\s*.+\n\s*(?:Sent|Date):\s*.+\n"
            r"\s*To:\s*.+(?:\n\s*Cc:\s*.+)?\n\s*Subject:\s*.+$"
        ),
    )
    _HTML_QUOTE_SELECTORS = (
        "blockquote",
        ".gmail_quote",
        ".gmail_extra",
        "#divRplyFwdMsg",
        "[data-marker='__QUOTED_TEXT__']",
    )

    @classmethod
    def unfold(cls, messages: Iterable[object]) -> list[ThreadMessageDelta]:
        ordered = sorted(messages, key=cls._sort_key)
        prior_bodies: list[str] = []
        deltas: list[ThreadMessageDelta] = []

        for position, message in enumerate(ordered):
            source, html_quote_removed = cls._message_text(message)
            source = cls._clean_text(source)
            subject = (getattr(message, "subject", None) or "").strip().lower()
            is_reply = subject.startswith(("re:", "aw:", "sv:"))
            delta, strategy = cls._strip_plain_reply(source, is_reply=is_reply)
            if html_quote_removed:
                strategy = "html_quote"

            delta, repeated_strategy = cls._strip_repeated_history(delta, prior_bodies)
            if repeated_strategy:
                strategy = repeated_strategy

            warnings: list[str] = []
            if source and not delta:
                strategy = "duplicate"
            elif not source:
                strategy = "empty"
            elif len(delta) < 8 and len(source) > len(delta):
                warnings.append("very_short_delta")

            deltas.append(
                ThreadMessageDelta(
                    email_id=str(getattr(message, "id", "")),
                    position=position,
                    text=delta,
                    source_length=len(source),
                    delta_length=len(delta),
                    strategy=strategy,
                    warnings=tuple(warnings),
                )
            )
            if source:
                prior_bodies.append(source)

        return deltas

    @staticmethod
    def _sort_key(message: object) -> tuple:
        dt = (
            getattr(message, "date_received", None)
            or getattr(message, "date_sent", None)
            or getattr(message, "created_date_time", None)
            or getattr(message, "created_at", None)
            or datetime.min.replace(tzinfo=timezone.utc)
        )
        return dt, str(getattr(message, "id", ""))

    @classmethod
    def _message_text(cls, message: object) -> tuple[str, bool]:
        html = getattr(message, "body_html", None) or ""
        if html:
            soup = BeautifulSoup(html, "html.parser")
            removed = False
            for selector in cls._HTML_QUOTE_SELECTORS:
                for quoted in soup.select(selector):
                    quoted.decompose()
                    removed = True
            for node in soup.select("style, script, head"):
                node.decompose()
            return soup.get_text(separator="\n", strip=True), removed

        return (
            getattr(message, "body_text", None)
            or getattr(message, "body_preview", None)
            or ""
        ), False

    @staticmethod
    def _clean_text(text: str) -> str:
        text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
        text = re.sub(r"[ \t]+\n", "\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    @classmethod
    def _strip_plain_reply(cls, text: str, *, is_reply: bool = True) -> tuple[str, str]:
        earliest = None
        patterns = cls._PLAIN_REPLY_BOUNDARIES if is_reply else cls._PLAIN_REPLY_BOUNDARIES[:1]
        for pattern in patterns:
            match = pattern.search(text)
            if match and (earliest is None or match.start() < earliest):
                earliest = match.start()
        if earliest is None:
            return text, "full_body"
        return cls._clean_text(text[:earliest]), "reply_boundary"

    @classmethod
    def _strip_repeated_history(cls, text: str, prior_bodies: list[str]) -> tuple[str, str | None]:
        normalized = cls._clean_text(text)
        if not normalized:
            return "", None

        for previous in reversed(prior_bodies):
            previous = cls._clean_text(previous)
            if len(previous) < 24:
                continue
            if normalized == previous:
                return "", "duplicate"
            index = normalized.find(previous)
            if index >= 0:
                prefix = cls._clean_text(normalized[:index])
                if prefix:
                    return prefix, "repeated_history"
        return normalized, None
