"""Safe matching helpers for OneDrive deal folders and Deal titles."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any, Iterable


_LEGAL_SUFFIXES = {
    "co",
    "company",
    "inc",
    "limited",
    "llc",
    "llp",
    "ltd",
    "pvt",
    "private",
}
_IGNORED_PREFIXES = {"deal", "project"}
_GENERIC_NAME_TOKENS = {
    "capital",
    "company",
    "enterprise",
    "finance",
    "financial",
    "group",
    "health",
    "healthcare",
    "hospital",
    "hospitals",
    "india",
    "industry",
    "industries",
    "investment",
    "investments",
    "services",
    "solution",
    "solutions",
    "technology",
    "technologies",
    "ventures",
}


def normalize_folder_deal_name(value: Any) -> str:
    """Return a conservative comparison form for a folder or deal name."""
    if value is None:
        return ""

    text = unicodedata.normalize("NFKD", str(value)).casefold()
    text = "".join(char for char in text if unicodedata.category(char) != "Mn")
    text = text.replace("&", " and ")
    text = re.sub(r"[^\w]+", " ", text, flags=re.UNICODE)
    tokens = [token for token in text.split() if token]

    while tokens and tokens[0] in _IGNORED_PREFIXES:
        tokens.pop(0)
    while tokens and tokens[-1] in _LEGAL_SUFFIXES:
        tokens.pop()

    return " ".join(tokens)


def _meaningful_tokens(value: str) -> set[str]:
    return {
        token
        for token in normalize_folder_deal_name(value).split()
        if len(token) >= 3 and token not in _IGNORED_PREFIXES
    }


@dataclass(frozen=True)
class NameMatch:
    score: float
    reason: str


@dataclass(frozen=True)
class _PreparedName:
    normalized: str
    compact: str
    tokens: frozenset[str]


def score_folder_deal_name(folder_name: str, deal_title: str) -> NameMatch:
    """Score a folder/title pair, returning zero for unsafe weak matches."""
    return _score_prepared(_prepare_name(folder_name), _prepare_name(deal_title))


def _prepare_name(value: Any) -> _PreparedName:
    normalized = normalize_folder_deal_name(value)
    return _PreparedName(
        normalized=normalized,
        compact=normalized.replace(" ", ""),
        tokens=frozenset(
            token
            for token in normalized.split()
            if len(token) >= 3
            and token not in _IGNORED_PREFIXES
            and token not in _GENERIC_NAME_TOKENS
        ),
    )


def _score_prepared(folder: _PreparedName, deal: _PreparedName) -> NameMatch:
    if not folder.normalized or not deal.normalized:
        return NameMatch(0.0, "empty name")

    if folder.normalized == deal.normalized:
        return NameMatch(1.0, "normalized exact match")

    if folder.compact == deal.compact and len(folder.compact) >= 5:
        return NameMatch(0.98, "compact exact match")

    folder_tokens = folder.tokens
    deal_tokens = deal.tokens
    overlap = folder_tokens & deal_tokens
    if overlap and (folder_tokens <= deal_tokens or deal_tokens <= folder_tokens):
        distinctive = max((len(token) for token in overlap), default=0)
        if distinctive >= 4 or len(overlap) >= 2:
            return NameMatch(0.94, "distinctive token containment")

    if min(len(folder.compact), len(deal.compact)) >= 6:
        ratio = SequenceMatcher(None, folder.compact, deal.compact).ratio()
        if ratio >= 0.92:
            return NameMatch(round(0.90 + (ratio - 0.92) / 2, 4), "high name similarity")

    return NameMatch(0.0, "no high-confidence match")


class FolderDealMatcher:
    """Index deal names once, then rank folders without repeated preparation."""

    def __init__(self, deals: Iterable[Any]):
        self.prepared_deals = [
            (deal, _prepare_name(getattr(deal, "title", "")))
            for deal in deals
        ]
        self.token_index: dict[str, set[int]] = {}
        self.exact_index: dict[str, set[int]] = {}
        self.compact_index: dict[str, set[int]] = {}
        for index, (_deal, prepared) in enumerate(self.prepared_deals):
            if prepared.normalized:
                self.exact_index.setdefault(prepared.normalized, set()).add(index)
            if prepared.compact:
                self.compact_index.setdefault(prepared.compact, set()).add(index)
            for token in prepared.tokens:
                self.token_index.setdefault(token, set()).add(index)

    def rank(self, folder_name: str) -> list[tuple[NameMatch, Any]]:
        folder = _prepare_name(folder_name)
        candidate_indexes = set(self.exact_index.get(folder.normalized, set()))
        candidate_indexes.update(self.compact_index.get(folder.compact, set()))
        for token in folder.tokens:
            candidate_indexes.update(self.token_index.get(token, set()))

        matches = []
        for index in candidate_indexes:
            deal, prepared = self.prepared_deals[index]
            match = _score_prepared(folder, prepared)
            if match.score:
                matches.append((match, deal))

        return sorted(matches, key=lambda item: (-item[0].score, str(getattr(item[1], "id", ""))))


def rank_folder_matches(
    folder_name: str,
    deals: Iterable[Any],
) -> list[tuple[NameMatch, Any]]:
    """Return high-confidence deal candidates in descending score order."""
    return FolderDealMatcher(deals).rank(folder_name)
