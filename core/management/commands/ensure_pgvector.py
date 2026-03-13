from django.core.management.base import BaseCommand, CommandError
from django.db import connection


class Command(BaseCommand):
    help = "Enable the pgvector extension on PostgreSQL if the server supports it"

    def handle(self, *args, **options):
        if connection.vendor != "postgresql":
            raise CommandError("Current database is not PostgreSQL.")

        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT COUNT(*) FROM pg_available_extensions WHERE name = 'vector'"
            )
            available = cursor.fetchone()[0] > 0

            if not available:
                raise CommandError(
                    "The current PostgreSQL server does not have the vector extension installed. "
                    "On Railway's default Postgres template, you need a pgvector-capable Postgres service instead."
                )

            cursor.execute("CREATE EXTENSION IF NOT EXISTS vector;")
            cursor.execute(
                "SELECT extversion FROM pg_extension WHERE extname = 'vector'"
            )
            version = cursor.fetchone()[0]

        self.stdout.write(self.style.SUCCESS(f"pgvector enabled. Version: {version}"))
