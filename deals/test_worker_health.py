from unittest.mock import patch

from django.test import SimpleTestCase

from deals.services.worker_health import advertised_roles


class WorkerHealthTests(SimpleTestCase):
    @patch.dict("os.environ", {"RUN_VDR_COORDINATOR": "true"})
    def test_embedded_coordinator_advertises_both_roles(self):
        self.assertEqual(advertised_roles("worker"), ("worker", "coordinator"))

    @patch.dict("os.environ", {"RUN_VDR_COORDINATOR": "false"})
    def test_plain_worker_only_advertises_worker_role(self):
        self.assertEqual(advertised_roles("worker"), ("worker",))

    def test_dedicated_coordinator_advertises_coordinator_role(self):
        self.assertEqual(advertised_roles("coordinator"), ("coordinator",))
