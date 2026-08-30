from django.contrib.auth.models import User
from django.test import TestCase
from rest_framework.test import APIClient

from deals.models import Deal


class DealTableFilterTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="deal-filter-reviewer",
            password="test-password",
        )
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def test_portfolio_filter_combines_invested_and_portfolio_rows_for_one_fund(self):
        current_portfolio = Deal.objects.create(
            title="Fund I portfolio",
            fund="Fund I",
            current_phase="Portfolio",
            deal_status="1: Deal Sourced",
        )
        invested = Deal.objects.create(
            title="Fund I invested",
            fund="FUND1",
            current_phase="Invested",
            deal_status="Invested",
        )
        Deal.objects.create(
            title="Fund I passed",
            fund="FUND1",
            current_phase="Passed",
            deal_status="Passed",
        )
        Deal.objects.create(
            title="Fund II portfolio",
            fund="FUND2",
            current_phase="Portfolio",
            deal_status="Portfolio",
        )

        response = self.client.get(
            "/api/deals/",
            {"deal_group": "portfolio", "fund": "FUND1"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["count"], 2)
        self.assertEqual(
            {row["id"] for row in response.data["results"]},
            {str(current_portfolio.id), str(invested.id)},
        )

    def test_active_filter_uses_the_canonical_current_phase(self):
        active = Deal.objects.create(
            title="Active deal",
            fund="FUND3",
            current_phase="4: Initial Materials Review",
            deal_status="4: Initial Materials Review",
        )
        stale_secondary_status = Deal.objects.create(
            title="Stale portfolio phase",
            fund="FUND3",
            current_phase="4: Initial Materials Review",
            deal_status="Portfolio",
        )

        response = self.client.get(
            "/api/deals/",
            {"deal_group": "active", "fund": "FUND3"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["count"], 2)
        self.assertEqual(
            {row["id"] for row in response.data["results"]},
            {str(active.id), str(stale_secondary_status.id)},
        )
