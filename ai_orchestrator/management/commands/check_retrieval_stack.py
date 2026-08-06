from django.core.management.base import BaseCommand, CommandError
from django.db import connection

from ai_orchestrator.models import DealRetrievalProfile, DocumentChunk
from ai_orchestrator.services.runtime import AIRuntimeService


class Command(BaseCommand):
    help = "Validate that retrieval is running on PostgreSQL with pgvector and expected embedding metadata."

    def handle(self, *args, **options):
        if connection.vendor != "postgresql":
            raise CommandError("PostgreSQL is required for retrieval; current vendor is %s." % connection.vendor)

        with connection.cursor() as cursor:
            cursor.execute("SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'vector')")
            if not cursor.fetchone()[0]:
                raise CommandError("The pgvector extension is not installed in this database.")

            cursor.execute(
                """
                SELECT indexname
                FROM pg_indexes
                WHERE schemaname = current_schema()
                  AND indexname IN (
                    'docchunk_embedding_hnsw',
                    'dealprofile_embedding_hnsw',
                    'docchunk_search_vector_gin'
                  )
                """
            )
            found_indexes = {row[0] for row in cursor.fetchall()}

        expected_indexes = {
            "docchunk_embedding_hnsw",
            "dealprofile_embedding_hnsw",
            "docchunk_search_vector_gin",
        }
        missing_indexes = sorted(expected_indexes - found_indexes)
        if missing_indexes:
            raise CommandError("Missing retrieval indexes: %s" % ", ".join(missing_indexes))

        expected_model = AIRuntimeService.get_embedding_model()
        chunk_dimensions = (
            DocumentChunk.objects.exclude(embedding_dimensions__isnull=True)
            .values_list("embedding_dimensions", flat=True)
            .distinct()
        )
        profile_dimensions = (
            DealRetrievalProfile.objects.exclude(embedding_dimensions__isnull=True)
            .values_list("embedding_dimensions", flat=True)
            .distinct()
        )
        dimensions = sorted(set(chunk_dimensions) | set(profile_dimensions))
        bad_dimensions = [value for value in dimensions if value != 1024]
        if bad_dimensions:
            raise CommandError("Unexpected embedding dimensions found: %s" % ", ".join(map(str, bad_dimensions)))

        stale_models = (
            DocumentChunk.objects.exclude(embedding_model="")
            .exclude(embedding_model=expected_model)
            .values_list("embedding_model", flat=True)
            .distinct()
        )
        stale_models = sorted(set(stale_models))
        if stale_models:
            self.stdout.write(self.style.WARNING("Chunks exist for older embedding models: %s" % ", ".join(stale_models)))

        self.stdout.write(self.style.SUCCESS("Retrieval stack OK: PostgreSQL, pgvector, HNSW, GIN, and dimensions validated."))
