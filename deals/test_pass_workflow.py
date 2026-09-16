from django.test import TestCase
from datetime import date
from rest_framework.test import APIClient

from accounts.models import Profile
from django.contrib.auth.models import User
from deals.models import (
    Deal,
    DealAnalysis,
    DealPassReasonRemediationAudit,
)
from deals.serializers import DealSerializer


class PassReasonContractTests(TestCase):
    def test_serializer_requires_reason_for_new_pass_transition(self):
        deal = Deal.objects.create(title="Scoped", deal_status="New")
        serializer = DealSerializer(
            deal,
            data={"deal_status": "Passed", "reasons_for_passing": "   "},
            partial=True,
        )

        self.assertFalse(serializer.is_valid())
        self.assertIn("reasons_for_passing", serializer.errors)

    def test_serializer_preserves_existing_reason_on_pass_transition(self):
        deal = Deal.objects.create(
            title="Scoped",
            deal_status="New",
            reasons_for_passing="Existing investment-team rationale",
        )
        serializer = DealSerializer(deal, data={"deal_status": "Passed"}, partial=True)

        self.assertTrue(serializer.is_valid(), serializer.errors)
        updated = serializer.save()
        self.assertEqual(updated.reasons_for_passing, "Existing investment-team rationale")

    def test_unrelated_edit_of_legacy_passed_deal_is_not_blocked(self):
        deal = Deal.objects.create(title="Legacy", deal_status="Passed", reasons_for_passing=None)
        serializer = DealSerializer(deal, data={"city": "Mumbai"}, partial=True)

        self.assertTrue(serializer.is_valid(), serializer.errors)

class PassReasonApiTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="pass-reviewer", password="test-password")
        self.profile = Profile.objects.create(
            user=self.user,
            name="Reviewer",
            email="pass-reviewer@example.com",
        )
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def test_remediation_list_is_bounded_to_missing_passed_fund_one_and_two_deals(self):
        eligible = Deal.objects.create(
            title="Eligible",
            deal_status="Passed",
            fund="FUND1",
            reasons_for_passing="   ",
        )
        eligible.responsibility.add(self.profile)
        populated = Deal.objects.create(
            title="Already complete",
            deal_status="Passed",
            fund="FUND2",
            reasons_for_passing="Documented rationale",
        )
        populated.responsibility.add(self.profile)
        wrong_phase = Deal.objects.create(
            title="Active",
            deal_status="Interesting",
            fund="FUND1",
        )
        wrong_phase.responsibility.add(self.profile)
        fund_three = Deal.objects.create(
            title="Fund III",
            deal_status="Passed",
            fund="FUND3",
        )
        fund_three.responsibility.add(self.profile)

        response = self.client.get("/api/deals/pass-reason-remediation/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual([row["id"] for row in response.data["results"]], [str(eligible.id)])
        self.assertEqual(response.data["counts"], {"FUND1": 1, "FUND2": 0, "total": 1})

    def test_remediation_update_records_actor_and_removes_deal_from_queue(self):
        deal = Deal.objects.create(
            title="Legacy passed deal",
            deal_status="Passed",
            fund="FUND2",
            reasons_for_passing=None,
        )
        deal.responsibility.add(self.profile)

        response = self.client.patch(
            f"/api/deals/{deal.id}/pass-reason-remediation/",
            {
                "reason": "  Investment mandate mismatch  ",
                "expected_updated_at": deal.updated_at.isoformat(),
            },
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        deal.refresh_from_db()
        self.assertEqual(deal.reasons_for_passing, "Investment mandate mismatch")
        audit = DealPassReasonRemediationAudit.objects.get(deal=deal)
        self.assertEqual(audit.actor, self.user)
        self.assertIsNone(audit.previous_reason)
        self.assertEqual(audit.new_reason, "Investment mandate mismatch")
        queue = self.client.get("/api/deals/pass-reason-remediation/")
        self.assertEqual(queue.data["counts"]["total"], 0)

    def test_remediation_rejects_stale_update_without_audit(self):
        deal = Deal.objects.create(
            title="Concurrent edit",
            deal_status="Passed",
            fund="FUND1",
        )
        deal.responsibility.add(self.profile)
        stale_updated_at = deal.updated_at
        deal.city = "Mumbai"
        deal.save(update_fields=["city", "updated_at"])

        response = self.client.patch(
            f"/api/deals/{deal.id}/pass-reason-remediation/",
            {
                "reason": "Out of mandate",
                "expected_updated_at": stale_updated_at.isoformat(),
            },
            format="json",
        )

        self.assertEqual(response.status_code, 409)
        deal.refresh_from_db()
        self.assertIsNone(deal.reasons_for_passing)
        self.assertFalse(DealPassReasonRemediationAudit.objects.filter(deal=deal).exists())

    def test_remediation_rejects_unassigned_analyst(self):
        deal = Deal.objects.create(
            title="Another analyst's deal",
            deal_status="Passed",
            fund="FUND1",
        )

        response = self.client.patch(
            f"/api/deals/{deal.id}/pass-reason-remediation/",
            {
                "reason": "Out of mandate",
                "expected_updated_at": deal.updated_at.isoformat(),
            },
            format="json",
        )

        self.assertEqual(response.status_code, 403)
        self.assertFalse(DealPassReasonRemediationAudit.objects.filter(deal=deal).exists())

    def test_deal_update_returns_400_for_blank_pass_reason(self):
        deal = Deal.objects.create(title="API Deal", deal_status="Interesting")

        response = self.client.patch(
            f"/api/deals/{deal.id}/",
            {"deal_status": "Passed", "reasons_for_passing": "   "},
            format="json",
        )

        self.assertEqual(response.status_code, 400)
        deal.refresh_from_db()
        self.assertEqual(deal.deal_status, "Interesting")

    def test_non_passed_status_preserves_historic_rejection_reason(self):
        deal = Deal.objects.create(
            title="Reactivated",
            deal_status="Passed",
            rejection_reason="Weak financials",
            reasons_for_passing="Weak financials",
        )

        response = self.client.patch(
            f"/api/deals/{deal.id}/",
            {"deal_status": "Interesting"},
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        deal.refresh_from_db()
        self.assertEqual(deal.deal_status, "Interesting")
        self.assertEqual(deal.rejection_reason, "Weak financials")

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

    def test_deal_list_exposes_data_coverage_flags(self):
        deal = Deal.objects.create(
            title="Coverage",
            competitor_candidates=[{"name": "Peer Co"}],
        )
        DealAnalysis.objects.create(deal=deal)

        response = self.client.get("/api/deals/", {"search": "Coverage"})

        self.assertEqual(response.status_code, 200)
        row = response.data["results"][0]
        self.assertTrue(row["has_analysis"])
        self.assertFalse(row["has_vi_data"])
        self.assertTrue(row["has_competitors"])

    def test_deal_list_filters_data_coverage_flags(self):
        covered = Deal.objects.create(
            title="Covered",
            competitor_candidates=[{"name": "Peer Co"}],
        )
        DealAnalysis.objects.create(deal=covered)
        Deal.objects.create(title="Uncovered")

        response = self.client.get(
            "/api/deals/",
            {"has_analysis": "true", "has_competitors": "true"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [row["title"] for row in response.data["results"]],
            ["Covered"],
        )
