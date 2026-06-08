import json
import logging
import re
from typing import Any

from deals.models import Deal, VentureIntelligenceCompanyRelation, VentureIntelligenceRelationType
from deals.services.venture_intelligence import VentureIntelligenceService

logger = logging.getLogger(__name__)


COMPETITOR_LIST_KEYS = ("competitors", "peers", "companies", "results")


def _extract_json_object(text: str) -> dict[str, Any]:
    cleaned = (text or "").strip()
    if not cleaned:
        return {}

    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if not match:
            return {}
        try:
            return json.loads(match.group(0))
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

    for key in ("company_name", "name", "entity_name", "registered_name", "company"):
        name = _clean_company_name(item.get(key))
        if name:
            return name
    return ""


def _notes_from_item(item: Any) -> str:
    if not isinstance(item, dict):
        return ""

    parts = []
    for key in ("core_business", "business_description", "description", "nature_of_competition", "rationale", "scale_indicators", "recent_developments"):
        value = item.get(key)
        if value:
            parts.append(f"{key.replace('_', ' ').title()}: {value}")
    return "\n".join(parts)


def competitor_names_from_payload(payload: Any, *, limit: int = 10) -> list[dict[str, str]]:
    if isinstance(payload, str):
        parsed = _extract_json_object(payload)
        if parsed:
            return competitor_names_from_payload(parsed, limit=limit)
        return competitor_names_from_text(payload, limit=limit)

    items = []
    if isinstance(payload, dict):
        for key in COMPETITOR_LIST_KEYS:
            if isinstance(payload.get(key), list):
                items = payload[key]
                break
    elif isinstance(payload, list):
        items = payload

    results = []
    seen = set()
    for item in items:
        name = _competitor_name_from_item(item)
        key = name.casefold()
        if not name or key in seen:
            continue
        seen.add(key)
        results.append({"name": name, "notes": _notes_from_item(item)})
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
        results.append({"name": name, "notes": ""})
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


def enrich_competitors_for_deal(deal: Deal, competitors: list[dict[str, str]], *, limit: int = 10) -> dict[str, list[dict[str, str]]]:
    vi_service = VentureIntelligenceService()
    enriched = []
    failed = []
    target_key = (deal.title or "").strip().casefold()

    for item in competitors[:limit]:
        name = _clean_company_name(item.get("name"))
        if not name:
            continue
        if target_key and name.casefold() == target_key:
            failed.append({"name": name, "error": "Skipped target company name."})
            continue

        try:
            profile = vi_service.enrich_deal(
                deal_id=deal.id,
                company_name=name,
                relation_type=VentureIntelligenceRelationType.COMPETITOR,
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
                "cin": profile.cin or "",
                "profile_id": str(profile.id),
            })
        except Exception as exc:
            logger.warning("Failed to enrich competitor '%s' for deal %s: %s", name, deal.id, exc)
            failed.append({"name": name, "error": str(exc)})

    return {"enriched": enriched, "failed": failed}
