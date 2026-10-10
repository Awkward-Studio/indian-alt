from django.core.management.base import BaseCommand

from deals.models import Deal
from work_items.services import merged_task_candidates, analysis_report, latest_task_analysis, report_gap_candidates, sync_deal_suggestions


class Command(BaseCommand):
    help = "Backfill analysis-derived task suggestions. Dry-run unless --apply is provided."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true")
        parser.add_argument("--deal-id")
        parser.add_argument('--source-gaps-only', action='store_true', help='Only synchronize deals with recorded report source gaps.')
        parser.add_argument("--limit", type=int, default=0)
        parser.add_argument(
            "--missing-only", action="store_true",
            help="Skip deals that already have at least one persisted suggestion.",
        )
        parser.add_argument("--progress-every", type=int, default=100)

    def handle(self, *args, **options):
        queryset = Deal.objects.filter(analyses__isnull=False).distinct().order_by("title")
        if options["deal_id"]:
            queryset = queryset.filter(id=options["deal_id"])
        if options["missing_only"]:
            queryset = queryset.filter(task_suggestions__isnull=True)
        if options["limit"]:
            queryset = queryset[: max(options["limit"], 0)]
        deals = candidates = created = updated = 0
        for deal in queryset.iterator():
            if options['source_gaps_only'] and not report_gap_candidates(deal):
                continue
            deals += 1
            analysis = latest_task_analysis(deal)
            if options["apply"]:
                result = sync_deal_suggestions(deal, analysis)
                candidates += result["candidates"]
                created += result["created"]
                updated += result["updated"]
            else:
                candidates += len(merged_task_candidates(analysis_report(analysis, deal), deal))
            if options["progress_every"] and deals % options["progress_every"] == 0:
                self.stdout.write(
                    f"progress deals={deals} candidates={candidates} created={created} updated={updated}"
                )
        self.stdout.write(
            f"mode={'apply' if options['apply'] else 'dry-run'} deals={deals} candidates={candidates} "
            f"created={created} updated={updated}"
        )
