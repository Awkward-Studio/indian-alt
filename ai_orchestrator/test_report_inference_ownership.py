from django.test import TestCase
from ai_orchestrator.models import AIAuditLog
from ai_orchestrator.services.inference_queue import InferenceQueueLease, InferenceCancelled

class ReportInferenceOwnershipTests(TestCase):
    def test_refilling_another_slot_does_not_cancel_a_running_section(self):
        parent = AIAuditLog.objects.create(status='PROCESSING', source_metadata={
            'queue_version': 2, 'dispatch_generation': 4,
            'active_report_units': {'Key Financials': {'dispatch_generation': 2, 'current_task_id': 'financial-task'},
                                    'Risk Factors': {'dispatch_generation': 4, 'current_task_id': 'risk-task'}}})
        audit = AIAuditLog.objects.create(status='PROCESSING', celery_task_id='financial-task',
            source_type='vdr_report_section', source_metadata={'vdr_parent_audit_id': str(parent.id),
            'report_section': 'Key Financials', 'vdr_dispatch_generation': 2})
        InferenceQueueLease(audit).check_cancelled()
        parent.source_metadata['active_report_units']['Key Financials']['dispatch_generation'] = 5
        parent.save(update_fields=['source_metadata'])
        with self.assertRaises(InferenceCancelled):
            InferenceQueueLease(audit).check_cancelled()
