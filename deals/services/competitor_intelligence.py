import json
import logging
import re
from typing import Any

from deals.models import Deal, VentureIntelligenceCompanyRelation, VentureIntelligenceRelationType
from deals.services.venture_intelligence import VentureIntelligenceService, is_valid_cin, normalize_cin

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


def competitor_names_from_payload(payload: Any, *, limit: int = 10, include_cin: bool = True) -> list[dict[str, str]]:
    if isinstance(payload, str):
        parsed = _extract_json_object(payload)
        if parsed:
            return competitor_names_from_payload(parsed, limit=limit, include_cin=include_cin)
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
        cin = ""
        if include_cin and isinstance(item, dict):
            cin = str(item.get("cin") or item.get("resolved_cin") or "").strip()
        results.append({"name": name, "notes": _notes_from_item(item), "cin": cin})
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
    return existing_by_name, existing_by_cin


def resolve_competitor_cins_for_deal(deal: Deal, competitors: list[dict[str, str]], *, limit: int = 10) -> dict[str, list[dict[str, str]]]:
    vi_service = VentureIntelligenceService()
    resolved = []
    failed = []
    skipped = []
    target_key = (deal.title or "").strip().casefold()
    existing_by_name, existing_by_cin = _existing_competitor_relations(deal)
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
    existing_by_name, existing_by_cin = _existing_competitor_relations(deal)
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
            })
        except Exception as exc:
            logger.warning("Failed to fetch VI profile for competitor '%s' (%s) on deal %s: %s", name, cin, deal.id, exc)
            failed.append({"name": name, "cin": cin, "error": str(exc)})

    return {"enriched": enriched, "failed": failed, "skipped": skipped}


def enrich_competitors_for_deal(deal: Deal, competitors: list[dict[str, str]], *, limit: int = 10) -> dict[str, list[dict[str, str]]]:
    cin_resolution = resolve_competitor_cins_for_deal(deal, competitors, limit=limit)
    vi_fetch = fetch_competitor_vi_profiles_for_deal(deal, cin_resolution["resolved"], limit=limit)
    return {
        "enriched": vi_fetch["enriched"],
        "failed": [*cin_resolution["failed"], *vi_fetch["failed"]],
        "skipped": [*cin_resolution["skipped"], *vi_fetch["skipped"]],
        "steps": {
            "cin_resolution": cin_resolution,
            "vi_fetch": vi_fetch,
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
    annotated = []
    for item in competitors:
        name = _clean_company_name(item.get("name"))
        cin = str(item.get("cin") or "").strip().upper()
        existing = existing_by_cin.get(cin) if cin else None
        existing = existing or existing_by_name.get(name.casefold())
        annotated.append({
            **item,
            "name": name,
            "cin": existing.company_profile.cin if existing else cin,
            "already_fetched": bool(existing),
            "profile_id": str(existing.company_profile.id) if existing else "",
        })
    return annotated
