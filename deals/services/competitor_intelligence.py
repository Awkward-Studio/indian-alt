import json
import logging
import os
import re
from typing import Any

from decouple import config
from deals.models import Deal, VentureIntelligenceCompanyProfile, VentureIntelligenceCompanyRelation, VentureIntelligenceFinancialStatement, VentureIntelligenceRelationType
from deals.services.venture_intelligence import VentureIntelligenceService, is_valid_cin, normalize_cin

logger = logging.getLogger(__name__)


COMPETITOR_LIST_KEYS = ("competitors", "peers", "companies", "results")
DEMO_MODE_ENV = "VI_COMPETITOR_DEMO_MODE"


def is_competitor_demo_mode() -> bool:
    """Return whether competitor VI enrichment should use local demo VI profiles.

    This intentionally does not control competitor web search. Competitor research
    should still run through Claude web search; only the downstream VI profile
    fetch/enrichment is mocked by this flag.
    """
    value = os.environ.get(DEMO_MODE_ENV)
    if value is None:
        value = config(DEMO_MODE_ENV, default="")
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def demo_competitor_results(deal: Deal) -> list[dict[str, str]]:
    return [
        {
            "name": "Amazon India (Amazon Seller Services Pvt Ltd)",
            "cin": "U74999KA2012PTC066462",
            "notes": "Marketplace and seller services peer competing across ecommerce categories.",
        },
        {
            "name": "Meesho (Fashnear Technologies Pvt Ltd)",
            "cin": "U72900KA2015PTC082263",
            "notes": "Value-led social commerce and marketplace peer serving small sellers and mass consumers.",
        },
        {
            "name": "Myntra Designs Pvt Ltd",
            "cin": "U72200KA2007PTC041799",
            "notes": "Fashion ecommerce peer with strong private-label and brand marketplace presence.",
        },
        {
            "name": "Nykaa E-Retail Pvt Ltd",
            "cin": "U74900MH2012PTC230136",
            "notes": "Beauty and personal care ecommerce peer with omnichannel retail footprint.",
        },
    ]


def demo_competitor_report(deal: Deal, competitors: list[dict[str, str]]) -> str:
    lines = [f"# Top Competitors for {deal.title}"]
    for index, competitor in enumerate(competitors, start=1):
        lines.append(f"\n## {index}. {competitor['name']}")
        lines.append(f"- **CIN:** {competitor['cin']}")
        lines.append(f"- **Competition Rationale:** {competitor['notes']}")
    return "\n".join(lines).strip()


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

    for item in competitors[:limit]:
        name = _clean_company_name(item.get("name") if isinstance(item, dict) else "")
        supplied_cin = normalize_cin(item.get("cin") if isinstance(item, dict) else "")
        if not name:
            continue
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

    for item in resolved_competitors[:limit]:
        name = _clean_company_name(item.get("name"))
        cin = normalize_cin(item.get("cin"))
        if not name or not is_valid_cin(cin):
            failed.append({"name": name or "Unknown", "cin": cin, "error": "Missing valid resolved CIN."})
            continue

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
    if is_competitor_demo_mode():
        return enrich_demo_competitors_for_deal(deal, competitors, limit=limit)

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


def enrich_demo_competitors_for_deal(deal: Deal, competitors: list[dict[str, str]], *, limit: int = 10) -> dict[str, list[dict[str, str]]]:
    enriched = []
    failed = []
    skipped = []

    for item in competitors[:limit]:
        name = _clean_company_name(item.get("name"))
        cin = normalize_cin(item.get("cin"))
        if not cin:
            cin = normalize_cin(VentureIntelligenceService().resolve_company_identity(company_name=name).get("cin"))
        if not name or not is_valid_cin(cin):
            failed.append({"name": name or "Unknown", "error": "Demo competitor requires a valid company name or CIN."})
            continue

        existing = VentureIntelligenceCompanyRelation.objects.filter(
            deal=deal,
            relation_type=VentureIntelligenceRelationType.COMPETITOR,
            company_profile__cin=cin,
        ).select_related("company_profile").first()
        if existing:
            skipped.append({
                "name": existing.company_profile.name or name,
                "cin": existing.company_profile.cin or cin,
                "profile_id": str(existing.company_profile.id),
                "reason": "Already fetched for this deal.",
            })
            continue

        profile, _ = VentureIntelligenceCompanyProfile.objects.update_or_create(
            cin=cin,
            defaults={
                "name": name,
                "registered_name": name,
                "industry": deal.industry or "Consumer Internet",
                "sector": deal.sector or "E-Commerce",
                "city": "Bengaluru",
                "country": "India",
                "website": "https://example.com",
                "year_founded": "2015",
                "total_funding": "Demo profile",
                "business_description": item.get("notes") or "Demo competitor profile for VI workflow recording.",
                "raw_profile_json": {
                    "demo_mode": True,
                    "source": DEMO_MODE_ENV,
                    "profile": {"name": name, "cin": cin},
                },
            },
        )
        VentureIntelligenceFinancialStatement.objects.filter(company_profile=profile).delete()
        for fy, revenue in (("FY22", "850"), ("FY23", "1120"), ("FY24", "1460")):
            VentureIntelligenceFinancialStatement.objects.create(
                company_profile=profile,
                statement_type="profit_loss",
                fy=fy,
                fin_type="Standalone",
                data={"revenue": revenue, "ebitda": str(round(float(revenue) * 0.08, 1)), "pat": str(round(float(revenue) * 0.035, 1))},
            )

        relation, _ = VentureIntelligenceCompanyRelation.objects.update_or_create(
            deal=deal,
            company_profile=profile,
            defaults={"relation_type": VentureIntelligenceRelationType.COMPETITOR, "notes": item.get("notes") or ""},
        )
        enriched.append({
            "name": profile.name or name,
            "cin": profile.cin or cin,
            "profile_id": str(profile.id),
        })

    return {"enriched": enriched, "failed": failed, "skipped": skipped}


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
