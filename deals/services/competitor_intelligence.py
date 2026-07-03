import json
import logging
import re
from typing import Any

from deals.models import Deal, VentureIntelligenceCompanyRelation, VentureIntelligenceRelationType
from deals.services.screener import ScreenerCompanyService
from deals.services.venture_intelligence import VentureIntelligenceService, is_valid_cin, normalize_cin

logger = logging.getLogger(__name__)


COMPETITOR_LIST_KEYS = (
    "competitors",
    "competitor_candidates",
    "competitor_companies",
    "top_competitors",
    "public_competitors",
    "private_competitors",
    "listed_competitors",
    "unlisted_competitors",
    "peer_companies",
    "peer_list",
    "peers",
    "companies",
    "results",
    "items",
)
PUBLIC_COMPANY_TYPES = {
    "listed_public",
    "public",
    "listed",
    "publicly_listed",
    "listed_company",
    "public_company",
    "public_limited",
    "stock_exchange_listed",
    "nse_listed",
    "bse_listed",
}
PRIVATE_COMPANY_TYPES = {
    "private",
    "unlisted_private",
    "unlisted",
    "privately_held",
    "private_company",
    "unlisted_company",
    "unlisted_private_company",
}


def _extract_json_object(text: str) -> dict[str, Any]:
    cleaned = (text or "").strip()
    if not cleaned:
        return {}

    if "```" in cleaned:
        fenced = re.search(r"```(?:json)?\s*(.*?)```", cleaned, re.DOTALL | re.IGNORECASE)
        if fenced:
            cleaned = fenced.group(1).strip()

    try:
        parsed = json.loads(cleaned)
        if isinstance(parsed, dict):
            return parsed
        if isinstance(parsed, list):
            return {"competitors": parsed}
        return {}
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if not match:
            list_match = re.search(r"\[.*\]", cleaned, re.DOTALL)
            if list_match:
                try:
                    parsed = json.loads(list_match.group(0))
                    return {"competitors": parsed} if isinstance(parsed, list) else {}
                except json.JSONDecodeError:
                    return {}
            return {}
        try:
            parsed = json.loads(match.group(0))
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}


def _clean_company_name(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""

    text = re.sub(r"^#{1,6}\s*", "", text)
    text = re.sub(r"^\s*(?:[-*]|\d{1,2}[.)])\s*", "", text)
    text = re.sub(r"^\s*(?:company\s*name|name)\s*:\s*", "", text, flags=re.IGNORECASE)
    text = text.replace("**", "").replace("__", "").replace("`", "")
    text = re.sub(r"\s+", " ", text).strip()

    text = text.replace(chr(8211), "-").replace(chr(8212), "-")
    for separator in (" - ", " | ", "\t"):
        if separator in text:
            text = text.split(separator, 1)[0].strip()

    text = text.strip(" :;,.-")
    if not text or len(text) > 120:
        return ""
    if text.lower() in {"company", "competitor", "competitors", "peer", "peers"}:
        return ""
    return text


def _competitor_name_from_item(item: Any) -> str:
    if isinstance(item, str):
        return _clean_company_name(item)
    if not isinstance(item, dict):
        return ""

    for key in (
        "company_name",
        "competitor_name",
        "peer_name",
        "name",
        "companyName",
        "entity_name",
        "registered_name",
        "company",
        "entity",
        "organization",
        "organisation",
        "brand",
    ):
        name = _clean_company_name(item.get(key))
        if name:
            return name
    return ""


def _notes_from_item(item: Any) -> str:
    if not isinstance(item, dict):
        return ""

    parts = []
    for key in (
        "core_business",
        "business_description",
        "description",
        "nature_of_competition",
        "rationale",
        "reason",
        "competition_rationale",
        "why_competitor",
        "scale_indicators",
        "recent_developments",
    ):
        value = item.get(key)
        if value:
            parts.append(f"{key.replace('_', ' ').title()}: {value}")
    return "\n".join(parts)


def _normalize_company_type(value: Any) -> str:
    text = re.sub(r"[^a-z_]+", "_", str(value or "").strip().lower()).strip("_")
    if text in PUBLIC_COMPANY_TYPES:
        return "listed_public"
    if text in PRIVATE_COMPANY_TYPES:
        return "private"
    return "unknown"


def _normalize_confidence(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        parsed = float(value)
        if parsed > 1:
            parsed = parsed / 100
        return max(0, min(parsed, 1))
    except (TypeError, ValueError):
        return None


def _competitor_metadata_from_item(item: Any, *, include_cin: bool = True) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "notes": _notes_from_item(item),
        "cin": "",
        "company_type": "unknown",
        "classification_confidence": None,
        "exchange": "",
        "ticker": "",
        "screener_url": "",
        "classification_source": "",
    }
    if not isinstance(item, dict):
        return metadata

    if include_cin:
        metadata["cin"] = str(item.get("cin") or item.get("resolved_cin") or "").strip()
    metadata["company_type"] = _normalize_company_type(
        item.get("company_type")
        or item.get("listing_type")
        or item.get("listing_status")
        or item.get("public_private_status")
        or item.get("ownership_type")
    )
    metadata["classification_confidence"] = _normalize_confidence(
        item.get("classification_confidence") or item.get("confidence")
    )
    metadata["exchange"] = str(item.get("exchange") or item.get("stock_exchange") or "").strip().upper()
    metadata["ticker"] = str(item.get("ticker") or item.get("symbol") or item.get("stock_symbol") or "").strip().upper()
    metadata["screener_url"] = str(item.get("screener_url") or item.get("screener_link") or "").strip()
    metadata["classification_source"] = str(
        item.get("classification_source") or item.get("listing_evidence") or item.get("source") or ""
    ).strip()
    if metadata["ticker"] or metadata["exchange"] or metadata["screener_url"]:
        metadata["company_type"] = "listed_public"
    return metadata


def _looks_like_competitor_item(item: Any) -> bool:
    return bool(_competitor_name_from_item(item))


def _find_competitor_items(payload: Any) -> list[Any]:
    if isinstance(payload, list):
        if any(_looks_like_competitor_item(item) for item in payload):
            return payload
        for item in payload:
            nested = _find_competitor_items(item)
            if nested:
                return nested
        return []

    if not isinstance(payload, dict):
        return []

    grouped_items: list[Any] = []
    for key in COMPETITOR_LIST_KEYS:
        value = payload.get(key)
        if isinstance(value, list):
            if any(_looks_like_competitor_item(item) for item in value):
                grouped_items.extend(value)
            else:
                nested = _find_competitor_items(value)
                if nested:
                    grouped_items.extend(nested)
    if grouped_items:
        return grouped_items

    for value in payload.values():
        nested = _find_competitor_items(value)
        if nested:
            return nested
    return []


def competitor_names_from_payload(payload: Any, *, limit: int = 10, include_cin: bool = True) -> list[dict[str, str]]:
    if isinstance(payload, str):
        parsed = _extract_json_object(payload)
        if parsed:
            return competitor_names_from_payload(parsed, limit=limit, include_cin=include_cin)
        return competitor_names_from_text(payload, limit=limit)

    items = _find_competitor_items(payload)

    results = []
    seen = set()
    for item in items:
        name = _competitor_name_from_item(item)
        key = name.casefold()
        if not name or key in seen:
            continue
        seen.add(key)
        results.append({
            "name": name,
            **_competitor_metadata_from_item(item, include_cin=include_cin),
        })
        if len(results) >= limit:
            break
    return results


def competitor_names_from_text(text: str, *, limit: int = 10) -> list[dict[str, str]]:
    results = []
    seen = set()
    lines = (text or "").splitlines()

    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue

        name = ""
        table_cells = [cell.strip() for cell in stripped.strip("|").split("|")] if "|" in stripped else []
        if len(table_cells) >= 2 and not re.match(r"^-+$", table_cells[0].replace(" ", "")):
            headerish = "company" in table_cells[0].lower() or "competitor" in table_cells[0].lower()
            if not headerish:
                name = _clean_company_name(table_cells[0])

        if not name and re.match(r"^\s*(?:[-*]|\d{1,2}[.)])\s+", stripped):
            name = _clean_company_name(stripped)

        if not name:
            heading_match = re.match(r"^#{1,6}\s+(?:\d{1,2}[.)]\s*)?(.+)$", stripped)
            if heading_match and index > 0:
                name = _clean_company_name(heading_match.group(1))

        key = name.casefold()
        if not name or key in seen:
            continue
        seen.add(key)
        results.append({"name": name, "notes": "", "cin": "", "company_type": "unknown"})
        if len(results) >= limit:
            break

    return results


def format_competitor_report(payload: dict[str, Any]) -> str:
    competitors = competitor_names_from_payload(payload, limit=10)
    if not competitors:
        return ""

    raw_items = []
    for key in COMPETITOR_LIST_KEYS:
        value = payload.get(key)
        if isinstance(value, list):
            raw_items = value
            break

    lines = ["# Top 10 Competitors"]
    for index, competitor in enumerate(competitors, start=1):
        raw_item = raw_items[index - 1] if index - 1 < len(raw_items) else {}
        lines.append(f"\n## {index}. {competitor['name']}")
        if isinstance(raw_item, dict):
            for label, key in (
                ("Core Business", "core_business"),
                ("Competition Rationale", "nature_of_competition"),
                ("Scale / Recent Developments", "scale_indicators"),
                ("Recent Developments", "recent_developments"),
            ):
                value = raw_item.get(key)
                if value:
                    lines.append(f"- **{label}:** {value}")
        elif competitor.get("notes"):
            lines.append(competitor["notes"])
    return "\n".join(lines).strip()


def _existing_competitor_relations(deal: Deal):
    existing_relations = VentureIntelligenceCompanyRelation.objects.filter(
        deal=deal,
        relation_type=VentureIntelligenceRelationType.COMPETITOR,
    ).select_related("company_profile")
    existing_by_name = {
        (relation.company_profile.name or "").strip().casefold(): relation
        for relation in existing_relations
        if relation.company_profile.name
    }
    existing_by_cin = {
        (relation.company_profile.cin or "").strip().upper(): relation
        for relation in existing_relations
        if relation.company_profile.cin
    }
    existing_by_ticker = {
        f"{(relation.company_profile.exchange or '').strip().upper()}:{(relation.company_profile.ticker or '').strip().upper()}": relation
        for relation in existing_relations
        if relation.company_profile.ticker
    }
    return existing_by_name, existing_by_cin, existing_by_ticker


def _route_from_competitor(item: dict[str, Any], *, resolved_cin: str = "") -> str:
    company_type = _normalize_company_type(item.get("company_type"))
    cin = normalize_cin(resolved_cin or item.get("cin"))
    if company_type == "listed_public" or cin.startswith("L"):
        return "public_screener"
    if company_type == "private" or cin.startswith("U"):
        return "private_vi"
    if item.get("ticker") or item.get("exchange") or item.get("screener_url"):
        return "public_screener"
    return "unknown"


def resolve_competitor_cins_for_deal(deal: Deal, competitors: list[dict[str, str]], *, limit: int = 10) -> dict[str, list[dict[str, str]]]:
    vi_service = VentureIntelligenceService()
    resolved = []
    failed = []
    skipped = []
    target_key = (deal.title or "").strip().casefold()
    existing_by_name, existing_by_cin, _existing_by_ticker = _existing_competitor_relations(deal)
    seen_names = set()
    seen_cins = set()

    for item in competitors[:limit]:
        name = _clean_company_name(item.get("name") if isinstance(item, dict) else "")
        supplied_cin = normalize_cin(item.get("cin") if isinstance(item, dict) else "")
        if not name:
            continue
        name_key = name.casefold()
        if name_key in seen_names:
            continue
        seen_names.add(name_key)
        if target_key and name.casefold() == target_key:
            failed.append({"name": name, "error": "Skipped target company name."})
            continue

        existing = existing_by_cin.get(supplied_cin) if supplied_cin else None
        existing = existing or existing_by_name.get(name.casefold())
        if existing:
            skipped.append({
                "name": existing.company_profile.name or name,
                "cin": existing.company_profile.cin or supplied_cin,
                "profile_id": str(existing.company_profile.id),
                "reason": "Already fetched for this deal.",
            })
            continue

        try:
            if supplied_cin and is_valid_cin(supplied_cin):
                resolution = vi_service.resolve_company_identity(company_name=name, cin=supplied_cin)
            else:
                resolution = vi_service.resolve_company_identity(company_name=name, cin=None)
            resolved_cin = normalize_cin(resolution.get("cin"))
            if not resolution.get("is_valid") or not is_valid_cin(resolved_cin):
                failed.append({
                    "name": name,
                    "error": resolution.get("error") or "Could not resolve a valid CIN.",
                    "resolution": resolution,
                })
                continue
            if resolved_cin in seen_cins:
                skipped.append({
                    "name": name,
                    "cin": resolved_cin,
                    "reason": "Duplicate resolved CIN in this selection.",
                })
                continue
            seen_cins.add(resolved_cin)
            resolved.append({
                "name": name,
                "cin": resolved_cin,
                "entity_name": resolution.get("entity_name") or name,
                "confidence": resolution.get("confidence"),
                "source": resolution.get("source") or "",
                "notes": item.get("notes") or "",
                "company_type": "listed_public" if resolved_cin.startswith("L") else "private",
                "source_route": "public_screener" if resolved_cin.startswith("L") else "private_vi",
                "resolution": resolution,
            })
        except Exception as exc:
            logger.warning("Failed to resolve CIN for competitor '%s' on deal %s: %s", name, deal.id, exc)
            failed.append({"name": name, "error": str(exc)})

    return {"resolved": resolved, "failed": failed, "skipped": skipped}


def fetch_competitor_vi_profiles_for_deal(deal: Deal, resolved_competitors: list[dict[str, str]], *, limit: int = 10) -> dict[str, list[dict[str, str]]]:
    vi_service = VentureIntelligenceService()
    enriched = []
    failed = []
    skipped = []
    existing_by_name, existing_by_cin, _existing_by_ticker = _existing_competitor_relations(deal)
    seen_cins = set()

    for item in resolved_competitors[:limit]:
        name = _clean_company_name(item.get("name"))
        cin = normalize_cin(item.get("cin"))
        if not name or not is_valid_cin(cin):
            failed.append({"name": name or "Unknown", "cin": cin, "error": "Missing valid resolved CIN."})
            continue
        if cin in seen_cins:
            skipped.append({"name": name, "cin": cin, "reason": "Duplicate resolved CIN in this selection."})
            continue
        seen_cins.add(cin)

        existing = existing_by_cin.get(cin) or existing_by_name.get(name.casefold())
        if existing:
            skipped.append({
                "name": existing.company_profile.name or name,
                "cin": existing.company_profile.cin or cin,
                "profile_id": str(existing.company_profile.id),
                "reason": "Already fetched for this deal.",
            })
            continue

        try:
            profile = vi_service.enrich_deal(
                deal_id=deal.id,
                company_name=item.get("entity_name") or name,
                cin=cin,
                relation_type=VentureIntelligenceRelationType.COMPETITOR,
                index_for_rag=False,
            )
            relation = VentureIntelligenceCompanyRelation.objects.filter(
                deal=deal,
                company_profile=profile,
                relation_type=VentureIntelligenceRelationType.COMPETITOR,
            ).first()
            if relation and item.get("notes"):
                relation.notes = item["notes"]
                relation.save(update_fields=["notes"])
            enriched.append({
                "name": profile.name or name,
                "cin": profile.cin or cin,
                "profile_id": str(profile.id),
                "source_route": "private_vi",
                "data_source": getattr(profile, "data_source", "venture_intelligence"),
            })
        except Exception as exc:
            logger.warning("Failed to fetch VI profile for competitor '%s' (%s) on deal %s: %s", name, cin, deal.id, exc)
            failed.append({"name": name, "cin": cin, "error": str(exc), "source_route": "private_vi"})

    return {"enriched": enriched, "failed": failed, "skipped": skipped}


def fetch_competitor_screener_profiles_for_deal(deal: Deal, public_competitors: list[dict[str, Any]], *, limit: int = 10) -> dict[str, list[dict[str, Any]]]:
    screener_service = ScreenerCompanyService()
    enriched = []
    failed = []
    skipped = []
    existing_by_name, _existing_by_cin, existing_by_ticker = _existing_competitor_relations(deal)
    seen_keys = set()

    for item in public_competitors[:limit]:
        name = _clean_company_name(item.get("name"))
        ticker = str(item.get("ticker") or "").strip().upper()
        exchange = str(item.get("exchange") or "").strip().upper()
        if not name:
            continue
        ticker_key = f"{exchange}:{ticker}" if ticker else ""
        dedupe_key = ticker_key or name.casefold()
        if dedupe_key in seen_keys:
            skipped.append({"name": name, "ticker": ticker, "reason": "Duplicate public competitor in this selection.", "source_route": "public_screener"})
            continue
        seen_keys.add(dedupe_key)

        existing = existing_by_ticker.get(ticker_key) if ticker_key else None
        existing = existing or existing_by_name.get(name.casefold())
        if existing:
            skipped.append({
                "name": existing.company_profile.name or name,
                "cin": existing.company_profile.cin or "",
                "ticker": existing.company_profile.ticker or ticker,
                "exchange": existing.company_profile.exchange or exchange,
                "profile_id": str(existing.company_profile.id),
                "reason": "Already fetched for this deal.",
                "source_route": "public_screener",
            })
            continue

        try:
            profile = screener_service.save_public_competitor(deal, item)
            enriched.append({
                "name": profile.name or name,
                "cin": profile.cin or "",
                "ticker": profile.ticker or ticker,
                "exchange": profile.exchange or exchange,
                "profile_id": str(profile.id),
                "source_route": "public_screener",
                "data_source": "screener",
            })
        except Exception as exc:
            logger.warning("Failed to fetch Screener profile for competitor '%s' on deal %s: %s", name, deal.id, exc)
            failed.append({"name": name, "ticker": ticker, "exchange": exchange, "error": str(exc), "source_route": "public_screener"})

    return {"enriched": enriched, "failed": failed, "skipped": skipped}


def enrich_competitors_for_deal(deal: Deal, competitors: list[dict[str, str]], *, limit: int = 10) -> dict[str, list[dict[str, str]]]:
    public_items = []
    private_candidates = []
    unknown_candidates = []
    for item in competitors[:limit]:
        route = _route_from_competitor(item)
        if route == "public_screener":
            public_items.append(item)
        elif route == "private_vi":
            private_candidates.append(item)
        else:
            unknown_candidates.append(item)

    unknown_resolution = resolve_competitor_cins_for_deal(deal, unknown_candidates, limit=limit) if unknown_candidates else {"resolved": [], "failed": [], "skipped": []}
    for resolved_item in unknown_resolution["resolved"]:
        if _route_from_competitor(resolved_item, resolved_cin=resolved_item.get("cin")) == "public_screener":
            public_items.append(resolved_item)
        else:
            private_candidates.append(resolved_item)

    cin_resolution = resolve_competitor_cins_for_deal(deal, private_candidates, limit=limit) if private_candidates else {"resolved": [], "failed": [], "skipped": []}
    vi_fetch = fetch_competitor_vi_profiles_for_deal(deal, cin_resolution["resolved"], limit=limit)
    screener_fetch = fetch_competitor_screener_profiles_for_deal(deal, public_items, limit=limit)
    enriched = [*vi_fetch["enriched"], *screener_fetch["enriched"]]
    failed = [*unknown_resolution["failed"], *cin_resolution["failed"], *vi_fetch["failed"], *screener_fetch["failed"]]
    skipped = [*unknown_resolution["skipped"], *cin_resolution["skipped"], *vi_fetch["skipped"], *screener_fetch["skipped"]]
    return {
        "enriched": enriched,
        "failed": failed,
        "skipped": skipped,
        "steps": {
            "classification": {
                "public_screener": len(public_items),
                "private_vi": len(private_candidates),
                "unknown_resolved": len(unknown_resolution["resolved"]),
            },
            "unknown_cin_resolution": unknown_resolution,
            "cin_resolution": cin_resolution,
            "vi_fetch": vi_fetch,
            "screener_fetch": screener_fetch,
            "private_vi": {
                "enriched": vi_fetch["enriched"],
                "failed": [*cin_resolution["failed"], *vi_fetch["failed"]],
                "skipped": [*cin_resolution["skipped"], *vi_fetch["skipped"]],
            },
            "public_screener": screener_fetch,
        },
    }


def annotate_existing_competitors(deal: Deal, competitors: list[dict[str, str]]) -> list[dict[str, str]]:
    existing_relations = VentureIntelligenceCompanyRelation.objects.filter(
        deal=deal,
        relation_type=VentureIntelligenceRelationType.COMPETITOR,
    ).select_related("company_profile")
    existing_by_name = {
        (relation.company_profile.name or "").strip().casefold(): relation
        for relation in existing_relations
        if relation.company_profile.name
    }
    existing_by_cin = {
        (relation.company_profile.cin or "").strip().upper(): relation
        for relation in existing_relations
        if relation.company_profile.cin
    }
    existing_by_ticker = {
        f"{(relation.company_profile.exchange or '').strip().upper()}:{(relation.company_profile.ticker or '').strip().upper()}": relation
        for relation in existing_relations
        if relation.company_profile.ticker
    }
    annotated = []
    for item in competitors:
        name = _clean_company_name(item.get("name"))
        cin = str(item.get("cin") or "").strip().upper()
        ticker = str(item.get("ticker") or "").strip().upper()
        exchange = str(item.get("exchange") or "").strip().upper()
        ticker_key = f"{exchange}:{ticker}" if ticker else ""
        existing = existing_by_cin.get(cin) if cin else None
        existing = existing or (existing_by_ticker.get(ticker_key) if ticker_key else None)
        existing = existing or existing_by_name.get(name.casefold())
        company_type = _normalize_company_type(item.get("company_type"))
        if existing and getattr(existing.company_profile, "company_type", ""):
            company_type = existing.company_profile.company_type
        elif ticker or exchange or str(item.get("screener_url") or "").strip() or cin.startswith("L"):
            company_type = "listed_public"
        elif cin.startswith("U"):
            company_type = "private"
        annotated.append({
            **item,
            "name": name,
            "cin": existing.company_profile.cin if existing else cin,
            "company_type": company_type,
            "classification_confidence": item.get("classification_confidence"),
            "exchange": existing.company_profile.exchange if existing else exchange,
            "ticker": existing.company_profile.ticker if existing else ticker,
            "screener_url": existing.company_profile.screener_url if existing else item.get("screener_url", ""),
            "already_fetched": bool(existing),
            "profile_id": str(existing.company_profile.id) if existing else "",
        })
    return annotated
