from django.core.management.base import BaseCommand, CommandError
from django.db import connections


INDEXES = {
    "dealprofile": (
        "dealprofile_embedding_hnsw",
        'ai_orchestrator_dealretrievalprofile',
    ),
    "documentchunk": (
        "docchunk_embedding_hnsw",
        'ai_orchestrator_documentchunk',
    ),
}


class Command(BaseCommand):
    help = "Build deferred pgvector HNSW indexes outside the deploy migration."

    def add_arguments(self, parser):
        parser.add_argument(
            "--index",
            choices=["all", *INDEXES],
            default="all",
            help="Index to build. Defaults to all deferred vector indexes.",
        )
        parser.add_argument("--m", type=int, default=16)
        parser.add_argument("--ef-construction", type=int, default=64)
        parser.add_argument("--maintenance-work-mem", default="32MB")

    def handle(self, *args, **options):
        if options["m"] < 2 or options["ef_construction"] < 4:
            raise CommandError("HNSW m must be at least 2 and ef-construction at least 4.")

        connection = connections["default"]
        names = INDEXES if options["index"] == "all" else {options["index"]: INDEXES[options["index"]]}
        previous_autocommit = connection.get_autocommit()
        connection.set_autocommit(True)
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT set_config('maintenance_work_mem', %s, false)",
                    [options["maintenance_work_mem"]],
                )
                for label, (index_name, table_name) in names.items():
                    self.stdout.write(f"Building {label} index {index_name} on {table_name}...")
                    cursor.execute(
                        f'CREATE INDEX CONCURRENTLY IF NOT EXISTS "{index_name}" '
                        f'ON "{table_name}" USING hnsw ("embedding" vector_cosine_ops) '
                        f'WITH (m = {int(options["m"])}, ef_construction = {int(options["ef_construction"])})'
                    )
                    self.stdout.write(self.style.SUCCESS(f"{index_name} is ready."))
        finally:
            connection.set_autocommit(previous_autocommit)
