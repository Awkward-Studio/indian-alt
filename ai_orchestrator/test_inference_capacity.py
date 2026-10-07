from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.core.cache import cache
from django.test import SimpleTestCase, override_settings

from ai_orchestrator.services.inference_queue import InferenceQueueLease


@override_settings(CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}})
class InferenceCapacityTests(SimpleTestCase):
    def setUp(self):
        cache.clear()

    def lease(self, number):
        audit = SimpleNamespace(id=f"audit-{number}", celery_task_id=f"task-{number}",
            source_type="vdr_report_section", source_id="parent", context_label="section", source_metadata={}, save=Mock())
        lease = InferenceQueueLease(audit, max_wait_seconds=.01)
        lease.check_cancelled = Mock()
        lease._start_heartbeat = Mock()
        return lease

    @override_settings(AI_INFERENCE_MAX_CONCURRENT_REQUESTS=4)
    def test_four_owned_leases_and_selective_cancellation(self):
        leases = [self.lease(n).__enter__() for n in range(4)]
        self.assertEqual(len({lease.lease_key for lease in leases}), 4)
        self.assertTrue(InferenceQueueLease.release_for_audits(["audit-2"]))
        self.assertIsNone(cache.get(leases[2].lease_key))
        self.assertTrue(all(cache.get(lease.lease_key) for lease in [leases[0], leases[1], leases[3]]))
        self.assertTrue(InferenceQueueLease.force_release())
        self.assertTrue(all(cache.get(key) is None for key in InferenceQueueLease.lease_keys()))

    @override_settings(AI_INFERENCE_MAX_CONCURRENT_REQUESTS=1)
    def test_t4_remains_single_slot(self):
        first = self.lease(0).__enter__()
        with patch("ai_orchestrator.services.inference_queue.time.sleep"), self.assertRaises(TimeoutError):
            self.lease(1).__enter__()
        first.__exit__(None, None, None)

    @override_settings(AI_INFERENCE_MAX_CONCURRENT_REQUESTS=4)
    def test_fifth_request_waits_and_release_cannot_erase_a_successor(self):
        leases = [self.lease(n).__enter__() for n in range(4)]
        with patch("ai_orchestrator.services.inference_queue.time.sleep"), self.assertRaises(TimeoutError):
            self.lease(4).__enter__()
        key = leases[0].lease_key
        replacement = {"lease_token": "new-owner", "audit_log_id": "successor"}
        cache.set(key, replacement)
        self.assertFalse(leases[0]._mutate_owned_lease())
        self.assertEqual(cache.get(key), replacement)
