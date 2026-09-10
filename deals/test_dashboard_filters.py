from django.contrib.auth.models import User
from django.test import TestCase
from rest_framework.test import APIClient

from ai_orchestrator.models import AIAuditLog
from deals.models import Deal
from deals.serializers import DealListSerializer
from deals.views import DealFilterSet, DealViewSet
from deals.models import DealDocument


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

    def test_dashboard_exposes_folder_and_deal_document_counts(self):
        linked = Deal.objects.create(
            title="Linked dataroom",
            source_onedrive_id="folder-1",
            source_drive_id="drive-1",
        )
        unlinked = Deal.objects.create(title="Unlinked dataroom")
        DealDocument.objects.create(deal=linked, title="Memo")
        DealDocument.objects.create(deal=linked, title="Model")
        AIAuditLog.objects.create(
            source_type="onedrive_folder",
            source_id="folder-1",
            model_used="test",
            system_prompt="",
            user_prompt="",
            raw_response="",
            source_metadata={
                "drive_id": "drive-1",
                "total_files": 7,
                "file_tree": [{"id": "file-1"}],
            },
        )

        queryset = DealViewSet.queryset.filter(pk__in=[linked.pk, unlinked.pk])
        payload = {
            row["title"]: row
            for row in DealListSerializer(queryset, many=True).data
        }

        self.assertEqual(payload["Linked dataroom"]["folder_linked"], True)
        self.assertEqual(payload["Linked dataroom"]["deal_document_count"], 2)
        self.assertEqual(payload["Linked dataroom"]["folder_document_count"], 7)
        self.assertEqual(payload["Unlinked dataroom"]["folder_linked"], False)
        self.assertEqual(payload["Unlinked dataroom"]["deal_document_count"], 0)
        self.assertIsNone(payload["Unlinked dataroom"]["folder_document_count"])

        filtered = DealFilterSet(
            data={
                "folder_linked": "true",
                "deal_document_count_min": "2",
                "folder_document_count_min": "7",
            },
            queryset=DealViewSet.queryset.all(),
        ).qs
        self.assertEqual(list(filtered.values_list("title", flat=True)), ["Linked dataroom"])

    def test_search_matches_raw_names_and_tolerates_small_typos(self):
        deal = Deal.objects.create(
            title="Acme Biotech",
            bank_name="HDFC Bank",
            primary_contact_name="Priya Shah",
        )
        Deal.objects.create(title="Unrelated company")

        response = self.client.get(
            "/api/deals/",
            {"search": "Acm Biotech", "page_size": 100},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual([row["id"] for row in response.data["results"]], [str(deal.id)])

        banker_response = self.client.get(
            "/api/deals/",
            {"search": "HDFC", "page_size": 100},
        )
        self.assertEqual(banker_response.status_code, 200)
        self.assertEqual([row["id"] for row in banker_response.data["results"]], [str(deal.id)])
