import os
import json
import logging
import re
import requests
from django.conf import settings
from django.db import transaction
from django.utils import timezone
from decouple import config
from deals.models import (
    Deal, 
    VentureIntelligenceCompanyProfile, 
    VentureIntelligenceFinancialStatement, 
    VentureIntelligenceCompanyRelation,
    VentureIntelligenceRelationType,
    VentureIntelligenceExecutive,
    VentureIntelligencePEInvestment,
    VentureIntelligencePEExit,
    VentureIntelligencePEIPO,
    VentureIntelligenceAngelInvestment,
    VentureIntelligenceIncubationInvestment,
    VentureIntelligenceMergerAcquisition,
    VentureIntelligenceEpfoData,
    VentureIntelligenceSimilarCompany
)
from contacts.models import Contact
from ai_orchestrator.services.ai_processor import AIProcessorService
from ai_orchestrator.services.embedding_processor import EmbeddingService

logger = logging.getLogger(__name__)

CIN_PATTERN = re.compile(r"^[A-Z]\d{5}[A-Z]{2}\d{4}[A-Z]{3}\d{6}$")
TRAILING_DEAL_WORDS_PATTERN = re.compile(
    r"\b(?:test\s+deal|deal|mandate|project|transaction|opportunity)\b\s*$",
    re.IGNORECASE,
)
VI_DEMO_MODE_ENV = "VI_COMPETITOR_DEMO_MODE"
DEMO_CIN_BY_NAME = {
    "amazon": "U74999KA2012PTC066462",
    "meesho": "U72900KA2015PTC082263",
    "fashnear": "U72900KA2015PTC082263",
    "myntra": "U72200KA2007PTC041799",
    "nykaa": "U74900MH2012PTC230136",
}


def is_vi_demo_mode():
    value = os.environ.get(VI_DEMO_MODE_ENV)
    if value is None:
        value = config(VI_DEMO_MODE_ENV, default="")
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def normalize_cin(value):
    return re.sub(r"[^A-Z0-9]", "", str(value or "").upper())


def is_valid_cin(value):
    return bool(CIN_PATTERN.match(normalize_cin(value)))


def company_name_candidates(company_name):
    raw_name = str(company_name or "").strip()
    if not raw_name:
        return []

    candidates = [raw_name]
    without_brackets = re.sub(r"\s*[\(\[].*?[\)\]]\s*", " ", raw_name).strip()
    candidates.append(without_brackets)

    for separator in [" - ", " | ", " / ", ":"]:
        if separator in without_brackets:
            candidates.append(without_brackets.split(separator, 1)[0].strip())

    stripped = TRAILING_DEAL_WORDS_PATTERN.sub("", without_brackets).strip(" -_/|:")
    candidates.append(stripped)

    unique_candidates = []
    seen = set()
    for candidate in candidates:
        normalized = re.sub(r"\s+", " ", candidate).strip()
        key = normalized.casefold()
        if normalized and key not in seen:
            seen.add(key)
            unique_candidates.append(normalized)
    return unique_candidates


def demo_cin_for_company(company_name=None, cin=None):
    supplied = normalize_cin(cin)
    if supplied:
        return supplied

    name = str(company_name or "").casefold()
    for key, value in DEMO_CIN_BY_NAME.items():
        if key in name:
            return value
    return "U74999KA2012PTC066462"


def demo_company_details(company_name=None, cin=None, entity_name=None):
    resolved_cin = demo_cin_for_company(company_name=company_name or entity_name, cin=cin)
    display_name = company_name or entity_name or f"Demo VI Company {resolved_cin}"
    registered_name = display_name
    if resolved_cin == "U74999KA2012PTC066462":
        display_name = "Amazon India"
        registered_name = "Amazon Seller Services Pvt Ltd"
    elif resolved_cin == "U72900KA2015PTC082263":
        display_name = "Meesho"
        registered_name = "Fashnear Technologies Pvt Ltd"
    elif resolved_cin == "U72200KA2007PTC041799":
        display_name = "Myntra"
        registered_name = "Myntra Designs Pvt Ltd"
    elif resolved_cin == "U74900MH2012PTC230136":
        display_name = "Nykaa"
        registered_name = "Nykaa E-Retail Pvt Ltd"

    return {
        "success": True,
        "results": {
            "profile": {
                "cin": resolved_cin,
                "name": display_name,
                "registered_name": registered_name,
                "website": "https://example.com",
                "industry": "Consumer Internet",
                "sector": "E-Commerce",
                "email": "demo@example.com",
                "year_founded": "2015",
                "city": {"name": "Bengaluru", "state": "Karnataka", "region": "South", "country": "India"},
                "total_funding": "Demo VI funding profile",
                "management_info": [
                    {"name": "Demo CEO", "designation": "CEO", "belongs_to_firm_name": display_name}
                ],
                "board_info": [
                    {"name": "Demo Director", "designation": "Director", "belongs_to_firm_name": display_name}
                ],
            },
            "cfs_profile": {
                "full_name": registered_name,
                "business_description": f"Demo VI profile for {display_name}, used for workflow recording.",
                "incorp_year": 2015,
                "company_status": "Active",
                "address": "Demo Business Park",
                "pincode": "560001",
            },
            "profit_loss": [
                {"fy": "FY22", "fin_type": "Standalone", "revenue": "850", "ebitda": "68", "pat": "29.8"},
                {"fy": "FY23", "fin_type": "Standalone", "revenue": "1120", "ebitda": "89.6", "pat": "39.2"},
                {"fy": "FY24", "fin_type": "Standalone", "revenue": "1460", "ebitda": "116.8", "pat": "51.1"},
            ],
            "balance_sheet": [
                {"fy": "FY24", "fin_type": "Standalone", "assets": "640", "net_worth": "210"},
            ],
            "cash_flow": [
                {"fy": "FY24", "fin_type": "Standalone", "operating_cash": "74"},
            ],
            "similar_cos": [],
        },
    }

class VentureIntelligenceService:
    def __init__(self):
        self.api_key = getattr(settings, "VENTURE_INTELLIGENCE_API_KEY", os.environ.get("VENTURE_INTELLIGENCE_API_KEY", ""))
        self.base_url = getattr(settings, "VENTURE_INTELLIGENCE_BASE_URL", "https://api-hub.ventureintelligence.com")

    def fetch_company_details(self, company_name=None, cin=None, entity_name=None):
        if is_vi_demo_mode():
            return demo_company_details(company_name=company_name, cin=cin, entity_name=entity_name)

        if not self.api_key:
            raise ValueError("Venture Intelligence API key is not configured.")
        
        url = f"{self.base_url.rstrip('/')}/vendor/company-full/"
        headers = {
            "x-api-key": self.api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
        payload = {}
        if cin:
            payload["cin"] = cin
        elif company_name:
            payload["company_name"] = company_name
        elif entity_name:
            payload["entity_name"] = entity_name
        else:
            raise ValueError("At least one search parameter (cin, company_name, entity_name) is required.")

        response = requests.post(url, headers=headers, json=payload, timeout=30)
        
        if response.status_code == 404:
            raise ValueError(f"Company details not found for query: {payload}")
            
        response.raise_for_status()
        data = response.json()
        if isinstance(data, dict) and "results" not in data:
            data = {
                "success": True,
                "results": data
            }
        return data

    def _extract_json_object(self, text: str) -> dict:
        cleaned = (text or "").strip()
        if not cleaned:
            return {}

        if "```" in cleaned:
            match = re.search(r"\{.*\}", cleaned, re.DOTALL)
            if match:
                cleaned = match.group(0)

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

    def normalize_cin_resolution(self, payload: dict, *, source: str = "anthropic_web_search") -> dict:
        cin = normalize_cin(payload.get("cin"))
        confidence = payload.get("confidence")
        try:
            confidence = float(confidence) if confidence is not None else None
        except (TypeError, ValueError):
            confidence = None

        return {
            "cin": cin if is_valid_cin(cin) else "",
            "entity_name": payload.get("entity_name") or payload.get("registered_name") or payload.get("company_name") or "",
            "confidence": confidence,
            "source": payload.get("source") or source,
            "raw": payload,
            "is_valid": is_valid_cin(cin),
        }

    def normalize_cin_candidates(self, payload: dict, *, source: str = "anthropic_web_search") -> list[dict]:
        raw_candidates = payload.get("candidates")
        if not isinstance(raw_candidates, list):
            raw_candidates = [payload]

        candidates = []
        seen = set()
        for raw_candidate in raw_candidates:
            if not isinstance(raw_candidate, dict):
                continue

            candidate = self.normalize_cin_resolution(raw_candidate, source=source)
            cin = candidate.get("cin")
            if not candidate.get("is_valid") or not cin or cin in seen:
                continue
            seen.add(cin)
            candidate["rationale"] = raw_candidate.get("rationale") or raw_candidate.get("reason") or ""
            candidates.append(candidate)

        candidates.sort(
            key=lambda item: item.get("confidence") if item.get("confidence") is not None else 0,
            reverse=True,
        )
        return candidates

    def resolve_cin_candidates_via_ai(self, company_name: str) -> list[dict]:
        """
        Uses Anthropic's Claude with native web search to resolve ranked MCA CIN candidates.
        """
        if is_vi_demo_mode():
            cin = demo_cin_for_company(company_name=company_name)
            return [{
                "cin": cin,
                "entity_name": company_name,
                "confidence": 1.0,
                "source": "demo_mode",
                "raw": {"demo_mode": True},
                "is_valid": True,
                "rationale": "Demo mode CIN resolution",
            }]

        ai_service = AIProcessorService()
        # Force using Anthropic to leverage native web search tool
        ai_service.model_provider = "anthropic"
        ai_service.current_provider = ai_service.anthropic_provider
        
        prompt = (
            f"Search the web to find ranked official 21-character Corporate Identity Number (CIN) candidates "
            f"issued by the Ministry of Corporate Affairs (MCA) in India for the company or brand: \"{company_name}\".\n"
            f"Use official MCA/company registry evidence when available. If the company has multiple Indian legal entities, return each plausible Indian legal entity.\n"
            f"Prefer the operating company most likely to match a Venture Intelligence company profile, but do not collapse multiple entities into one answer.\n"
            f"Return ONLY a JSON object in this format:\n"
            f"{{\n  \"cin\": \"U74999KA2012PTC066107\",\n  \"entity_name\": \"Flipkart Private Limited\",\n  \"confidence\": 0.95\n}}\n"
            f"Better format when multiple entities exist:\n"
            f"{{\n  \"candidates\": [\n"
            f"    {{\"cin\": \"U51909KA2011PTC060489\", \"entity_name\": \"FLIPKART INDIA PRIVATE LIMITED\", \"confidence\": 0.95, \"rationale\": \"Indian operating entity\"}},\n"
            f"    {{\"cin\": \"U51109KA2007PTC041957\", \"entity_name\": \"FLIPKART INTERNET PRIVATE LIMITED\", \"confidence\": 0.85, \"rationale\": \"Related ecommerce marketplace entity\"}}\n"
            f"  ]\n}}\n"
            f"Do not return any markdown code blocks, explanations, or extra text."
        )

        try:
            result = ai_service.process_content(
                content=prompt,
                skill_name="universal_chat",
                source_type="deal_enrichment",
                source_id="cin_resolution",
                metadata={"model_provider": "anthropic", "temperature": 0.0},
                stream=False
            )
            response_text = result.get("response", "").strip()
            candidates = self.normalize_cin_candidates(self._extract_json_object(response_text))
            if not candidates:
                logger.warning("AI web search did not return a valid CIN for '%s': %s", company_name, response_text[:500])
            return candidates
        except Exception as e:
            logger.error(f"Failed to resolve CIN via AI web search: {e}", exc_info=True)
            return []

    def resolve_cin_via_ai(self, company_name: str) -> dict:
        """
        Compatibility wrapper that returns the highest-confidence AI CIN candidate.
        """
        candidates = self.resolve_cin_candidates_via_ai(company_name)
        if candidates:
            return candidates[0]
        return {
            "cin": "",
            "entity_name": "",
            "confidence": None,
            "source": "anthropic_web_search",
            "raw": {},
            "is_valid": False,
        }

    def resolve_company_identity(self, company_name=None, cin=None, *, min_confidence=0.6) -> dict:
        """
        Resolve a company to a VI-usable identifier.

        Priority:
        1. Use supplied valid CIN.
        2. Use Anthropic web search to resolve the MCA CIN.
        3. Fall back to VI direct company-name lookup only if CIN resolution fails.
        """
        if is_vi_demo_mode():
            resolved_cin = demo_cin_for_company(company_name=company_name, cin=cin)
            return {
                "cin": resolved_cin,
                "entity_name": company_name or "Demo VI Company",
                "confidence": 1.0,
                "source": "demo_mode",
                "is_valid": True,
                "cin_candidates": [{
                    "cin": resolved_cin,
                    "entity_name": company_name or "Demo VI Company",
                    "confidence": 1.0,
                    "source": "demo_mode",
                    "is_valid": True,
                    "rationale": "Demo mode CIN resolution",
                }],
                "vi_data": demo_company_details(company_name=company_name, cin=resolved_cin),
            }

        if cin:
            normalized = normalize_cin(cin)
            is_valid = is_valid_cin(normalized)
            supplied_candidate = {
                "cin": normalized if is_valid else "",
                "entity_name": company_name or "",
                "confidence": 1.0 if is_valid else 0.0,
                "source": "user_supplied_cin",
                "is_valid": is_valid,
                "rationale": "CIN supplied manually",
            }
            return {
                "cin": normalized,
                "entity_name": company_name or "",
                "confidence": supplied_candidate["confidence"],
                "source": "user_supplied_cin",
                "is_valid": is_valid,
                "cin_candidates": [supplied_candidate] if is_valid else [],
                "vi_data": None,
            }

        if not company_name:
            return {
                "cin": "",
                "entity_name": "",
                "confidence": None,
                "source": "missing_input",
                "is_valid": False,
                "vi_data": None,
            }

        candidates = company_name_candidates(company_name)
        for lookup_name in candidates:
            ai_candidates = []
            for ai_candidate in self.resolve_cin_candidates_via_ai(lookup_name):
                confidence = ai_candidate.get("confidence")
                if confidence is not None and confidence < min_confidence:
                    continue
                ai_candidates.append(ai_candidate)

            if ai_candidates:
                ai_resolution = ai_candidates[0]
                return {
                    **ai_resolution,
                    "query_name": lookup_name,
                    "cin_candidates": ai_candidates,
                    "vi_data": None,
                }

        last_lookup_error = None
        for lookup_name in candidates:
            try:
                vi_data = self.fetch_company_details(company_name=lookup_name)
                profile = vi_data.get("results", {}).get("profile", {}) or {}
                direct_cin = normalize_cin(profile.get("cin"))
                if is_valid_cin(direct_cin):
                    return {
                        "cin": direct_cin,
                        "entity_name": profile.get("registered_name") or profile.get("name") or lookup_name,
                        "confidence": 1.0,
                        "source": "venture_intelligence_name_lookup",
                        "is_valid": True,
                        "vi_data": vi_data,
                    }
                return {
                    "cin": direct_cin,
                    "entity_name": profile.get("registered_name") or profile.get("name") or lookup_name,
                    "confidence": 0.5,
                    "source": "venture_intelligence_name_lookup_invalid_cin",
                    "is_valid": False,
                    "vi_data": vi_data,
                }
            except Exception as exc:
                last_lookup_error = exc

        logger.info(
            "AI CIN resolution failed for '%s' using candidates %s, and direct VI lookup also failed. Last VI error: %s",
            company_name,
            candidates,
            last_lookup_error,
        )

        return {
            "cin": "",
            "entity_name": company_name,
            "confidence": None,
            "source": "unresolved",
            "raw": {"last_vi_error": str(last_lookup_error) if last_lookup_error else ""},
            "is_valid": False,
            "vi_data": None,
        }

    def fetch_resolved_company_details(self, company_name=None, cin=None):
        resolution = self.resolve_company_identity(company_name=company_name, cin=cin)
        return self.fetch_company_details_from_resolution(resolution, company_name=company_name)

    def fetch_company_details_from_resolution(self, resolution, company_name=None):
        if resolution.get("vi_data"):
            return resolution["vi_data"], resolution
        if not resolution.get("is_valid") or not resolution.get("cin"):
            raise ValueError("Could not resolve a valid Corporate Identity Number (CIN).")

        cin_errors = []
        cin_candidates = resolution.get("cin_candidates")
        if not isinstance(cin_candidates, list) or not cin_candidates:
            cin_candidates = [resolution]

        seen_cins = set()
        for cin_candidate in cin_candidates:
            candidate_cin = normalize_cin(cin_candidate.get("cin"))
            if not candidate_cin or candidate_cin in seen_cins:
                continue
            seen_cins.add(candidate_cin)
            try:
                data = self.fetch_company_details(cin=candidate_cin)
                profile = data.get("results", {}).get("profile", {}) or {}
                return data, {
                    **resolution,
                    **cin_candidate,
                    "cin": candidate_cin,
                    "entity_name": profile.get("registered_name") or profile.get("name") or cin_candidate.get("entity_name") or resolution.get("entity_name"),
                    "source": cin_candidate.get("source") or resolution.get("source"),
                    "is_valid": True,
                }
            except Exception as exc:
                cin_errors.append(f"{candidate_cin}: {exc}")

        if resolution.get("source") == "user_supplied_cin":
            raise ValueError(f"VI lookup failed for supplied CIN: {resolution.get('cin')}")

        try:
            fallback_names = [
                resolution.get("entity_name"),
                *company_name_candidates(company_name),
            ]
            tried = set()
            for fallback_name in fallback_names:
                key = str(fallback_name or "").strip().casefold()
                if not key or key in tried:
                    continue
                tried.add(key)
                try:
                    data = self.fetch_company_details(company_name=fallback_name)
                    profile = data.get("results", {}).get("profile", {}) or {}
                    fallback_cin = normalize_cin(profile.get("cin"))
                    if is_valid_cin(fallback_cin):
                        resolution = {
                            **resolution,
                            "cin": fallback_cin,
                            "entity_name": profile.get("registered_name") or profile.get("name") or fallback_name,
                            "source": "venture_intelligence_name_lookup_after_cin_miss",
                            "is_valid": True,
                    }
                    return data, resolution
                except Exception:
                    continue
            raise ValueError(f"VI lookup failed for resolved CIN candidates: {cin_errors}")
        except Exception:
            raise ValueError(f"VI lookup failed for resolved CIN candidates: {cin_errors}")

    @transaction.atomic
    def enrich_deal(self, deal_id, company_name=None, cin=None, relation_type='target', index_for_rag=True):
        """
        Queries VI (resolving CIN via AI web search if necessary) and saves profile and financials in the DB.
        """
        deal = Deal.objects.get(id=deal_id)
        
        # 1. Resolve identity and fetch full details using CIN whenever possible.
        vi_data, resolution = self.fetch_resolved_company_details(company_name=company_name, cin=cin)
        resolved_cin = resolution.get("cin") or cin
        company_name = resolution.get("entity_name") or company_name
        results = vi_data.get("results", {})
        profile_data = results.get("profile", {})
        cfs_profile_data = results.get("cfs_profile", {}) or {}
        
        # 3. Extract location information
        city_dict = profile_data.get("city") or cfs_profile_data.get("city") or {}
        city_name = None
        state_val = None
        region_val = None
        country_val = None
        if isinstance(city_dict, dict):
            city_name = city_dict.get("name")
            state_val = city_dict.get("state")
            region_val = city_dict.get("region")
            country_val = city_dict.get("country")
        elif isinstance(city_dict, str):
            city_name = city_dict

        # Create or update VentureIntelligenceCompanyProfile
        cin_val = profile_data.get("cin") or resolved_cin
        vi_profile, _ = VentureIntelligenceCompanyProfile.objects.update_or_create(
            cin=cin_val,
            defaults={
                "name": profile_data.get("name") or company_name or deal.title,
                "registered_name": profile_data.get("registered_name") or cfs_profile_data.get("full_name"),
                "website": profile_data.get("website") or cfs_profile_data.get("website"),
                "industry": profile_data.get("industry") or cfs_profile_data.get("industry"),
                "sector": profile_data.get("sector") or cfs_profile_data.get("sector"),
                "email": profile_data.get("email") or cfs_profile_data.get("email"),
                "year_founded": profile_data.get("year_founded") or (str(cfs_profile_data.get("incorp_year")) if cfs_profile_data.get("incorp_year") else None),
                "city": city_name,
                "total_funding": profile_data.get("total_funding"),
                
                # New Location & Contact fields
                "state": state_val,
                "region": region_val,
                "country": country_val,
                "pincode": cfs_profile_data.get("pincode"),
                "telephone": profile_data.get("telephone"),
                "phone": profile_data.get("phone") or cfs_profile_data.get("phone"),
                "linkedin": profile_data.get("linkedin") or cfs_profile_data.get("linkedin"),
                
                # New Profile & Status fields
                "tags": profile_data.get("tags"),
                "listing_status": profile_data.get("listing_status") or cfs_profile_data.get("listing_status"),
                "additional_info": profile_data.get("additional_info"),
                
                "short_name": cfs_profile_data.get("short_name"),
                "previous_name": cfs_profile_data.get("previous_name"),
                "full_name": cfs_profile_data.get("full_name"),
                "business_description": cfs_profile_data.get("business_description"),
                "transacted_status": cfs_profile_data.get("transacted_status"),
                "incorp_year": cfs_profile_data.get("incorp_year"),
                "company_status": cfs_profile_data.get("company_status"),
                "address": cfs_profile_data.get("address"),
                "address_line2": cfs_profile_data.get("address_line2"),
                "contact_name": cfs_profile_data.get("contact_name"),
                "contact_designation": cfs_profile_data.get("contact_designation"),
                "auditor_name": cfs_profile_data.get("auditor_name"),
                
                # New Shareholding & Tech fields
                "shp_year": cfs_profile_data.get("shp_year"),
                "shp_promoter": cfs_profile_data.get("shp_promoter"),
                "shp_non_promoter": cfs_profile_data.get("shp_non_promoter"),
                "is_xbrl": cfs_profile_data.get("is_xbrl"),
                
                "raw_profile_json": vi_data
            }
        )

        # 4. Save Financials
        # Clear existing financials for this profile
        VentureIntelligenceFinancialStatement.objects.filter(company_profile=vi_profile).delete()
        
        # Populate P&L
        for pl in results.get("profit_loss", []) or []:
            if not pl.get("fy"):
                continue
            VentureIntelligenceFinancialStatement.objects.create(
                company_profile=vi_profile,
                statement_type="profit_loss",
                fy=pl.get("fy"),
                fin_type=pl.get("fin_type", "Standalone"),
                data=pl
            )
            
        # Populate Balance Sheets
        for bs in results.get("balance_sheet", []) or []:
            if not bs.get("fy"):
                continue
            VentureIntelligenceFinancialStatement.objects.create(
                company_profile=vi_profile,
                statement_type="balance_sheet",
                fy=bs.get("fy"),
                fin_type=bs.get("fin_type", "Standalone"),
                data=bs
            )

        # Populate Cash Flow
        for cf in results.get("cash_flow", []) or []:
            if not cf.get("fy"):
                continue
            VentureIntelligenceFinancialStatement.objects.create(
                company_profile=vi_profile,
                statement_type="cash_flow",
                fy=cf.get("fy"),
                fin_type=cf.get("fin_type", "Standalone"),
                data=cf
            )

        # 5. Populate and Save child arrays
        VentureIntelligenceExecutive.objects.filter(company_profile=vi_profile).delete()
        VentureIntelligencePEInvestment.objects.filter(company_profile=vi_profile).delete()
        VentureIntelligenceAngelInvestment.objects.filter(company_profile=vi_profile).delete()
        VentureIntelligenceIncubationInvestment.objects.filter(company_profile=vi_profile).delete()
        VentureIntelligencePEExit.objects.filter(company_profile=vi_profile).delete()
        VentureIntelligencePEIPO.objects.filter(company_profile=vi_profile).delete()
        VentureIntelligenceMergerAcquisition.objects.filter(company_profile=vi_profile).delete()
        VentureIntelligenceEpfoData.objects.filter(company_profile=vi_profile).delete()
        VentureIntelligenceSimilarCompany.objects.filter(company_profile=vi_profile).delete()

        # Executives
        for exec_data in (profile_data.get("management_info", []) or []):
            if not exec_data.get("name"):
                continue
            VentureIntelligenceExecutive.objects.create(
                company_profile=vi_profile,
                name=exec_data.get("name"),
                designation=exec_data.get("designation"),
                belongs_to_firm_name=exec_data.get("belongs_to_firm_name"),
                role_type='management'
            )
        for exec_data in (profile_data.get("board_info", []) or []):
            if not exec_data.get("name"):
                continue
            VentureIntelligenceExecutive.objects.create(
                company_profile=vi_profile,
                name=exec_data.get("name"),
                designation=exec_data.get("designation"),
                belongs_to_firm_name=exec_data.get("belongs_to_firm_name"),
                role_type='board'
            )

        # Private Equity
        pe_data = results.get("private_equity", {}) or {}
        for inv in (pe_data.get("pe_investments", []) or []):
            VentureIntelligencePEInvestment.objects.create(
                company_profile=vi_profile,
                round=inv.get("round"),
                deal_date=inv.get("deal_date"),
                amount=inv.get("amount"),
                amount_inr=inv.get("amount_inr"),
                investors=inv.get("investors") or [],
                exit_status=inv.get("exit_status"),
                company_valuation_post_money=inv.get("company_valuation_post_money"),
                revenue_multiple_post_money=inv.get("revenue_multiple_post_money"),
                is_vc=inv.get("is_vc"),
                is_amount_hide=inv.get("is_amount_hide"),
                is_debt_deal=inv.get("is_debt_deal"),
                is_agg_hide=inv.get("is_agg_hide")
            )
        for inv in (pe_data.get("angel_investments", []) or []):
            VentureIntelligenceAngelInvestment.objects.create(
                company_profile=vi_profile,
                date=inv.get("date"),
                investors=inv.get("investors") or [],
                is_exited=inv.get("is_exited"),
                is_agg_hide=inv.get("is_agg_hide")
            )
        for inv in (pe_data.get("incubation_investments", []) or []):
            VentureIntelligenceIncubationInvestment.objects.create(
                company_profile=vi_profile,
                date=inv.get("date"),
                status=inv.get("status"),
                incubator=inv.get("incubator")
            )
        for ex in (pe_data.get("pe_exits", []) or []):
            VentureIntelligencePEExit.objects.create(
                company_profile=vi_profile,
                deal_type=ex.get("deal_type"),
                date=ex.get("date"),
                exit_investors=ex.get("exit_investors") or [],
                amount=ex.get("amount"),
                exit_status=ex.get("exit_status"),
                valuation=ex.get("valuation"),
                revenue_multiple=ex.get("revenue_multiple"),
                is_vc=ex.get("is_vc"),
                is_hide_amount=ex.get("is_hide_amount")
            )
        for ipo in (pe_data.get("pe_ipos", []) or []):
            VentureIntelligencePEIPO.objects.create(
                company_profile=vi_profile,
                date=ipo.get("date"),
                ipo_investors=ipo.get("ipo_investors") or [],
                ipo_size=ipo.get("ipo_size"),
                is_investor_sale=ipo.get("is_investor_sale"),
                ipo_valuation=ipo.get("ipo_valuation"),
                is_amount_hide=ipo.get("is_amount_hide"),
                is_vc=ipo.get("is_vc")
            )

        # Merger & Acquisition
        for ma in (results.get("merger_acquisition", []) or []):
            VentureIntelligenceMergerAcquisition.objects.create(
                company_profile=vi_profile,
                company=ma.get("company"),
                date=ma.get("date"),
                amount=ma.get("amount"),
                acquirer=ma.get("acquirer"),
                company_valuation=ma.get("company_valuation"),
                company_valuation_post=ma.get("company_valuation_post"),
                revenue_multiple=ma.get("revenue_multiple"),
                revenue_multiple_post=ma.get("revenue_multiple_post"),
                is_hide_amount=ma.get("is_hide_amount"),
                is_asset_sale=ma.get("is_asset_sale"),
                is_minority_deal=ma.get("is_minority_deal")
            )

        # EPFO Employees
        for epfo in (cfs_profile_data.get("epfo_data", []) or []):
            VentureIntelligenceEpfoData.objects.create(
                company_profile=vi_profile,
                qrtr=epfo.get("qrtr"),
                employees=epfo.get("employees")
            )

        # Similar Companies
        for sim in (results.get("similar_cos", []) or []):
            VentureIntelligenceSimilarCompany.objects.create(
                company_profile=vi_profile,
                name=sim.get("name"),
                cin=normalize_cin(sim.get("cin")) or None,
                sector=sim.get("sector"),
                total_funding=sim.get("total_funding"),
                latest_investment=sim.get("latest_investment") or {},
                city=sim.get("city")
            )

        # 6. Create Relation to the main Deal
        if relation_type == VentureIntelligenceRelationType.COMPETITOR:
            existing_target = VentureIntelligenceCompanyRelation.objects.filter(
                deal=deal,
                relation_type=VentureIntelligenceRelationType.TARGET,
                company_profile__cin=vi_profile.cin,
            ).select_related("company_profile").first()
            if existing_target:
                raise ValueError(
                    f"{vi_profile.name} ({vi_profile.cin}) is already saved as this deal's target profile."
                )

        VentureIntelligenceCompanyRelation.objects.update_or_create(
            deal=deal,
            company_profile=vi_profile,
            defaults={"relation_type": relation_type}
        )

        # 7. Synchronize Board and Management Executives as Contacts
        all_execs = []
        all_execs.extend(profile_data.get("management_info", []) or [])
        all_execs.extend(profile_data.get("board_info", []) or [])
        
        created_contacts = []
        for exec_data in all_execs:
            exec_name = exec_data.get("name")
            if not exec_name:
                continue
            
            # Create or update contact
            contact, _ = Contact.objects.update_or_create(
                name=exec_name,
                defaults={
                    "designation": exec_data.get("designation"),
                    "location": vi_profile.city,
                }
            )
            created_contacts.append(contact)

        # Link contacts to the deal
        if created_contacts:
            # If deal has no primary contact, set the first executive as primary
            if not deal.primary_contact:
                deal.primary_contact = created_contacts[0]
                deal.save(update_fields=["primary_contact"])
                
            # Add all as additional contacts
            deal.additional_contacts.add(*created_contacts)

        # 8. Update main Deal fields if it's the target company
        if relation_type == 'target':
            deal.industry = vi_profile.industry or deal.industry
            deal.sector = vi_profile.sector or deal.sector
            deal.city = vi_profile.city or deal.city
            deal.company_details = vi_profile.registered_name or deal.company_details
            deal.save(update_fields=["industry", "sector", "city", "company_details"])

        # 9. Index for RAG
        if index_for_rag:
            try:
                self.index_profile_for_rag(vi_profile, deal=deal)
            except Exception as e:
                logger.error(f"Error indexing VI profile for RAG: {e}", exc_info=True)

        return vi_profile

    def index_profile_for_rag(self, vi_profile, deal=None):
        """
        Serializes all Venture Intelligence data for the company profile into a Markdown dossier,
        and saves it to the pgvector-based DocumentChunk store for RAG retrieval.
        """
        lines = []
        lines.append(f"# Company Dossier: {vi_profile.name}")
        lines.append(f"- **Official Name**: {vi_profile.registered_name or vi_profile.name}")
        if vi_profile.cin:
            lines.append(f"- **CIN (Corporate Identity Number)**: {vi_profile.cin}")
        if vi_profile.short_name:
            lines.append(f"- **Short Name**: {vi_profile.short_name}")
        if vi_profile.previous_name:
            lines.append(f"- **Previous Name**: {vi_profile.previous_name}")
        if vi_profile.website:
            lines.append(f"- **Website**: {vi_profile.website}")
        if vi_profile.year_founded:
            lines.append(f"- **Year Founded / Incorp**: {vi_profile.year_founded}")
        if vi_profile.industry or vi_profile.sector:
            lines.append(f"- **Industry/Sector**: {vi_profile.industry or 'N/A'} / {vi_profile.sector or 'N/A'}")
        
        location = []
        if vi_profile.city: location.append(vi_profile.city)
        if vi_profile.state: location.append(vi_profile.state)
        if vi_profile.region: location.append(vi_profile.region)
        if vi_profile.country: location.append(vi_profile.country)
        if location:
            lines.append(f"- **Location**: {', '.join(location)}")
        if vi_profile.pincode:
            lines.append(f"- **Pincode**: {vi_profile.pincode}")
        if vi_profile.address:
            addr = vi_profile.address
            if vi_profile.address_line2:
                addr += f", {vi_profile.address_line2}"
            lines.append(f"- **Address**: {addr}")
            
        if vi_profile.email:
            lines.append(f"- **Email**: {vi_profile.email}")
        if vi_profile.phone or vi_profile.telephone:
            lines.append(f"- **Phone**: {vi_profile.phone or vi_profile.telephone}")
        if vi_profile.linkedin:
            lines.append(f"- **LinkedIn**: {vi_profile.linkedin}")
        if vi_profile.listing_status:
            lines.append(f"- **Listing Status**: {vi_profile.listing_status}")
        if vi_profile.company_status:
            lines.append(f"- **Company Status**: {vi_profile.company_status}")
        if vi_profile.transacted_status:
            lines.append(f"- **Transacted Status**: {vi_profile.transacted_status}")
        if vi_profile.total_funding:
            lines.append(f"- **Total Funding**: {vi_profile.total_funding}")
        if vi_profile.tags:
            lines.append(f"- **Tags**: {vi_profile.tags}")
        if vi_profile.auditor_name:
            lines.append(f"- **Auditor**: {vi_profile.auditor_name}")
        if vi_profile.shp_promoter is not None or vi_profile.shp_non_promoter is not None:
            lines.append(f"- **Shareholding Pattern (Promoters / Non-Promoters)**: {vi_profile.shp_promoter or 0}% / {vi_profile.shp_non_promoter or 0}% (Year: {vi_profile.shp_year or 'N/A'})")
        if vi_profile.additional_info:
            lines.append(f"\n## Additional Info\n{vi_profile.additional_info}")
        if vi_profile.business_description:
            lines.append(f"\n## Business Description\n{vi_profile.business_description}")

        # Management & Board
        management = vi_profile.executives.filter(role_type='management')
        if management.exists():
            lines.append("\n## Key Management Executives")
            for exec_in in management:
                belongs = f" ({exec_in.belongs_to_firm_name})" if exec_in.belongs_to_firm_name else ""
                lines.append(f"- **{exec_in.name}**: {exec_in.designation or 'Executive'}{belongs}")
                
        board = vi_profile.executives.filter(role_type='board')
        if board.exists():
            lines.append("\n## Board of Directors")
            for exec_in in board:
                belongs = f" ({exec_in.belongs_to_firm_name})" if exec_in.belongs_to_firm_name else ""
                lines.append(f"- **{exec_in.name}**: {exec_in.designation or 'Director'}{belongs}")

        # Financial Statements
        financials = vi_profile.financial_statements.all()
        if financials.exists():
            lines.append("\n## Historical Financial Statements")
            for fs in financials:
                lines.append(f"\n### {fs.get_statement_type_display()} ({fs.fy} - {fs.fin_type})")
                for key, val in fs.data.items():
                    if key in ('fy', 'fin_type'):
                        continue
                    lines.append(f"- **{key}**: {val}")

        # Private Equity Investments
        pe_invs = vi_profile.pe_investments.all()
        if pe_invs.exists():
            lines.append("\n## Private Equity (PE) Investments")
            for inv in pe_invs:
                invs_list = ", ".join(inv.investors) if isinstance(inv.investors, list) else str(inv.investors)
                lines.append(f"- **Round {inv.round or 'N/A'}** ({inv.deal_date or 'N/A'}): Amount: {inv.amount or 'N/A'} (INR: {inv.amount_inr or 'N/A'}), Investors: {invs_list}, Post-Money Valuation: {inv.company_valuation_post_money or 'N/A'}, Revenue Multiple: {inv.revenue_multiple_post_money or 'N/A'}, Exit Status: {inv.exit_status or 'N/A'}")

        # Angel Investments
        angels = vi_profile.angel_investments.all()
        if angels.exists():
            lines.append("\n## Angel Investments")
            for inv in angels:
                invs_list = ", ".join(inv.investors) if isinstance(inv.investors, list) else str(inv.investors)
                exited = "Exited" if inv.is_exited else "Active"
                lines.append(f"- **Date**: {inv.date or 'N/A'}, Investors: {invs_list}, Status: {exited}")

        # Incubation Investments
        incubations = vi_profile.incubation_investments.all()
        if incubations.exists():
            lines.append("\n## Incubation Investments")
            for inv in incubations:
                lines.append(f"- **Date**: {inv.date or 'N/A'}, Incubator: {inv.incubator or 'N/A'}, Status: {inv.status or 'N/A'}")

        # PE Exits
        pe_exs = vi_profile.pe_exits.all()
        if pe_exs.exists():
            lines.append("\n## Private Equity (PE) Exits")
            for ex in pe_exs:
                invs_list = ", ".join(ex.exit_investors) if isinstance(ex.exit_investors, list) else str(ex.exit_investors)
                lines.append(f"- **Type**: {ex.deal_type or 'N/A'} ({ex.date or 'N/A'}), Amount: {ex.amount or 'N/A'}, Valuation: {ex.valuation or 'N/A'}, Revenue Multiple: {ex.revenue_multiple or 'N/A'}, Investors: {invs_list}")

        # PE IPOs
        ipos = vi_profile.pe_ipos.all()
        if ipos.exists():
            lines.append("\n## Private Equity (PE) IPOs")
            for ipo in ipos:
                invs_list = ", ".join(ipo.ipo_investors) if isinstance(ipo.ipo_investors, list) else str(ipo.ipo_investors)
                lines.append(f"- **IPO Date**: {ipo.date or 'N/A'}, IPO Size: {ipo.ipo_size or 'N/A'}, Valuation: {ipo.ipo_valuation or 'N/A'}, Investors: {invs_list}")

        # Merger & Acquisition
        mas = vi_profile.mergers_acquisitions.all()
        if mas.exists():
            lines.append("\n## Mergers & Acquisitions")
            for ma in mas:
                lines.append(f"- **Target/Acquired**: {ma.company or 'N/A'} ({ma.date or 'N/A'}), Acquirer: {ma.acquirer or 'N/A'}, Amount: {ma.amount or 'N/A'}, Valuation: {ma.company_valuation or 'N/A'}, Post-Deal Valuation: {ma.company_valuation_post or 'N/A'}, Revenue Multiple: {ma.revenue_multiple or 'N/A'}")

        # EPFO Employees
        epfo_records = vi_profile.epfo_data.all()
        if epfo_records.exists():
            lines.append("\n## EPFO Employee Counts (Quarterly)")
            for record in epfo_records:
                lines.append(f"- **{record.qrtr}**: {record.employees} employees")

        # Similar Companies
        similars = vi_profile.similar_companies.all()
        if similars.exists():
            lines.append("\n## Similar / Peer Companies")
            for sim in similars:
                latest = sim.latest_investment or {}
                latest_str = f"Latest Round: {latest.get('round', 'N/A')} on {latest.get('date', 'N/A')} (Amount: {latest.get('amount', 'N/A')})"
                lines.append(f"- **{sim.name}** (Sector: {sim.sector or 'N/A'}, City: {sim.city or 'N/A'}): Total Funding: {sim.total_funding or 'N/A'}, {latest_str}")

        dossier_text = "\n".join(lines)
        
        # Invoke embedding/chunking pipeline
        embedding_processor = EmbeddingService()
        embedding_processor.chunk_and_embed(
            text=dossier_text,
            deal=deal,
            source_type='extracted_source',
            source_id=f"vi_{vi_profile.id}",
            metadata={"company_name": vi_profile.name, "cin": vi_profile.cin},
            replace_existing=True
        )
        logger.info(f"Successfully indexed Venture Intelligence profile '{vi_profile.name}' for RAG (source_id: vi_{vi_profile.id})")
        return dossier_text
