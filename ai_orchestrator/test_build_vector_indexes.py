from unittest.mock import MagicMock, patch

from django.core.management.base import CommandError
from django.test import SimpleTestCase

from ai_orchestrator.management.commands.build_vector_indexes import Command


class BuildVectorIndexesTests(SimpleTestCase):
    def setUp(self):
        self.connection = MagicMock()
        self.connection.get_autocommit.return_value = True
        self.cursor = self.connection.cursor.return_value.__enter__.return_value

    def test_rejects_invalid_existing_index(self):
        self.cursor.fetchone.return_value = (False,)
        with patch(
            "ai_orchestrator.management.commands.build_vector_indexes.connections",
            {"default": self.connection},
        ):
            with self.assertRaisesMessage(CommandError, "exists but is invalid"):
                Command().handle(
                    index="documentchunk", m=16, ef_construction=64,
                    maintenance_work_mem="4GB", max_parallel_maintenance_workers=0,
                )
        self.assertFalse(any(
            "CREATE INDEX" in str(call)
            for call in self.cursor.execute.call_args_list
        ))

    def test_sets_serial_build_before_creating_index(self):
        self.cursor.fetchone.return_value = None
        with patch(
            "ai_orchestrator.management.commands.build_vector_indexes.connections",
            {"default": self.connection},
        ):
            Command().handle(
                index="documentchunk", m=16, ef_construction=64,
                maintenance_work_mem="4GB", max_parallel_maintenance_workers=0,
            )
        sql_calls = [str(call.args[0]) for call in self.cursor.execute.call_args_list]
        self.assertTrue(any("max_parallel_maintenance_workers" in sql for sql in sql_calls))
        self.assertTrue(any("CREATE INDEX CONCURRENTLY" in sql for sql in sql_calls))
