"""Conservative extraction of explicit factual assertions from saved source text."""
import re

# These deliberately narrow phrases avoid treating absence of a fact as denial.
ASSERTIONS = (
    ("audited_accounts", r"(?:financial statements|accounts) (?:are|were|have been) audited", r"(?:financial statements|accounts) (?:are|were|have) (?:not audited|unaudited)|(?:financial statements|accounts) have not been audited"),
    ("pending_litigation", r"(?:the company|we) (?:has|have|faces?|is facing) pending (?:litigation|legal proceedings)", r"(?:the company|we) (?:has|have|faces?) no pending (?:litigation|legal proceedings)"),
    ("regulatory_approval", r"(?:the company|we) (?:has|have) (?:received|obtained) regulatory approval", r"(?:the company|we) (?:has|have) not (?:received|obtained) regulatory approval"),
    ("debt_default", r"(?:the company|we) (?:has|have) defaulted on (?:its |our )?debt", r"(?:the company|we) (?:has|have) not defaulted on (?:its |our )?debt"),
    ("customer_exclusivity", r"(?:the company|we) (?:has|have) exclusive customer contracts", r"(?:the company|we) (?:has|have) no exclusive customer contracts"),
)
UNSETTLED = re.compile(r"\b(?:if|may|might|could|would|should|expects?|plans?|targets?|forecasts?|believes?|estimates?)\b", re.I)


def extract_assertions(text):
    """Yield metric, polarity and an exact source quote, excluding hypothetical language."""
    for passage in re.split(r"(?<=[.!?])\s+|[\n;]+", str(text or "")):
        passage = passage.strip()
        if not passage or len(passage) > 1000 or UNSETTLED.search(passage):
            continue
        for metric, positive, negative in ASSERTIONS:
            yes = bool(re.search(r"\b(?:" + positive + r")\b", passage, re.I))
            no = bool(re.search(r"\b(?:" + negative + r")\b", passage, re.I))
            if yes != no:
                yield metric, yes, passage
