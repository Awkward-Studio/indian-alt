from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from email.utils import parseaddr
import re
from typing import Iterable

from bs4 import BeautifulSoup


@dataclass(frozen=True)
class EmailThreadOriginator:
    email_id: str
    address: str
    position: int
    name: str = ""
    evidence: str = "oldest_external_sender"

    def as_dict(self) -> dict:
        return asdict(self)


class EmailThreadOriginatorResolver:
    """Find the original external sender, including forwarded-message headers."""

    INTERNAL_DOMAINS = {"india-alt.com", "india-alternatives.com"}
    EMAIL_PATTERN = re.compile(r"[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}", re.IGNORECASE)
    FORWARDED_FROM_PATTERN = re.compile(
        r"(?ims)^\s*From:\s*(?P<value>.+?)(?=^\s*(?:Sent|Date|To|Cc|Subject):|\Z)"
    )

    @classmethod
    def resolve(cls, messages: Iterable[object]) -> EmailThreadOriginator | None:
        ordered = sorted(list(messages), key=cls._sort_key)
        mailbox_addresses = {
            str(getattr(getattr(message, "email_account", None), "email", None) or "").strip().casefold()
            for message in ordered
        }
        mailbox_addresses.discard("")

        for position, message in enumerate(ordered):
            # Microsoft Graph stores a forwarded message as one mailbox item.
            # Its real external sender often exists only in the embedded
            # ``From: Name <email>`` header, sometimes split across lines.
            for name, embedded_address in cls._forwarded_senders(message):
                if cls._is_external(embedded_address, mailbox_addresses):
                    return EmailThreadOriginator(
                        email_id=str(getattr(message, "id", "")),
                        address=embedded_address,
                        position=position,
                        name=name,
                        evidence="forwarded_message_header",
                    )
            address = str(getattr(message, "from_email", None) or "").strip().casefold()
            if cls._is_external(address, mailbox_addresses):
                return EmailThreadOriginator(
                    email_id=str(getattr(message, "id", "")),
                    address=address,
                    position=position,
                    name="",
                )
        return None

    @classmethod
    def _is_external(cls, address: str, mailbox_addresses: set[str]) -> bool:
        normalized = str(address or "").strip().casefold()
        if not normalized or normalized in mailbox_addresses or "@" not in normalized:
            return False
        return normalized.rsplit("@", 1)[-1] not in cls.INTERNAL_DOMAINS

    @classmethod
    def _forwarded_senders(cls, message: object) -> list[tuple[str, str]]:
        body = str(getattr(message, "body_text", None) or "")
        if not body:
            html = str(getattr(message, "body_html", None) or "")
            if html:
                body = BeautifulSoup(html, "html.parser").get_text("\n")

        senders: list[tuple[str, str]] = []
        for match in cls.FORWARDED_FROM_PATTERN.finditer(body.replace("\r", "")):
            value = " ".join(match.group("value").split())
            address_match = cls.EMAIL_PATTERN.search(value)
            if not address_match:
                continue
            address = address_match.group(0).casefold()
            parsed_name, _ = parseaddr(value)
            name = parsed_name.strip().strip('"')
            if not name:
                name = value[:address_match.start()].strip(" <>\t\n\"")
            senders.append((name, address))
        return senders

    @staticmethod
    def _sort_key(message: object) -> tuple:
        timestamp = (
            getattr(message, "date_received", None)
            or getattr(message, "date_sent", None)
            or getattr(message, "created_date_time", None)
            or getattr(message, "created_at", None)
            or datetime.min.replace(tzinfo=timezone.utc)
        )
        return timestamp, str(getattr(message, "id", ""))
