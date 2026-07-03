import json

from django.core.management.base import BaseCommand
from django.db import transaction


class Command(BaseCommand):
    help = "Run live competitor discovery, select a listed/public competitor, and fetch Screener-backed financial data."

    def add_arguments(self, parser):
        parser.add_argument("--deal-id", type=str, help="Existing Deal UUID to use as the target context")
        parser.add_argument("--company", type=str, help="Company name for a temporary diagnostic deal")
        parser.add_argument("--sector", type=str, default="", help="Optional sector for a temporary diagnostic deal")
        parser.add_argument("--industry", type=str, default="", help="Optional industry for a temporary diagnostic deal")
        parser.add_argument("--city", type=str, default="", help="Optional city for a temporary diagnostic deal")
        parser.add_argument(
            "--instruction",
            type=str,
            default="Find competitors and include listed public companies where relevant.",
            help="Instruction passed to the live competitor discovery step",
        )
        parser.add_argument(
            "--candidate",
            type=str,
            help="Skip auto-selection and fetch Screener data for this public competitor name",
        )
        parser.add_argument("--ticker", type=str, default="", help="Ticker hint for --candidate")
        parser.add_argument("--exchange", type=str, default="", help="Exchange hint for --candidate, e.g. NSE or BSE")
        parser.add_argument(
            "--persist",
            action="store_true",
            help="Persist fetched profile/relation/chunks. By default DB writes are rolled back.",
        )
        parser.add_argument(
            "--skip-embedding",
            action="store_true",
            help="Fetch and store profile/financial rows but skip RAG embedding during the diagnostic.",
        )
        parser.add_argument(
            "--raw-response-chars",
            type=int,
            default=3000,
            help="Characters of raw competitor-search response to print when parsing fails",
        )

    def _print_competitors(self, competitors):
        if not competitors:
            self.stdout.write(self.style.WARNING("No parsed competitors returned."))
            return
        self.stdout.write(self.style.MIGRATE_HEADING("Parsed Competitors"))
        for index, item in enumerate(competitors, start=1):
            market = " / ".join(part for part in [item.get("exchange"), item.get("ticker")] if part)
            self.stdout.write(
                f"{index}. {item.get('name')} | type={item.get('company_type') or 'unknown'}"
                f" | market={market or '-'} | cin={item.get('cin') or '-'}"
            )
            if item.get("classification_source"):
                self.stdout.write(f"   source: {item.get('classification_source')}")

    def _select_public_candidate(self, competitors):
        for item in competitors:
            if (
                item.get("company_type") == "listed_public"
                or item.get("ticker")
                or item.get("exchange")
                or item.get("screener_url")
            ):
                return item
        return None

    def handle(self, *args, **options):
        from deals.models import Deal, VentureIntelligenceFinancialStatement
        from deals.services.screener import ScreenerCompanyService
        from deals.tasks import fetch_competitors_async_task

        deal_id = options.get("deal_id")
        company = options.get("company")
        if not deal_id and not company:
            raise SystemExit("Provide either --deal-id or --company.")

        with transaction.atomic():
            if deal_id:
                deal = Deal.objects.get(id=deal_id)
                created_temp_deal = False
            else:
                deal = Deal.objects.create(
                    title=company,
                    sector=options.get("sector") or "",
                    industry=options.get("industry") or "",
                    city=options.get("city") or "",
                    country="India",
                    deal_summary=f"Diagnostic competitor search target: {company}",
                )
                created_temp_deal = True

            self.stdout.write(self.style.MIGRATE_HEADING("=" * 80))
            self.stdout.write(self.style.MIGRATE_HEADING("Competitor -> Public Screener Pipeline Diagnostic"))
            self.stdout.write(self.style.MIGRATE_HEADING("=" * 80))
            self.stdout.write(f"Deal: {deal.title} ({deal.id})")
            self.stdout.write(f"Instruction: {options['instruction']}")
            self.stdout.write(f"Persist writes: {bool(options['persist'])}")
            self.stdout.write("")

            if options.get("candidate"):
                selected = {
                    "name": options["candidate"],
                    "company_type": "listed_public",
                    "ticker": options.get("ticker") or "",
                    "exchange": options.get("exchange") or "",
                    "notes": "Manual public competitor diagnostic candidate.",
                }
                competitors = [selected]
                self.stdout.write(self.style.WARNING("Skipping competitor discovery because --candidate was provided."))
            else:
                self.stdout.write(self.style.MIGRATE_HEADING("1. Live competitor discovery"))
                result = fetch_competitors_async_task(
                    str(deal.id),
                    instruction=options["instruction"],
                    existing_competitors=[],
                )
                if result.get("error"):
                    self.stdout.write(self.style.ERROR(result["error"]))
                    raw = result.get("response") or ""
                    if raw:
                        self.stdout.write("")
                        self.stdout.write(self.style.MIGRATE_HEADING("Raw Discovery Response"))
                        self.stdout.write(raw[: options["raw_response_chars"]])
                    transaction.set_rollback(True)
                    return

                competitors = result.get("competitors") or []
                self._print_competitors(competitors)
                selected = self._select_public_candidate(competitors)
                if not selected:
                    self.stdout.write("")
                    self.stdout.write(self.style.ERROR("No public/listed competitor was parsed from discovery results."))
                    raw = result.get("response") or ""
                    if raw:
                        self.stdout.write("")
                        self.stdout.write(self.style.MIGRATE_HEADING("Rendered/Raw Discovery Response"))
                        self.stdout.write(raw[: options["raw_response_chars"]])
                    transaction.set_rollback(True)
                    return

            self.stdout.write("")
            self.stdout.write(self.style.MIGRATE_HEADING("2. Selected public competitor"))
            self.stdout.write(json.dumps(selected, indent=2, default=str))

            self.stdout.write("")
            self.stdout.write(self.style.MIGRATE_HEADING("3. Fetching Screener-backed public company data"))
            screener_service = ScreenerCompanyService()
            profile = screener_service.save_public_competitor(deal, selected)
            self.stdout.write(self.style.SUCCESS("Screener profile stored."))
            self.stdout.write(f"  Profile ID: {profile.id}")
            self.stdout.write(f"  Name: {profile.name}")
            self.stdout.write(f"  Ticker / Exchange: {profile.ticker or '-'} / {profile.exchange or '-'}")
            self.stdout.write(f"  Screener URL: {profile.screener_url or '-'}")
            self.stdout.write(f"  Market Cap: {profile.market_cap or '-'}")

            statement_counts = {
                statement_type: VentureIntelligenceFinancialStatement.objects.filter(
                    company_profile=profile,
                    statement_type=statement_type,
                ).count()
                for statement_type in ["profit_loss", "balance_sheet", "cash_flow", "screener_quarterly"]
            }
            self.stdout.write("")
            self.stdout.write(self.style.MIGRATE_HEADING("Stored Financial Rows"))
            for statement_type, count in statement_counts.items():
                self.stdout.write(f"  {statement_type}: {count}")

            trading_comps = (profile.public_market_snapshot or {}).get("trading_comps") or {}
            if trading_comps:
                self.stdout.write("")
                self.stdout.write(self.style.MIGRATE_HEADING("Trading Comps Row"))
                self.stdout.write(json.dumps(trading_comps, indent=2, default=str)[:2500])

            latest_pl = VentureIntelligenceFinancialStatement.objects.filter(
                company_profile=profile,
                statement_type="profit_loss",
            ).order_by("-fy").first()
            if latest_pl:
                self.stdout.write("")
                self.stdout.write(self.style.MIGRATE_HEADING("Latest P&L Row"))
                self.stdout.write(json.dumps(latest_pl.data, indent=2, default=str)[:2500])

            if not options["skip_embedding"]:
                self.stdout.write("")
                self.stdout.write(self.style.MIGRATE_HEADING("4. Embedding public company dossier"))
                screener_service.index_profile_for_rag(profile, deal=deal)
                self.stdout.write(self.style.SUCCESS("Embedding completed."))
            else:
                self.stdout.write(self.style.WARNING("Embedding skipped by --skip-embedding."))

            if created_temp_deal:
                self.stdout.write("")
                self.stdout.write(self.style.WARNING("Temporary diagnostic deal was created for this run."))

            if not options["persist"]:
                transaction.set_rollback(True)
                self.stdout.write("")
                self.stdout.write(self.style.WARNING("Rolled back DB writes. Pass --persist to keep the profile/relation/chunks."))

            self.stdout.write(self.style.MIGRATE_HEADING("=" * 80))
