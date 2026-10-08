from threading import Barrier
from types import SimpleNamespace
from unittest.mock import Mock, patch
from django.test import SimpleTestCase, override_settings
from deals.services.deal_field_synthesis import DealFieldSynthesisService

class ReportSynthesisParallelTests(SimpleTestCase):
    @override_settings(VDR_DURABLE_REPORT_CONCURRENCY=3, AI_INFERENCE_MAX_CONCURRENT_REQUESTS=3)
    def test_three_batches_run_concurrently_and_keep_their_input_order(self):
        barrier=Barrier(3)
        def run(_processor, **kwargs):
            barrier.wait(timeout=10)
            return [{'index':kwargs['phase_index']}]
        with patch.object(DealFieldSynthesisService,'_run_model_adaptive',side_effect=run):
            results=DealFieldSynthesisService._run_batches(Mock(),['one','two','three'],
                deal=SimpleNamespace(id='deal'),batch_key='batch',source_type='confirmed_report',
                phase='evidence_map',delivery_context={'audit_log_id':'parent','task_id':'task','generation':1})
        self.assertEqual(results,[{'index':0},{'index':1},{'index':2}])
