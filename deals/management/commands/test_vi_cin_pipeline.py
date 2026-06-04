from django.core.management.base import BaseCommand
from django.db import OperationalError
from django.db import transaction


class Command(BaseCommand):
    help = "Test company name -> web-search CIN resolution -> Venture Intelligence lookup -> optional rolled-back DB mapping"

    def add_arguments(self, parser):
        parser.add_argument("--company", type=str, required=True, help="Company name to resolve and fetch")
        parser.add_argument("--cin", type=str, help="Known CIN to bypass web-search resolution")
        parser.add_argument(
            "--test-store",
            action="store_true",
            help="Create a temporary deal and run enrich_deal in a transaction that is rolled back",
        )

    def handle(self, *args, **options):
        from deals.models import (
            Deal,
            VentureIntelligenceCompanyRelation,
            VentureIntelligenceFinancialStatement,
        )
        from deals.services.venture_intelligence import VentureIntelligenceService

        company = options["company"]
        supplied_cin = options.get("cin")
        service = VentureIntelligenceService()

        self.stdout.write(self.style.MIGRATE_HEADING("=" * 72))
        self.stdout.write(self.style.MIGRATE_HEADING("VI CIN Pipeline Test"))
        self.stdout.write(self.style.MIGRATE_HEADING("=" * 72))
        self.stdout.write(f"Company: {company}")
        self.stdout.write(f"Supplied CIN: {supplied_cin or '<none>'}")

        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING("1. Resolving CIN"))
        resolution = service.resolve_company_identity(company_name=company, cin=supplied_cin)
        self.stdout.write(f"  Source: {resolution.get('source')}")
        self.stdout.write(f"  CIN: {resolution.get('cin') or '<unresolved>'}")
        self.stdout.write(f"  Entity: {resolution.get('entity_name') or '<unknown>'}")
        self.stdout.write(f"  Confidence: {resolution.get('confidence')}")
        self.stdout.write(f"  Valid: {resolution.get('is_valid')}")
        cin_candidates = resolution.get("cin_candidates") or []
        if cin_candidates:
            self.stdout.write("  Candidate CINs:")
            for index, candidate in enumerate(cin_candidates, start=1):
                self.stdout.write(
                    f"    {index}. {candidate.get('cin')} | "
                    f"{candidate.get('entity_name') or '<unknown>'} | "
                    f"confidence={candidate.get('confidence')}"
                )

        if not resolution.get("is_valid") or not resolution.get("cin"):
            self.stdout.write(self.style.ERROR("Resolution failed; stopping before VI lookup."))
            return

        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING("2. Fetching VI data by resolved CIN candidates"))
        vi_data, resolution = service.fetch_company_details_from_resolution(resolution, company_name=company)

        results = vi_data.get("results", {}) if isinstance(vi_data, dict) else {}
        profile = results.get("profile", {}) or {}
        self.stdout.write(self.style.SUCCESS("  VI lookup succeeded."))
        self.stdout.write(f"  Name: {profile.get('name')}")
        self.stdout.write(f"  Registered Name: {profile.get('registered_name')}")
        self.stdout.write(f"  CIN: {profile.get('cin')}")
        self.stdout.write(f"  Industry: {profile.get('industry')}")
        self.stdout.write(f"  Sector: {profile.get('sector')}")
        self.stdout.write(f"  P&L rows: {len(results.get('profit_loss') or [])}")
        self.stdout.write(f"  Balance Sheet rows: {len(results.get('balance_sheet') or [])}")
        self.stdout.write(f"  Cash Flow rows: {len(results.get('cash_flow') or [])}")

        if not options["test_store"]:
            self.stdout.write("")
            self.stdout.write("Skipping DB mapping. Pass --test-store to verify storage in a rolled-back transaction.")
            self.stdout.write(self.style.MIGRATE_HEADING("=" * 72))
            return

        try:
            from django.db import connection
            connection.ensure_connection()
        except OperationalError as exc:
            self.stdout.write("")
            self.stdout.write(self.style.ERROR("DB mapping skipped: Django could not connect to Postgres."))
            self.stdout.write(self.style.ERROR(str(exc)))
            self.stdout.write("")
            self.stdout.write("Start the local stack first, then rerun with --test-store:")
            self.stdout.write(r'  cd "D:\Freelance Projects\India-alt"')
            self.stdout.write(r"  .\start-local.ps1 -SkipDocproc")
            self.stdout.write(self.style.MIGRATE_HEADING("=" * 72))
            return

        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING("3. Testing DB mapping (rolled back)"))
        with transaction.atomic():
            deal = Deal.objects.create(title=f"__vi_pipeline_test__ {company}")
            stored_profile = service.enrich_deal(
                deal_id=deal.id,
                company_name=company,
                cin=resolution["cin"],
                relation_type="target",
            )
            relation_exists = VentureIntelligenceCompanyRelation.objects.filter(
                deal=deal,
                company_profile=stored_profile,
            ).exists()
            statement_count = VentureIntelligenceFinancialStatement.objects.filter(company_profile=stored_profile).count()

            self.stdout.write(self.style.SUCCESS("  DB mapping succeeded."))
            self.stdout.write(f"  Stored profile id: {stored_profile.id}")
            self.stdout.write(f"  Stored profile CIN: {stored_profile.cin}")
            self.stdout.write(f"  Deal relation created: {relation_exists}")
            self.stdout.write(f"  Financial statements stored: {statement_count}")
            transaction.set_rollback(True)
            self.stdout.write(self.style.WARNING("  Rolled back test transaction; no test data persisted."))

        self.stdout.write(self.style.MIGRATE_HEADING("=" * 72))
