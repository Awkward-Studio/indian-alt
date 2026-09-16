import re

from deals.models import DealStatus


DEAL_STATUS_ALIASES = {
    "new": DealStatus.NEW,
    "interesting": DealStatus.INTERESTING,
    "semi interesting": DealStatus.SEMI_INTERESTING,
    "semi-interesting": DealStatus.SEMI_INTERESTING,
    "to pass": DealStatus.TO_PASS,
    "to be passed": DealStatus.TO_PASS,
    "to be pass": DealStatus.TO_PASS,
    "passed": DealStatus.PASSED,
    "invested": DealStatus.PORTFOLIO,
    "portfolio": DealStatus.PORTFOLIO,
}


def normalize_deal_status(value, *, default=None):
    normalized = " ".join(str(value or "").split())
    status = DEAL_STATUS_ALIASES.get(normalized.casefold())
    if status:
        return status
    match = re.fullmatch(r"(\d+):\s*.+", normalized)
    if match:
        return DealStatus.NEW if int(match.group(1)) == 1 else DealStatus.INTERESTING
    if default is not None and not normalized:
        return default
    raise ValueError(f"Unknown deal status: {value!r}")
