from django.core.management.base import BaseCommand, CommandError
from django.db import connection
from django.utils import timezone

from ai_orchestrator.models import DocumentChunk
from ai_orchestrator.services.embedding_processor import EmbeddingService


class Command(BaseCommand):
    help = "Rebuild chunk search_text, search_vector, and embeddings using contextual prepending."

    def add_arguments(self, parser):
        parser.add_argument("--deal-id", default="", help="Only rebuild chunks for this deal id.")
        parser.add_argument("--source-type", default="", help="Only rebuild chunks for this source type.")
        parser.add_argument("--limit", type=int, default=0, help="Maximum chunks to rebuild.")
        parser.add_argument("--batch-size", type=int, default=50, help="Number of chunks to process per batch.")
        parser.add_argument("--dry-run", action="store_true", help="Report how many chunks would be rebuilt.")

    def handle(self, *args, **options):
        if connection.vendor != "postgresql":
            raise CommandError("PostgreSQL is required for contextual embedding rebuilds.")

        queryset = DocumentChunk.objects.select_related("deal").order_by("created_at", "id")
        if options["deal_id"]:
            queryset = queryset.filter(deal_id=options["deal_id"])
        if options["source_type"]:
            queryset = queryset.filter(source_type=options["source_type"])
        if options["limit"]:
            queryset = queryset[: options["limit"]]

        total = queryset.count() if not options["limit"] else len(list(queryset.values_list("id", flat=True)))
        if options["dry_run"]:
            self.stdout.write("Would rebuild %s chunk(s)." % total)
            return

        service = EmbeddingService()
        batch_size = max(1, int(options["batch_size"] or 50))
        rebuilt = 0
        failed = 0
        pending = list(queryset)

        for start in range(0, len(pending), batch_size):
            batch = pending[start:start + batch_size]
            refreshed = []
            for chunk in batch:
                search_text = service._contextual_chunk_text(
                    content=chunk.content,
                    deal=chunk.deal,
                    source_type=chunk.source_type,
                    source_id=chunk.source_id,
                    metadata=chunk.metadata or {},
                )
                embedding = service._get_embedding(search_text)
                if not embedding:
                    failed += 1
                    continue
                chunk.search_text = search_text
                chunk.embedding = embedding
                chunk.embedding_model = service.model_name
                chunk.embedding_dimensions = service._embedding_dimensions(embedding)
                chunk.indexed_at = timezone.now()
                refreshed.append(chunk)

            if refreshed:
                DocumentChunk.objects.bulk_update(
                    refreshed,
                    ["search_text", "embedding", "embedding_model", "embedding_dimensions", "indexed_at"],
                )
                service._refresh_search_vectors(refreshed)
                rebuilt += len(refreshed)
                self.stdout.write("Rebuilt %s/%s chunk(s)." % (rebuilt, len(pending)))

        if failed:
            self.stdout.write(self.style.WARNING("Skipped %s chunk(s) because embedding generation failed." % failed))
        self.stdout.write(self.style.SUCCESS("Contextual embedding rebuild complete: %s rebuilt." % rebuilt))
