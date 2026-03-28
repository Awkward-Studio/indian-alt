import json

from django.core.management.base import BaseCommand
from django.db.models import Count, Q

from ai_orchestrator.models import DocumentChunk
from ai_orchestrator.services.ai_processor import AIProcessorService
from ai_orchestrator.services.universal_chat import UniversalChatService
from deals.models import Deal, DealDocument
from microsoft.models import Email


class Command(BaseCommand):
    help = "Audit universal chat indexing coverage and retrieval diagnostics for deals."

    def add_arguments(self, parser):
        parser.add_argument(
            "--deal-id",
            type=str,
            help="Optional specific deal ID to audit.",
        )
        parser.add_argument(
            "--query",
            type=str,
            help="Optional test query to run through universal chat simulation.",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=20,
            help="Maximum number of deals to print when no --deal-id is provided.",
        )

    def handle(self, *args, **options):
        deal_id = options.get("deal_id")
        query = (options.get("query") or "").strip()
        limit = max(int(options.get("limit") or 20), 1)

        deals = Deal.objects.all().order_by("-created_at")
        if deal_id:
            deals = deals.filter(id=deal_id)
        else:
            deals = deals[:limit]

        deal_list = list(deals)
        if not deal_list:
            self.stdout.write(self.style.WARNING("No deals matched the audit filter."))
            return

        self.stdout.write(self.style.SUCCESS(f"Auditing {len(deal_list)} deal(s)..."))

        for deal in deal_list:
            chunk_counts = list(
                DocumentChunk.objects.filter(deal=deal)
                .values("source_type")
                .annotate(count=Count("id"))
                .order_by("-count", "source_type")
            )
            documents = DealDocument.objects.filter(deal=deal)
            emails = Email.objects.filter(deal=deal)
            payload = {
                "deal_id": str(deal.id),
                "title": deal.title,
                "is_indexed": deal.is_indexed,
                "document_count": documents.count(),
                "indexed_document_count": documents.filter(is_indexed=True).count(),
                "document_with_text_count": documents.exclude(Q(extracted_text__isnull=True) | Q(extracted_text="")).count(),
                "email_count": emails.count(),
                "indexed_email_count": emails.filter(is_indexed=True).count(),
                "email_with_text_count": emails.exclude(Q(extracted_text__isnull=True) | Q(extracted_text="")).count(),
                "chunk_count": DocumentChunk.objects.filter(deal=deal).count(),
                "chunk_count_by_source_type": chunk_counts,
                "distinct_chunk_sources": (
                    DocumentChunk.objects.filter(deal=deal)
                    .values("source_type", "source_id")
                    .distinct()
                    .count()
                ),
            }
            self.stdout.write(json.dumps(payload, default=str, indent=2))

        if query:
            self.stdout.write(self.style.SUCCESS(f"Running universal chat simulation for query: {query}"))
            chat_service = UniversalChatService(AIProcessorService())
            simulation = chat_service.simulate_query(query)
            self.stdout.write(json.dumps(simulation, default=str, indent=2))
