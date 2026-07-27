from django.test import TestCase
from datetime import date
from rest_framework.test import APIClient

from accounts.models import Profile
from django.contrib.auth.models import User
from deals.models import Deal, DealPhaseLog
from deals.serializers import DealSerializer
from deals.services.deal_flow import DealFlowService, DealFlowValidationError


class PassReasonContractTests(TestCase):
    def test_serializer_requires_reason_for_new_pass_transition(self):
        deal = Deal.objects.create(title="Scoped", current_phase="1: Deal Sourced")
        serializer = DealSerializer(
            deal,
            data={"current_phase": "Passed", "reasons_for_passing": "   "},
            partial=True,
        )

        self.assertFalse(serializer.is_valid())
        self.assertIn("reasons_for_passing", serializer.errors)

    def test_serializer_preserves_existing_reason_on_pass_transition(self):
        deal = Deal.objects.create(
            title="Scoped",
            current_phase="1: Deal Sourced",
            reasons_for_passing="Existing investment-team rationale",
        )
        serializer = DealSerializer(deal, data={"current_phase": "Passed"}, partial=True)

        self.assertTrue(serializer.is_valid(), serializer.errors)
        updated = serializer.save()
        self.assertEqual(updated.reasons_for_passing, "Existing investment-team rationale")

    def test_unrelated_edit_of_legacy_passed_deal_is_not_blocked(self):
        deal = Deal.objects.create(title="Legacy", current_phase="Passed", reasons_for_passing=None)
        serializer = DealSerializer(deal, data={"city": "Mumbai"}, partial=True)

        self.assertTrue(serializer.is_valid(), serializer.errors)

    def test_flow_service_rejects_blank_pass_reason_without_mutation(self):
        deal = Deal.objects.create(title="Rejected", current_phase="5: Financial Model Call")

        with self.assertRaises(DealFlowValidationError):
            DealFlowService.update_flow_state(deal, active_stage="Passed", reason=" ")

        deal.refresh_from_db()
        self.assertEqual(deal.current_phase, "5: Financial Model Call")
        self.assertFalse(DealPhaseLog.objects.filter(deal=deal).exists())

    def test_flow_service_persists_canonical_reason_and_log(self):
        deal = Deal.objects.create(title="Rejected", current_phase="5: Financial Model Call")

        DealFlowService.update_flow_state(
            deal,
            active_stage="Passed",
            decisions_update={"5": "no"},
            reason="  Unit economics do not meet threshold  ",
            rejection_stage_id=5,
        )

        deal.refresh_from_db()
        log = DealPhaseLog.objects.get(deal=deal)
        self.assertEqual(deal.reasons_for_passing, "Unit economics do not meet threshold")
        self.assertEqual(deal.rejection_reason, "Unit economics do not meet threshold")
        self.assertEqual(log.rationale, "Unit economics do not meet threshold")


class PassReasonApiTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="pass-reviewer", password="test-password")
        Profile.objects.create(
            user=self.user,
            name="Reviewer",
            email="pass-reviewer@example.com",
        )
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def test_update_flow_endpoint_returns_400_for_blank_reason(self):
        deal = Deal.objects.create(title="API Deal", current_phase="4: Initial Materials Review")

        response = self.client.post(
            f"/api/deals/{deal.id}/update_flow_state/",
            {"active_stage": "Passed", "reason": "   ", "rejection_stage_id": 4},
            format="json",
        )

        self.assertEqual(response.status_code, 400)
        deal.refresh_from_db()
        self.assertEqual(deal.current_phase, "4: Initial Materials Review")

    def test_receipt_date_descending_keeps_undated_deals_last(self):
        Deal.objects.create(title="Undated", received_at=None)
        newest = Deal.objects.create(title="Newest", received_at=date(2026, 1, 2))
        older = Deal.objects.create(title="Older", received_at=date(2025, 1, 2))

        response = self.client.get("/api/deals/", {"ordering": "-received_at"})

        self.assertEqual(response.status_code, 200)
        ids = [row["id"] for row in response.data["results"]]
        self.assertLess(ids.index(str(newest.id)), ids.index(str(older.id)))
        self.assertLess(ids.index(str(older.id)), ids.index(str(
            Deal.objects.get(title="Undated").id
        )))
