from django.core.management.base import BaseCommand, CommandError

from ai_orchestrator.services.embedding_processor import EmbeddingService
from deals.models import Deal, DealDocument


class Command(BaseCommand):
    help = (
        "Rebuild retrieval chunks from stored document artifacts without rerunning "
        "download, extraction, OCR, or artifact inference."
    )

    def add_arguments(self, parser):
        parser.add_argument("--deal", required=True, help="Deal UUID to reindex.")
        parser.add_argument(
            "--spreadsheets-only",
            action="store_true",
            help="Only rebuild documents whose extraction manifest is a spreadsheet.",
        )
        parser.add_argument("--dry-run", action="store_true", help="List documents without changing them.")

    def handle(self, *args, **options):
        try:
            deal = Deal.objects.get(id=options["deal"])
        except (Deal.DoesNotExist, ValueError) as exc:
            raise CommandError(f"Deal {options['deal']} was not found.") from exc

        documents = DealDocument.objects.filter(deal=deal).order_by("title", "id")
        if options["spreadsheets_only"]:
            documents = documents.filter(extraction_manifest__kind="spreadsheet")
        documents = list(documents)
        if not documents:
            self.stdout.write(self.style.WARNING("No matching documents were found."))
            return

        for document in documents:
            self.stdout.write(f"{'Would reindex' if options['dry_run'] else 'Reindexing'} {document.title}")
        if options["dry_run"]:
            self.stdout.write(f"Would reindex {len(documents)} document(s).")
            return

        service = EmbeddingService()
        completed = 0
        failures = []
        for document in documents:
            try:
                if service.vectorize_document(document):
                    completed += 1
                else:
                    failures.append(f"{document.title}: no retrieval chunks were created")
            except Exception as exc:
                failures.append(f"{document.title}: {exc}")

        if failures:
            for failure in failures:
                self.stderr.write(self.style.ERROR(failure))
            raise CommandError(
                f"Reindexed {completed}/{len(documents)} document(s); {len(failures)} failed."
            )
        self.stdout.write(self.style.SUCCESS(f"Reindexed {completed} document(s)."))
