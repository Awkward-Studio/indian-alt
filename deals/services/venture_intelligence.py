import os
import json
import logging
import requests
from django.conf import settings
from django.db import transaction
from django.utils import timezone
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
                sector=sim.get("sector"),
                total_funding=sim.get("total_funding"),
                latest_investment=sim.get("latest_investment") or {},
                city=sim.get("city")
            )

        # 6. Create Relation to the main Deal
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
