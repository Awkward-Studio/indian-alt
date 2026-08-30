from django.test import TestCase

from deals.models import Deal
from deals.services.deal_flow import DealFlowService, DealFlowValidationError


class DealFlowStatusTests(TestCase):
    def test_rejects_obsolete_or_arbitrary_status(self):
        deal = Deal.objects.create(title="Status validation")

        with self.assertRaisesRegex(DealFlowValidationError, "Unknown deal stage"):
            DealFlowService.update_flow_state(deal, active_stage="New")

        deal.refresh_from_db()
        self.assertEqual(deal.current_phase, "1: Deal Sourced")
        self.assertEqual(deal.deal_status, "1: Deal Sourced")

    def test_keeps_canonical_status_fields_in_sync(self):
        deal = Deal.objects.create(title="Portfolio validation")

        DealFlowService.update_flow_state(deal, active_stage="Portfolio")

        deal.refresh_from_db()
        self.assertEqual(deal.current_phase, "Portfolio")
        self.assertEqual(deal.deal_status, "Portfolio")
