from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Iterable


@dataclass(frozen=True)
class EmailThreadOriginator:
    email_id: str
    address: str
    position: int
    evidence: str = "oldest_external_sender"

    def as_dict(self) -> dict:
        return asdict(self)


class EmailThreadOriginatorResolver:
    """Find the oldest sender outside the mailbox that received the thread."""

    @classmethod
    def resolve(cls, messages: Iterable[object]) -> EmailThreadOriginator | None:
        ordered = sorted(list(messages), key=cls._sort_key)
        mailbox_addresses = {
            str(getattr(getattr(message, "email_account", None), "email", None) or "").strip().casefold()
            for message in ordered
        }
        mailbox_addresses.discard("")

        for position, message in enumerate(ordered):
            address = str(getattr(message, "from_email", None) or "").strip().casefold()
            if address and address not in mailbox_addresses:
                return EmailThreadOriginator(
                    email_id=str(getattr(message, "id", "")),
                    address=address,
                    position=position,
                )
        return None

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
