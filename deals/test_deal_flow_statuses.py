from django.test import SimpleTestCase, TestCase

from deals.models import Deal, DealStatus
from deals.serializers import DealSerializer
from deals.statuses import normalize_deal_status


class DealStatusTests(TestCase):
    def test_rejects_obsolete_or_arbitrary_status(self):
        deal = Deal.objects.create(title="Status validation")
        serializer = DealSerializer(
            deal,
            data={"deal_status": "3: NDA Execution"},
            partial=True,
        )

        self.assertFalse(serializer.is_valid())
        self.assertIn("deal_status", serializer.errors)

    def test_accepts_each_canonical_status(self):
        for status in DealStatus.values:
            payload = {"title": status, "deal_status": status}
            if status == DealStatus.PASSED:
                payload["reasons_for_passing"] = "Outside mandate"
            serializer = DealSerializer(data=payload)
            self.assertTrue(serializer.is_valid(), serializer.errors)


class DealStatusNormalizationTests(SimpleTestCase):
    def test_maps_numbered_legacy_stages(self):
        self.assertEqual(normalize_deal_status("1: Deal Sourced"), DealStatus.NEW)
        self.assertEqual(normalize_deal_status("12: Term Sheet"), DealStatus.INTERESTING)

    def test_maps_pass_and_portfolio_aliases(self):
        self.assertEqual(normalize_deal_status("To Be Passed"), DealStatus.TO_PASS)
        self.assertEqual(normalize_deal_status("Invested"), DealStatus.PORTFOLIO)

    def test_rejects_unknown_status(self):
        with self.assertRaisesRegex(ValueError, "Unknown deal status"):
            normalize_deal_status("Initial Review")
