from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q

from ai_orchestrator.services.embedding_processor import EmbeddingService
from deals.models import Deal, DealDocument
from deals.services.document_artifacts import DocumentArtifactService


class Command(BaseCommand):
    SPREADSHEET_EXTENSIONS = (".xlsx", ".xlsm", ".xltx", ".xltm", ".xlsb", ".xls", ".xla", ".xlam", ".ods", ".csv", ".tsv")

    help = (
        "Rebuild retrieval chunks from stored document artifacts without rerunning "
        "download, extraction, OCR, or artifact inference."
    )

    def add_arguments(self, parser):
        scope = parser.add_mutually_exclusive_group(required=True)
        scope.add_argument("--deal", help="Deal UUID to reindex.")
        scope.add_argument(
            "--all-deals",
            action="store_true",
            help="Reindex matching documents across every deal.",
        )
        parser.add_argument(
            "--spreadsheets-only",
            action="store_true",
            help="Only rebuild documents whose extraction manifest is a spreadsheet.",
        )
        parser.add_argument("--dry-run", action="store_true", help="List documents without changing them.")

    def handle(self, *args, **options):
        documents = DealDocument.objects.all()
        if options["deal"]:
            try:
                deal = Deal.objects.get(id=options["deal"])
            except (Deal.DoesNotExist, ValueError) as exc:
                raise CommandError(f"Deal {options['deal']} was not found.") from exc
            documents = documents.filter(deal=deal)
        if options["spreadsheets_only"]:
            spreadsheet_filter = Q(extraction_manifest__kind="spreadsheet")
            for extension in self.SPREADSHEET_EXTENSIONS:
                spreadsheet_filter |= Q(title__iendswith=extension)
            documents = documents.filter(spreadsheet_filter)
        documents = list(documents.order_by("deal_id", "title", "id"))
        if not documents:
            self.stdout.write(self.style.WARNING("No matching documents were found."))
            return

        for document in documents:
            self.stdout.write(
                f"{'Would reindex' if options['dry_run'] else 'Reindexing'} "
                f"{document.title} (deal {document.deal_id})"
            )

        spreadsheet_documents = [
            document
            for document in documents
            if (document.extraction_manifest or {}).get("kind") == "spreadsheet"
            or document.title.lower().endswith(self.SPREADSHEET_EXTENSIONS)
        ]
        missing_manifests = [
            document for document in spreadsheet_documents
            if not any(
                sheet.get("cells")
                for sheet in (document.extraction_manifest or {}).get("sheets", [])
                if isinstance(sheet, dict)
            )
        ]
        reconstructed = {}
        for document in missing_manifests:
            text = document.extracted_text or document.normalized_text or ""
            manifest = DocumentArtifactService.spreadsheet_manifest_from_text(
                file_name=document.title,
                text=text,
            )
            if manifest:
                reconstructed[document.id] = manifest

        if missing_manifests:
            unavailable = len(missing_manifests) - len(reconstructed)
            self.stdout.write(
                self.style.WARNING(
                    f"{len(missing_manifests)} legacy spreadsheet(s) have no cell manifest: "
                    f"{len(reconstructed)} can be rebuilt from stored text; "
                    f"{unavailable} require source re-extraction."
                )
            )
        if options["dry_run"]:
            self.stdout.write(f"Would reindex {len(documents)} document(s).")
            return

        service = EmbeddingService()
        completed = 0
        failures = []
        for document in documents:
            try:
                rebuilt_manifest = reconstructed.get(document.id)
                if rebuilt_manifest:
                    document.extraction_manifest = rebuilt_manifest
                    document.save(update_fields=["extraction_manifest"])
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
