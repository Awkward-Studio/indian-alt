from django.contrib.auth.models import User
from django.test import TestCase
from rest_framework.test import APIClient

from deals.models import Deal, DealAnalysis


class DashboardMetricsApiTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="dashboard-reviewer",
            password="test-password",
        )
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def test_metrics_return_full_pipeline_fund_and_analysis_counts(self):
        available = Deal.objects.create(
            title="Analysis available",
            current_phase="4: Initial Materials Review",
            deal_status="4: Initial Materials Review",
            fund="FUND1",
            processing_status="completed",
        )
        DealAnalysis.objects.create(deal=available)
        Deal.objects.create(
            title="Analysis running",
            current_phase="7: Industry Research",
            deal_status="7: Industry Research",
            fund="FUND2",
            processing_status="processing",
        )
        Deal.objects.create(
            title="Analysis failed",
            current_phase="Passed",
            deal_status="Passed",
            fund="FUND3",
            processing_status="failed",
        )
        Deal.objects.create(
            title="Analysis not started",
            current_phase="1: Deal Sourced",
            deal_status="1: Deal Sourced",
            fund="",
            processing_status="idle",
        )

        response = self.client.get("/api/deals/dashboard_metrics/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["totalDeals"], 4)
        self.assertEqual(
            response.data["analysisCounts"],
            {
                "available": 1,
                "running": 1,
                "failed": 1,
                "notStarted": 1,
            },
        )
        self.assertEqual(
            {row["fund"]: row["count"] for row in response.data["fundCounts"]},
            {"FUND1": 1, "FUND2": 1, "FUND3": 1, "UNASSIGNED": 1},
        )
        self.assertEqual(
            {row["stage"]: row["count"] for row in response.data["stageCounts"]},
            {
                "1: Deal Sourced": 1,
                "4: Initial Materials Review": 1,
                "7: Industry Research": 1,
                "Passed": 1,
            },
        )

    def test_analysis_available_takes_precedence_over_processing_state(self):
        deal = Deal.objects.create(
            title="Existing analysis being refreshed",
            processing_status="processing",
        )
        DealAnalysis.objects.create(deal=deal)

        response = self.client.get("/api/deals/dashboard_metrics/")

        self.assertEqual(
            response.data["analysisCounts"],
            {
                "available": 1,
                "running": 0,
                "failed": 0,
                "notStarted": 0,
            },
        )
