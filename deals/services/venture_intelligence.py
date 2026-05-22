import os
import json
import logging
import requests
from django.conf import settings
from django.db import transaction
from deals.models import (
    Deal, 
    VentureIntelligenceCompanyProfile, 
    VentureIntelligenceFinancialStatement, 
    VentureIntelligenceCompanyRelation,
    VentureIntelligenceRelationType
)
from contacts.models import Contact
from ai_orchestrator.services.ai_processor import AIProcessorService

logger = logging.getLogger(__name__)

class VentureIntelligenceService:
    def __init__(self):
        self.api_key = getattr(settings, "VENTURE_INTELLIGENCE_API_KEY", os.environ.get("VENTURE_INTELLIGENCE_API_KEY", ""))
        self.base_url = getattr(settings, "VENTURE_INTELLIGENCE_BASE_URL", "https://api-hub.ventureintelligence.com")

    def fetch_company_details(self, company_name=None, cin=None, entity_name=None):
        if not self.api_key:
            raise ValueError("Venture Intelligence API key is not configured.")
        
        url = f"{self.base_url.rstrip('/')}/vendor/company-full/"
        headers = {
            "x-api-key": self.api_key,
            "Content-Type": "application/json"
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
        return response.json()

    def resolve_cin_via_ai(self, company_name: str) -> dict:
        """
        Uses Anthropic's Claude with native web search to resolve the official MCA CIN for a company.
        """
        ai_service = AIProcessorService()
        # Force using Anthropic to leverage native web search tool
        ai_service.model_provider = "anthropic"
        ai_service.current_provider = ai_service.anthropic_provider
        
        prompt = (
            f"Search the web to find the official 21-character Corporate Identity Number (CIN) "
            f"issued by the Ministry of Corporate Affairs (MCA) in India for the company: \"{company_name}\".\n"
            f"Return ONLY a JSON object in this format:\n"
            f"{{\n  \"cin\": \"U74999KA2012PTC066107\",\n  \"entity_name\": \"Flipkart Private Limited\",\n  \"confidence\": 0.95\n}}\n"
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
            # Clean JSON formatting wrappers if present
            if "```" in response_text:
                import re
                match = re.search(r"\{.*\}", response_text, re.DOTALL)
                if match:
                    response_text = match.group(0)
            
            return json.loads(response_text.strip())
        except Exception as e:
            logger.error(f"Failed to resolve CIN via AI web search: {e}", exc_info=True)
            return {}

    @transaction.atomic
    def enrich_deal(self, deal_id, company_name=None, cin=None, relation_type='target'):
        """
        Queries VI (resolving CIN via AI web search if necessary) and saves profile and financials in the DB.
        """
        deal = Deal.objects.get(id=deal_id)
        
        # 1. Resolve CIN if only company_name is provided
        resolved_cin = cin
        if not resolved_cin and company_name:
            # First try querying directly by name. If fails or not found, resolve via AI.
            try:
                vi_data = self.fetch_company_details(company_name=company_name)
                resolved_cin = vi_data.get("results", {}).get("profile", {}).get("cin")
            except Exception:
                logger.info(f"Direct query failed for '{company_name}', trying AI CIN resolution...")
                ai_resolution = self.resolve_cin_via_ai(company_name)
                resolved_cin = ai_resolution.get("cin")
                company_name = ai_resolution.get("entity_name") or company_name

        if not resolved_cin and not company_name:
            raise ValueError("Could not resolve CIN or company name for Enrichment.")

        # 2. Fetch full details using the resolved parameters
        vi_data = self.fetch_company_details(company_name=company_name, cin=resolved_cin)
        results = vi_data.get("results", {})
        profile_data = results.get("profile", {})
        
        # 3. Create or update VentureIntelligenceCompanyProfile
        cin_val = profile_data.get("cin") or resolved_cin
        vi_profile, _ = VentureIntelligenceCompanyProfile.objects.update_or_create(
            cin=cin_val,
            defaults={
                "name": profile_data.get("name") or company_name or deal.title,
                "registered_name": profile_data.get("registered_name"),
                "website": profile_data.get("website"),
                "industry": profile_data.get("industry"),
                "sector": profile_data.get("sector"),
                "email": profile_data.get("email"),
                "year_founded": profile_data.get("year_founded"),
                "city": (profile_data.get("city") or {}).get("name") if isinstance(profile_data.get("city"), dict) else (profile_data.get("city") if isinstance(profile_data.get("city"), str) else None),
                "total_funding": profile_data.get("total_funding"),
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

        # 5. Create Relation to the main Deal
        VentureIntelligenceCompanyRelation.objects.update_or_create(
            deal=deal,
            company_profile=vi_profile,
            defaults={"relation_type": relation_type}
        )

        # 6. Synchronize Board and Management Executives as Contacts
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

        # 7. Update main Deal fields if it's the target company
        if relation_type == 'target':
            deal.industry = vi_profile.industry or deal.industry
            deal.sector = vi_profile.sector or deal.sector
            deal.city = vi_profile.city or deal.city
            deal.company_details = vi_profile.registered_name or deal.company_details
            deal.save(update_fields=["industry", "sector", "city", "company_details"])

        return vi_profile
