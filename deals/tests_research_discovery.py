from unittest.mock import MagicMock, patch

from django.contrib.auth.models import User
from django.urls import reverse
from django.test import SimpleTestCase, TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import Profile
from ai_orchestrator.models import AIAuditLog
from deals.serializers import DealSerializer
from deals.models import (
    Deal,
    SectorResearchDiscoveryRun,
    SectorResearchRecommendation,
)
from deals.services.research_discovery import (
    ResearchDiscoveryCoordinator,
    ResearchDiscoveryService,
)
from deals.tasks import discover_sector_reports_task


class ResearchDiscoveryServiceTests(SimpleTestCase):
    def setUp(self):
        self.search = MagicMock()
        self.service = ResearchDiscoveryService(search_service=self.search)

    @patch.object(
        ResearchDiscoveryService,
        "_probe_access",
        return_value=("AVAILABLE", "application/pdf"),
    )
    def test_discovers_and_deduplicates_grounded_public_research(self, _probe):
        self.search.normalize_url.side_effect = (
            lambda value: str(value).rstrip("/").casefold()
        )
        self.search.search_many.return_value = [
            {
                "title": "India Logistics Industry Report 2026",
                "snippet": "A market study covering logistics growth in India.",
                "url": "https://www.ibef.org/reports/logistics.pdf",
                "published_date": "2026-05-10",
                "query": "India logistics industry report",
                "score": 2.4,
            },
            {
                "title": "Duplicate",
                "snippet": "Same report.",
                "url": "https://www.ibef.org/reports/logistics.pdf/",
                "query": "duplicate",
            },
            {
                "title": "Social profile",
                "snippet": "Not permitted.",
                "url": "https://linkedin.com/posts/report",
            },
        ]
        deal = MagicMock(
            title="Fast Freight",
            sector="Logistics",
            industry="Supply Chain",
        )

        result = self.service.discover(deal=deal, cin="U12345MH2020PLC123456")

        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(result["result_count"], 1)
        item = result["recommendations"][0]
        self.assertEqual(item["document_type"], "INDUSTRY_REPORT")
        self.assertEqual(item["accessibility"], "AVAILABLE")
        self.assertTrue(item["preferred_source"])
        self.assertEqual(item["publication_date"], "2026-05-10")
        self.assertIn("Logistics", item["reason"])

    def test_requires_deal_context_and_builds_cin_filing_query(self):
        with self.assertRaisesMessage(ValueError, "required"):
            self.service.discover(
                deal=MagicMock(title="", sector="", industry=""),
                cin="",
            )

        queries = self.service.build_queries(
            company_name="Example Limited",
            sector="Consumer",
            industry="Retail",
            cin="U12345MH2020PLC123456",
        )
        self.assertTrue(any("MCA ROC filing" in query for query in queries))


class ResearchDiscoveryTaskTests(TestCase):
    @patch(
        "deals.services.research_discovery.ResearchDiscoveryService.discover"
    )
    def test_task_uses_deal_context_and_returns_terminal_payload(self, discover):
        deal = Deal.objects.create(
            title="Task Company",
            sector="Healthcare",
            industry="Diagnostics",
        )
        discover.return_value = {
            "status": "COMPLETED",
            "recommendations": [],
            "result_count": 0,
        }

        result = discover_sector_reports_task.run(str(deal.id))

        self.assertEqual(result["status"], "COMPLETED")
        discover.assert_called_once()

    def test_missing_deal_is_a_deterministic_failure(self):
        result = discover_sector_reports_task.run(
            "00000000-0000-0000-0000-000000000000"
        )

        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(result["error"], "Deal not found.")


class ResearchRecommendationStoreTests(TestCase):
    def setUp(self):
        self.deal = Deal.objects.create(
            title="Fast Freight",
            sector="Logistics",
            industry="Supply Chain",
        )
        self.run = SectorResearchDiscoveryRun.objects.create(
            deal=self.deal,
            context_hash="context",
        )
        self.service = ResearchDiscoveryService(search_service=MagicMock())

    def test_scores_accessible_relevant_preferred_source_above_weak_result(self):
        strong = {
            "title": "Fast Freight Logistics Industry Report",
            "snippet": "Supply chain outlook",
            "source_query": "logistics report",
            "publisher_domain": "ibef.org",
            "preferred_source": True,
            "publication_date": timezone.localdate().isoformat(),
            "accessibility": "AVAILABLE",
            "search_score": 4,
        }
        weak = {
            "title": "Unrelated archive",
            "snippet": "",
            "source_query": "",
            "publisher_domain": "example.com",
            "preferred_source": False,
            "publication_date": "2018-01-01",
            "accessibility": "UNAVAILABLE",
            "search_score": 0,
        }

        strong_score = self.service.score_recommendation(
            strong,
            company_name=self.deal.title,
            sector=self.deal.sector,
            industry=self.deal.industry,
            cin="",
        )
        weak_score = self.service.score_recommendation(
            weak,
            company_name=self.deal.title,
            sector=self.deal.sector,
            industry=self.deal.industry,
            cin="",
        )

        self.assertGreater(strong_score["total_score"], weak_score["total_score"])
        self.assertIn(
            "logistics",
            strong_score["score_explanation"]["matched_context_tokens"],
        )

    def test_persistence_upserts_by_deal_and_canonical_url(self):
        recommendation = {
            "canonical_url": "https://ibef.org/report.pdf",
            "url": "https://ibef.org/report.pdf",
            "title": "Logistics report",
            "document_type": "INDUSTRY_REPORT",
            "accessibility": "AVAILABLE",
            "retrieved_at": timezone.now().isoformat(),
            "total_score": 0.8,
        }
        first = self.service.persist_recommendations(
            deal=self.deal,
            run=self.run,
            payload={"recommendations": [recommendation]},
        )
        recommendation["title"] = "Updated logistics report"
        second = self.service.persist_recommendations(
            deal=self.deal,
            run=self.run,
            payload={"recommendations": [recommendation]},
        )

        self.assertEqual(len(first), 1)
        self.assertEqual(len(second), 1)
        self.assertEqual(SectorResearchRecommendation.objects.count(), 1)
        stored = SectorResearchRecommendation.objects.get()
        self.assertEqual(stored.title, "Updated logistics report")
        self.assertEqual(stored.run, self.run)


class ResearchDiscoveryWorkflowTests(TestCase):
    def setUp(self):
        self.deal = Deal.objects.create(
            title="Workflow Company",
            sector="Healthcare",
            industry="Diagnostics",
        )

    @patch("deals.tasks.discover_sector_reports_task.delay")
    def test_duplicate_active_context_returns_existing_run(self, delay):
        delay.return_value.id = "celery-1"

        first, first_created = ResearchDiscoveryCoordinator.enqueue(
            deal=self.deal,
            trigger="MANUAL",
        )
        second, second_created = ResearchDiscoveryCoordinator.enqueue(
            deal=self.deal,
            trigger="MANUAL",
        )

        self.assertTrue(first_created)
        self.assertFalse(second_created)
        self.assertEqual(first.id, second.id)
        self.assertEqual(SectorResearchDiscoveryRun.objects.count(), 1)
        delay.assert_called_once_with(str(self.deal.id), str(first.id))

    @patch(
        "deals.services.research_discovery.ResearchDiscoveryCoordinator.enqueue"
    )
    def test_serializer_schedules_create_and_context_change_triggers(
        self,
        enqueue,
    ):
        with self.captureOnCommitCallbacks(execute=True):
            created = DealSerializer(
                data={
                    "title": "New Diagnostics",
                    "sector": "Healthcare",
                    "industry": "Diagnostics",
                }
            )
            self.assertTrue(created.is_valid(), created.errors)
            deal = created.save()

        with self.captureOnCommitCallbacks(execute=True):
            updated = DealSerializer(
                deal,
                data={"sector": "Health Technology"},
                partial=True,
            )
            self.assertTrue(updated.is_valid(), updated.errors)
            updated.save()

        self.assertEqual(enqueue.call_count, 2)
        self.assertEqual(enqueue.call_args_list[0].kwargs["trigger"], "AUTO_CREATE")
        self.assertEqual(
            enqueue.call_args_list[1].kwargs["trigger"],
            "AUTO_CONTEXT_CHANGE",
        )

    @patch("deals.tasks.discover_sector_reports_task.delay")
    @patch(
        "deals.services.research_discovery.ResearchDiscoveryService.discover"
    )
    def test_worker_persists_results_and_ai_history(self, discover, delay):
        delay.return_value.id = "celery-worker"
        run, _created = ResearchDiscoveryCoordinator.enqueue(
            deal=self.deal,
            trigger="MANUAL",
        )
        discover.return_value = {
            "status": "COMPLETED",
            "queries": ["diagnostics industry report"],
            "recommendations": [
                {
                    "canonical_url": "https://ibef.org/diagnostics.pdf",
                    "url": "https://ibef.org/diagnostics.pdf",
                    "title": "Diagnostics report",
                    "document_type": "INDUSTRY_REPORT",
                    "accessibility": "AVAILABLE",
                    "retrieved_at": timezone.now().isoformat(),
                    "total_score": 0.9,
                }
            ],
            "result_count": 1,
        }

        result = discover_sector_reports_task.run(
            str(self.deal.id),
            str(run.id),
        )

        run.refresh_from_db()
        self.assertEqual(result["persisted_count"], 1)
        self.assertEqual(run.status, "COMPLETED")
        self.assertEqual(run.queries, ["diagnostics industry report"])
        self.assertEqual(self.deal.research_recommendations.count(), 1)
        audit = AIAuditLog.objects.get(id=run.audit_log_id)
        self.assertEqual(audit.status, "COMPLETED")
        self.assertTrue(audit.is_success)
        self.assertIsNotNone(audit.completed_at)


class ResearchDiscoveryApiTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("research-analyst")
        self.profile = Profile.objects.create(
            user=self.user,
            name="Research Analyst",
            email="research@example.com",
        )
        self.other_user = User.objects.create_user("research-other")
        Profile.objects.create(
            user=self.other_user,
            name="Other Analyst",
            email="research-other@example.com",
        )
        self.deal = Deal.objects.create(
            title="API Company",
            sector="Technology",
            industry="Software",
        )
        self.deal.responsibility.add(self.profile)
        self.run = SectorResearchDiscoveryRun.objects.create(
            deal=self.deal,
            context_hash=ResearchDiscoveryCoordinator.context_hash(self.deal),
            status="COMPLETED",
            completed_at=timezone.now(),
        )
        SectorResearchRecommendation.objects.create(
            deal=self.deal,
            run=self.run,
            canonical_url="https://ibef.org/software.pdf",
            url="https://ibef.org/software.pdf",
            title="Software industry report",
            document_type="INDUSTRY_REPORT",
            accessibility="AVAILABLE",
            total_score=0.85,
            retrieved_at=timezone.now(),
            last_verified_at=timezone.now(),
        )
        self.client = APIClient()
        self.url = reverse(
            "deal-research-discovery",
            kwargs={"pk": self.deal.id},
        )

    def test_authenticated_user_can_read_ranked_recommendations(self):
        self.client.force_authenticate(self.other_user)

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.data["context_stale"])
        self.assertEqual(len(response.data["recommendations"]), 1)
        self.assertEqual(
            response.data["recommendations"][0]["accessibility"],
            "AVAILABLE",
        )

    @patch(
        "deals.services.research_discovery.ResearchDiscoveryCoordinator.enqueue"
    )
    def test_only_responsible_analyst_can_refresh(self, enqueue):
        enqueue.return_value = (self.run, True)
        self.client.force_authenticate(self.other_user)
        denied = self.client.post(self.url, {}, format="json")

        self.client.force_authenticate(self.user)
        allowed = self.client.post(self.url, {}, format="json")

        self.assertEqual(denied.status_code, 403)
        self.assertEqual(allowed.status_code, 202)
        enqueue.assert_called_once()

    def test_unauthenticated_request_is_rejected(self):
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 401)
