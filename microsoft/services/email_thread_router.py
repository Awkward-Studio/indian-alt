from dataclasses import dataclass
from enum import StrEnum
from typing import Iterable


class EmailThreadRouteMode(StrEnum):
    PROPOSE_NEW = "PROPOSE_NEW"
    ENRICH_EXISTING = "ENRICH_EXISTING"
    BLOCKED_AMBIGUOUS = "BLOCKED_AMBIGUOUS"


@dataclass(frozen=True)
class EmailThreadRoute:
    mode: EmailThreadRouteMode
    deal_id: str | None
    evidence: tuple[dict, ...]
    error: str | None = None

    def as_dict(self) -> dict:
        return {
            "mode": self.mode.value,
            "deal_id": self.deal_id,
            "evidence": list(self.evidence),
            "error": self.error,
        }


class EmailThreadRouter:
    """Resolve create/update routing from persisted email-to-deal relationships."""

    @classmethod
    def resolve(cls, emails: Iterable) -> EmailThreadRoute:
        linked_messages: list[dict] = []
        deal_ids: set[str] = set()

        for email in emails:
            deal_id = getattr(email, "deal_id", None)
            if not deal_id:
                continue
            normalized_deal_id = str(deal_id)
            deal_ids.add(normalized_deal_id)
            linked_messages.append({
                "email_id": str(email.id),
                "deal_id": normalized_deal_id,
                "relationship": "email.deal",
            })

        if not deal_ids:
            return EmailThreadRoute(
                mode=EmailThreadRouteMode.PROPOSE_NEW,
                deal_id=None,
                evidence=(),
            )

        if len(deal_ids) == 1:
            return EmailThreadRoute(
                mode=EmailThreadRouteMode.ENRICH_EXISTING,
                deal_id=next(iter(deal_ids)),
                evidence=tuple(linked_messages),
            )

        sorted_ids = sorted(deal_ids)
        return EmailThreadRoute(
            mode=EmailThreadRouteMode.BLOCKED_AMBIGUOUS,
            deal_id=None,
            evidence=tuple(linked_messages),
            error=(
                "Conversation is linked to multiple deals; automatic analysis was blocked "
                f"to prevent cross-deal mutation: {', '.join(sorted_ids)}"
            ),
        )
