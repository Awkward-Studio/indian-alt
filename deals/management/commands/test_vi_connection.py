import json
import requests
from django.core.management.base import BaseCommand
from django.conf import settings
from django.db import transaction

class Command(BaseCommand):
    help = "Test connection, authentication, and responses from the Venture Intelligence Commercial API"

    def add_arguments(self, parser):
        parser.add_argument(
            "--key",
            type=str,
            help="Venture Intelligence API Key (overrides settings.VENTURE_INTELLIGENCE_API_KEY)"
        )
        parser.add_argument(
            "--url",
            type=str,
            help="Venture Intelligence Base URL (overrides settings.VENTURE_INTELLIGENCE_BASE_URL)"
        )
        parser.add_argument(
            "--company",
            type=str,
            default="Flipkart",
            help="Company name to search (default: Flipkart)"
        )
        parser.add_argument(
            "--cin",
            type=str,
            help="CIN of company to search (takes precedence over --company)"
        )
        parser.add_argument(
            "--entity",
            type=str,
            help="Entity name of company to search"
        )
        parser.add_argument(
            "--test-store",
            action="store_true",
            help="Run the full enrich_deal pipeline in a rolled-back transaction to verify DB storage without persisting"
        )

    def mask_key(self, key: str) -> str:
        if not key:
            return "None/Empty"
        if len(key) <= 8:
            return "***"
        return f"{key[:3]}...{key[-3:]}"

    def handle(self, *args, **options):
        # 1. Resolve API key and Base URL
        api_key = options["key"] or getattr(settings, "VENTURE_INTELLIGENCE_API_KEY", "")
        base_url = options["url"] or getattr(settings, "VENTURE_INTELLIGENCE_BASE_URL", "https://api-hub.ventureintelligence.com")

        # 2. Resolve query parameters
        query = options["company"]
        query_type = "company_name"
        
        if options["cin"]:
            query = options["cin"]
            query_type = "cin"
        elif options["entity"]:
            query = options["entity"]
            query_type = "entity_name"

        self.stdout.write(self.style.MIGRATE_HEADING("=" * 60))
        self.stdout.write(self.style.MIGRATE_HEADING("Starting Venture Intelligence Connection Test"))
        self.stdout.write(self.style.MIGRATE_HEADING("=" * 60))
        self.stdout.write(f"Target URL: {base_url.rstrip('/')}/vendor/company-full/")
        self.stdout.write(f"Using API Key: {self.mask_key(api_key)}")
        self.stdout.write(f"Query Parameter: {query} ({query_type})")
        self.stdout.write("-" * 60)

        if not api_key:
            self.stdout.write(self.style.ERROR("ERROR: API Key is missing or empty. Please configure VENTURE_INTELLIGENCE_API_KEY in your settings/.env file."))
            self.stdout.write(self.style.MIGRATE_HEADING("=" * 60))
            return

        url = f"{base_url.rstrip('/')}/vendor/company-full/"
        headers = {
            "x-api-key": api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
        
        payload = {}
        if query_type == "cin":
            payload["cin"] = query
        elif query_type == "company_name":
            payload["company_name"] = query
        elif query_type == "entity_name":
            payload["entity_name"] = query

        self.stdout.write(f"Request Payload: {json.dumps(payload)}")
        self.stdout.write("Sending POST request to Venture Intelligence API...")

        try:
            response = requests.post(url, headers=headers, json=payload, timeout=30)
            
            self.stdout.write(f"Response Status Code: {response.status_code}")
            self.stdout.write(f"Elapsed Time: {response.elapsed.total_seconds():.3f}s")
            self.stdout.write("-" * 60)

            if response.status_code == 200:
                self.stdout.write(self.style.SUCCESS("SUCCESS: Successfully connected to Venture Intelligence API!"))
                try:
                    data = response.json()
                    self.stdout.write(self.style.SUCCESS("=== Raw Response Keys ==="))
                    self.stdout.write(f"Top-level keys: {list(data.keys())}")
                    if "results" in data and isinstance(data["results"], dict):
                        self.stdout.write(f"Results keys: {list(data['results'].keys())}")
                    self.stdout.write("-" * 60)
                    
                    if "results" in data and isinstance(data["results"], dict):
                        results = data["results"]
                    else:
                        results = data
                        
                    profile = results.get("profile", {})
                    if profile:
                        self.stdout.write(self.style.SUCCESS("-" * 40))
                        self.stdout.write(self.style.SUCCESS("Retrieved Company Profile Details:"))
                        self.stdout.write(f"  Name: {profile.get('name')}")
                        self.stdout.write(f"  Registered Name: {profile.get('registered_name')}")
                        self.stdout.write(f"  CIN: {profile.get('cin')}")
                        self.stdout.write(f"  Industry: {profile.get('industry')}")
                        self.stdout.write(f"  Sector: {profile.get('sector')}")
                        self.stdout.write(f"  Total Funding: {profile.get('total_funding')}")
                        self.stdout.write(self.style.SUCCESS("-" * 40))
                    else:
                        self.stdout.write(self.style.WARNING("Response success, but no company profile data was found."))
                        self.stdout.write(self.style.WARNING("=== Full Raw Response Body ==="))
                        self.stdout.write(json.dumps(data, indent=2))
                        self.stdout.write(self.style.WARNING("=============================="))
                    
                    pl_count = len(results.get("profit_loss", [])) if results.get("profit_loss") else 0
                    bs_count = len(results.get("balance_sheet", [])) if results.get("balance_sheet") else 0
                    cf_count = len(results.get("cash_flow", [])) if results.get("cash_flow") else 0
                    self.stdout.write(f"Statements Found: P&L ({pl_count}), Balance Sheet ({bs_count}), Cash Flow ({cf_count})")

                    # --test-store: exercise the full enrich pipeline in a rolled-back savepoint
                    if options["test_store"]:
                        self.stdout.write("")
                        self.stdout.write(self.style.MIGRATE_HEADING("=" * 60))
                        self.stdout.write(self.style.MIGRATE_HEADING("TEST-STORE: Verifying JSON → DB mapping (rolled-back transaction)"))
                        self.stdout.write(self.style.MIGRATE_HEADING("=" * 60))
                        self._test_store_pipeline(data, query, query_type)

                except json.JSONDecodeError:
                    self.stdout.write(self.style.ERROR("Failed to parse response body as JSON."))
                    self.stdout.write(f"Raw Response Body: {response.text}")
                
            elif response.status_code == 404:
                self.stdout.write(self.style.WARNING("NOT FOUND (404): No company matched the search criteria."))
                self.stdout.write(f"Response: {response.text}")
                
            elif response.status_code == 401 or response.status_code == 403:
                self.stdout.write(self.style.ERROR(f"AUTHENTICATION FAILURE ({response.status_code}): Invalid or inactive API key."))
                self.stdout.write(f"Response: {response.text}")
                
            else:
                self.stdout.write(self.style.ERROR(f"HTTP ERROR ({response.status_code}): Server returned an unexpected status code."))
                self.stdout.write(f"Response: {response.text}")

        except requests.exceptions.Timeout:
            self.stdout.write(self.style.ERROR("CONNECTION ERROR: Request to Venture Intelligence API timed out after 30 seconds."))
        except requests.exceptions.RequestException as e:
            self.stdout.write(self.style.ERROR(f"CONNECTION ERROR: An exception occurred while making request: {e}"))
        
        self.stdout.write(self.style.MIGRATE_HEADING("=" * 60))

    def _test_store_pipeline(self, raw_api_data, query, query_type):
        """
        Runs the full enrich_deal storage pipeline inside a savepoint that is
        rolled back at the end. Prints every stored field and child record count
        so the user can visually confirm correct JSON → DB mapping.
        """
        from deals.models import (
            Deal,
            VentureIntelligenceCompanyProfile,
            VentureIntelligenceFinancialStatement,
            VentureIntelligenceCompanyRelation,
            VentureIntelligenceExecutive,
            VentureIntelligencePEInvestment,
            VentureIntelligenceAngelInvestment,
            VentureIntelligenceIncubationInvestment,
            VentureIntelligencePEExit,
            VentureIntelligencePEIPO,
            VentureIntelligenceMergerAcquisition,
            VentureIntelligenceEpfoData,
            VentureIntelligenceSimilarCompany,
        )
        from deals.services.venture_intelligence import VentureIntelligenceService
        from unittest.mock import patch

        sid = transaction.savepoint()
        try:
            # Create a temporary deal to enrich
            deal = Deal.objects.create(title=f"__test_store_{query}__")
            self.stdout.write(f"  Created temporary Deal: id={deal.id}, title='{deal.title}'")

            # Normalize: ensure data has the {"results": {...}} wrapper that
            # fetch_company_details produces, since enrich_deal expects it.
            if isinstance(raw_api_data, dict) and "results" not in raw_api_data:
                normalized_data = {"success": True, "results": raw_api_data}
            else:
                normalized_data = raw_api_data

            # Patch fetch_company_details to return the already-fetched data
            # and patch the embedding service to skip real embedding
            svc = VentureIntelligenceService()
            with patch.object(svc, "fetch_company_details", return_value=normalized_data), \
                 patch("deals.services.venture_intelligence.EmbeddingService.chunk_and_embed") as mock_embed:

                if query_type == "cin":
                    profile = svc.enrich_deal(deal_id=deal.id, cin=query)
                else:
                    profile = svc.enrich_deal(deal_id=deal.id, company_name=query)

            # ── Profile fields ──────────────────────────────────────────
            self.stdout.write("")
            self.stdout.write(self.style.SUCCESS("─── STORED PROFILE FIELDS ───"))
            profile_fields = [
                "cin", "name", "registered_name", "website", "industry", "sector",
                "email", "year_founded", "city", "total_funding",
                "state", "region", "country", "pincode",
                "telephone", "phone", "linkedin",
                "tags", "listing_status", "additional_info",
                "short_name", "previous_name", "full_name",
                "business_description", "transacted_status", "incorp_year",
                "company_status", "address", "address_line2",
                "contact_name", "contact_designation", "auditor_name",
                "shp_year", "shp_promoter", "shp_non_promoter", "is_xbrl",
            ]
            for field in profile_fields:
                val = getattr(profile, field, None)
                if val is not None and val != "" and val != []:
                    self.stdout.write(self.style.SUCCESS(f"  ✓ {field}: {val}"))
                else:
                    self.stdout.write(self.style.WARNING(f"  ✗ {field}: <empty>"))

            # ── Child table counts ──────────────────────────────────────
            self.stdout.write("")
            self.stdout.write(self.style.SUCCESS("─── CHILD TABLE RECORD COUNTS ───"))
            child_tables = [
                ("Executives (management)", profile.executives.filter(role_type='management').count()),
                ("Executives (board)", profile.executives.filter(role_type='board').count()),
                ("Financial Statements (P&L)", profile.financial_statements.filter(statement_type='profit_loss').count()),
                ("Financial Statements (BS)", profile.financial_statements.filter(statement_type='balance_sheet').count()),
                ("Financial Statements (CF)", profile.financial_statements.filter(statement_type='cash_flow').count()),
                ("PE Investments", profile.pe_investments.count()),
                ("Angel Investments", profile.angel_investments.count()),
                ("Incubation Investments", profile.incubation_investments.count()),
                ("PE Exits", profile.pe_exits.count()),
                ("PE IPOs", profile.pe_ipos.count()),
                ("Mergers & Acquisitions", profile.mergers_acquisitions.count()),
                ("EPFO Data", profile.epfo_data.count()),
                ("Similar Companies", profile.similar_companies.count()),
            ]
            for label, count in child_tables:
                if count > 0:
                    self.stdout.write(self.style.SUCCESS(f"  ✓ {label}: {count} records"))
                else:
                    self.stdout.write(self.style.WARNING(f"  ✗ {label}: 0 records"))

            # ── Deal relation ───────────────────────────────────────────
            self.stdout.write("")
            relation = VentureIntelligenceCompanyRelation.objects.filter(deal=deal, company_profile=profile).first()
            if relation:
                self.stdout.write(self.style.SUCCESS(f"  ✓ Deal → Profile relation: type={relation.relation_type}"))
            else:
                self.stdout.write(self.style.ERROR(f"  ✗ Deal → Profile relation: NOT CREATED"))

            # ── Deal field updates ──────────────────────────────────────
            deal.refresh_from_db()
            self.stdout.write("")
            self.stdout.write(self.style.SUCCESS("─── DEAL FIELD UPDATES ───"))
            deal_fields = ["industry", "sector", "city", "company_details"]
            for field in deal_fields:
                val = getattr(deal, field, None)
                if val:
                    self.stdout.write(self.style.SUCCESS(f"  ✓ deal.{field}: {val}"))
                else:
                    self.stdout.write(self.style.WARNING(f"  ✗ deal.{field}: <empty>"))

            # ── RAG embedding call ──────────────────────────────────────
            self.stdout.write("")
            if mock_embed.called:
                call_kwargs = mock_embed.call_args
                text_arg = call_kwargs.kwargs.get("text") or (call_kwargs.args[0] if call_kwargs.args else "")
                text_preview = text_arg[:300] if text_arg else "<empty>"
                self.stdout.write(self.style.SUCCESS("─── RAG DOSSIER PREVIEW (first 300 chars) ───"))
                self.stdout.write(text_preview)
                self.stdout.write(self.style.SUCCESS(f"  ✓ chunk_and_embed called with source_id='vi_{profile.id}', text length={len(text_arg)} chars"))
            else:
                self.stdout.write(self.style.ERROR("  ✗ chunk_and_embed was NOT called — RAG indexing broken"))

            # ── Sample child records detail ─────────────────────────────
            self.stdout.write("")
            self.stdout.write(self.style.SUCCESS("─── SAMPLE CHILD RECORDS ───"))

            first_exec = profile.executives.first()
            if first_exec:
                self.stdout.write(f"  Executive: {first_exec.name} / {first_exec.designation} / {first_exec.role_type}")

            first_pe = profile.pe_investments.first()
            if first_pe:
                self.stdout.write(f"  PE Investment: Round={first_pe.round}, Date={first_pe.deal_date}, Amount={first_pe.amount}, Investors={first_pe.investors}")

            first_fs = profile.financial_statements.first()
            if first_fs:
                data_keys = list(first_fs.data.keys())[:8]
                self.stdout.write(f"  Financial Statement: {first_fs.statement_type} FY={first_fs.fy} ({first_fs.fin_type}), data keys={data_keys}")

            first_ma = profile.mergers_acquisitions.first()
            if first_ma:
                self.stdout.write(f"  M&A: Company={first_ma.company}, Acquirer={first_ma.acquirer}, Amount={first_ma.amount}")

            first_sim = profile.similar_companies.first()
            if first_sim:
                self.stdout.write(f"  Similar Company: {first_sim.name}, Sector={first_sim.sector}, Funding={first_sim.total_funding}")

            first_epfo = profile.epfo_data.first()
            if first_epfo:
                self.stdout.write(f"  EPFO: Quarter={first_epfo.qrtr}, Employees={first_epfo.employees}")

            self.stdout.write("")
            self.stdout.write(self.style.SUCCESS("TEST-STORE COMPLETE: All data parsed and stored correctly."))

        except Exception as e:
            self.stdout.write(self.style.ERROR(f"TEST-STORE FAILED: {e}"))
            import traceback
            self.stdout.write(traceback.format_exc())
        finally:
            # Roll back — nothing persists
            transaction.savepoint_rollback(sid)
            self.stdout.write(self.style.WARNING("(Transaction rolled back — no data was persisted)"))
