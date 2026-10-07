from unittest.mock import Mock, patch
from django.core.cache import cache
from django.test import TestCase, override_settings
from ai_orchestrator.models import AIAuditLog
from ai_orchestrator.services.inference_queue import InferenceQueueLease


@override_settings(CACHES={'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}},
                   AI_INFERENCE_MAX_CONCURRENT_REQUESTS=4, AI_SLOT_TRANSPORT_ENABLED=False)
class TerminalInferenceSlotTests(TestCase):
    def setUp(self):
        cache.clear()

    def occupy(self, key, audit):
        cache.set(key, {'audit_log_id': str(audit.id), 'lease_token': str(audit.id)})

    def test_three_cancelled_slots_cannot_leave_h100_with_only_one_request(self):
        keys = InferenceQueueLease.lease_keys()
        cancelled = [AIAuditLog.objects.create(status='FAILED') for _ in range(3)]
        live = AIAuditLog.objects.create(status='PROCESSING')
        for key, audit in zip(keys, [*cancelled, live]):
            self.occupy(key, audit)
        live_owner = cache.get(keys[3])
        leases = []
        for _ in range(3):
            audit = AIAuditLog.objects.create(status='PROCESSING', source_type='report_section_quality_review')
            lease = InferenceQueueLease(audit, max_wait_seconds=.01)
            lease._start_heartbeat = Mock()
            leases.append(lease.__enter__())
        self.assertEqual({lease.lease_key for lease in leases}, set(keys[:3]))
        self.assertEqual(cache.get(keys[3]), live_owner)
        for lease in leases:
            lease.__exit__(None, None, None)

    def test_cancelled_parent_releases_child_slot_even_if_child_audit_is_processing(self):
        parent = AIAuditLog.objects.create(status='FAILED', source_metadata={'cancel_requested': True})
        child = AIAuditLog.objects.create(status='PROCESSING', source_metadata={'vdr_parent_audit_id': str(parent.id)})
        current = AIAuditLog.objects.create(status='PROCESSING')
        key = InferenceQueueLease.lease_keys()[2]
        self.occupy(key, child)
        self.assertTrue(InferenceQueueLease(current)._release_terminal_owner(key))
        self.assertIsNone(cache.get(key))

    def test_api_cancellation_releases_only_its_own_inference_slots(self):
        from ai_orchestrator.views import AIAuditLogViewSet
        parent = AIAuditLog.objects.create(status='PROCESSING', source_type='deal_full_synthesis')
        child = AIAuditLog.objects.create(status='PROCESSING', source_id=str(parent.id),
            source_metadata={'vdr_parent_audit_id': str(parent.id)})
        other = AIAuditLog.objects.create(status='PROCESSING')
        keys = InferenceQueueLease.lease_keys()
        self.occupy(keys[0], child)
        self.occupy(keys[1], other)
        with patch.object(AIAuditLogViewSet, '_revoke', return_value=[]), patch(
                'ai_orchestrator.views.broadcast_audit_log_update'), self.captureOnCommitCallbacks(execute=True):
            AIAuditLogViewSet()._cancel_log(parent)
        self.assertIsNone(cache.get(keys[0]))
        self.assertEqual(cache.get(keys[1])['audit_log_id'], str(other.id))

    def test_deployment_recovery_retires_legacy_review_from_abandoned_section_task(self):
        from django.utils import timezone
        from deals.services.vdr_queue import _retire_superseded_inference_children
        parent = AIAuditLog.objects.create(status='PROCESSING', source_metadata={
            'active_report_units': {'Key Financials': {'current_task_id':'financial-task','dispatch_generation':2}}})
        legacy = AIAuditLog.objects.create(status='PROCESSING', source_type='report_section_quality_review',
            celery_task_id='financial-task', source_metadata={'vdr_parent_audit_id':str(parent.id)})
        unrelated = AIAuditLog.objects.create(status='PROCESSING', source_type='report_section_quality_review',
            celery_task_id='another-task', source_metadata={'vdr_parent_audit_id':str(parent.id)})
        retired = _retire_superseded_inference_children(parent_audit_id=str(parent.id), dispatch_generation=2, now=timezone.now())
        self.assertIn(str(legacy.id), retired)
        self.assertNotIn(str(unrelated.id), retired)
        legacy.refresh_from_db()
        self.assertEqual(legacy.status, 'FAILED')
