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
    DealDocument,
    SectorResearchAcquisition,
    SectorResearchDiscoveryRun,
    SectorResearchSourceRule,
    SectorResearchRecommendation,
)
from deals.services.research_acquisition import ResearchAcquisitionError, ResearchAcquisitionService
from deals.services.research_discovery import (
    ResearchDiscoveryCoordinator,
    ResearchDiscoveryService,
)
from deals.tasks import discover_sector_reports_task


class ResearchAcquisitionSecurityTests(SimpleTestCase):
    def _response(self, *, status=200, content_type="application/pdf", content=b"pdf", location=None, length=None):
        response = MagicMock(status_code=status)
        response.headers = {"Content-Type": content_type}
        if location:
            response.headers["Location"] = location
        if length is not None:
            response.headers["Content-Length"] = str(length)
        response.iter_content.return_value = [content]
        return response

    @patch("deals.services.research_discovery.socket.getaddrinfo", return_value=[(2, 1, 6, "", ("127.0.0.1", 443))])
    def test_private_dns_resolution_is_rejected(self, _dns):
        self.assertFalse(ResearchDiscoveryService.is_safe_public_url("https://publisher.example/report.pdf"))

    @patch.object(ResearchDiscoveryService, "is_safe_public_url", return_value=True)
    def test_redirects_are_revalidated_and_download_is_bounded(self, safe):
        http = MagicMock()
        http.get.side_effect = [
            self._response(status=302, location="https://cdn.example/report.pdf"),
            self._response(content=b"verified-pdf", length=12),
        ]
        content, access = ResearchAcquisitionService(http_session=http).download("https://publisher.example/report")
        self.assertEqual(content, b"verified-pdf")
        self.assertEqual(access["final_url"], "https://cdn.example/report.pdf")
        self.assertEqual(safe.call_count, 2)

    @patch.object(ResearchDiscoveryService, "is_safe_public_url", return_value=True)
    def test_mime_and_stream_size_policies_fail_deterministically(self, _safe):
        http = MagicMock()
        http.get.return_value = self._response(content_type="application/zip")
        with self.assertRaisesRegex(ResearchAcquisitionError, "not permitted"):
            ResearchAcquisitionService(http_session=http).download("https://publisher.example/archive")

        http.get.return_value = self._response(content=b"12345")
        service = ResearchAcquisitionService(http_session=http)
        service.max_bytes = 4
        with self.assertRaisesRegex(ResearchAcquisitionError, "exceeded"):
            service.download("https://publisher.example/report.pdf")


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


class ResearchSourceRuleApiTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("source-admin")
        self.profile = Profile.objects.create(
            user=self.user, name="Source Admin", email="source-admin@example.com", is_admin=True,
        )
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def test_admin_can_manage_preferred_source_rule(self):
        response = self.client.post("/api/deals/research-source-rules/", {
            "name": "O3 Capital", "domain": "www.o3capital.com",
            "is_preferred": True, "is_active": True,
            "query_templates": ["{market} research report filetype:pdf"],
            "rationale": "Preferred IA research source",
        }, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["domain"], "o3capital.com")

    def test_non_admin_cannot_mutate_source_rules(self):
        self.profile.is_admin = False
        self.profile.save(update_fields=["is_admin"])
        response = self.client.post("/api/deals/research-source-rules/", {
            "name": "Blocked", "domain": "blocked.example",
        }, format="json")
        self.assertEqual(response.status_code, 403)

    def test_rules_drive_preference_and_queries(self):
        SectorResearchSourceRule.objects.create(
            name="O3 Capital", domain="o3capital.com",
            query_templates=["{sector} investment research"],
        )
        service = ResearchDiscoveryService(search_service=MagicMock())
        self.assertTrue(service._is_preferred_domain("reports.o3capital.com"))


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


class ResearchAcquisitionWorkflowTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("acquisition-analyst")
        self.profile = Profile.objects.create(
            user=self.user,
            name="Acquisition Analyst",
            email="acquisition@example.com",
        )
        self.deal = Deal.objects.create(title="Acquisition Company", sector="Logistics")
        self.deal.responsibility.add(self.profile)
        self.run = SectorResearchDiscoveryRun.objects.create(
            deal=self.deal,
            context_hash=ResearchDiscoveryCoordinator.context_hash(self.deal),
            status="COMPLETED",
        )
        self.recommendation = SectorResearchRecommendation.objects.create(
            deal=self.deal,
            run=self.run,
            canonical_url="https://ibef.org/logistics.pdf",
            url="https://ibef.org/logistics.pdf",
            title="Logistics report",
            accessibility="AVAILABLE",
            content_type="application/pdf",
            retrieved_at=timezone.now(),
            last_verified_at=timezone.now(),
        )
        self.audit = AIAuditLog.objects.create(
            source_type="sector_research_acquisition",
            source_id=str(self.recommendation.id),
            model_used="runtime",
            system_prompt="",
            user_prompt="",
            raw_response="",
        )
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.acquire_url = f"/api/deals/{self.deal.id}/research-acquisition/{self.recommendation.id}/acquire/"

    @patch("deals.tasks.acquire_sector_research_task.delay")
    @patch("ai_orchestrator.services.runtime.AIRuntimeService.create_audit_log")
    def test_explicit_approval_queues_once_with_actor_and_audit(self, create_audit, delay):
        create_audit.return_value = self.audit
        delay.return_value.id = "task-1"
        denied = self.client.post(self.acquire_url, {}, format="json")
        queued = self.client.post(self.acquire_url, {"approved": True}, format="json")
        duplicate = self.client.post(self.acquire_url, {"approved": True}, format="json")

        self.assertEqual(denied.status_code, 400)
        self.assertEqual(queued.status_code, 202)
        self.assertEqual(duplicate.status_code, 200)
        acquisition = SectorResearchAcquisition.objects.get(recommendation=self.recommendation)
        self.assertEqual(acquisition.approved_by, self.user)
        self.assertEqual(acquisition.audit_log, self.audit)
        delay.assert_called_once_with(str(acquisition.id))

    @patch("ai_orchestrator.services.embedding_processor.EmbeddingService.vectorize_document", return_value=True)
    @patch("deals.services.document_artifacts.DocumentArtifactService.build_document_artifact")
    @patch("ai_orchestrator.services.document_processor.DocumentProcessorService.get_extraction_result")
    def test_attachment_is_provenance_linked_and_checksum_deduplicated(self, extract, artifact, vectorize):
        extract.return_value = {"normalized_text": "Grounded research text", "mode": "fallback_text"}
        artifact.return_value = {
            "normalized_text": "Grounded research text",
            "source_map": {"claim-1": {"page": 1}},
            "citations": [{"label": "p.1", "page": 1}],
        }
        acquisition = SectorResearchAcquisition.objects.create(
            deal=self.deal,
            recommendation=self.recommendation,
            approved_by=self.user,
            audit_log=self.audit,
            source_url=self.recommendation.canonical_url,
        )
        service = ResearchAcquisitionService()
        access = {
            "final_url": self.recommendation.url,
            "content_type": "application/pdf",
            "content_length": 9,
            "redirects": [],
            "verified_at": timezone.now().isoformat(),
        }
        document = service.attach(acquisition, b"pdf-bytes", access)

        acquisition.refresh_from_db()
        self.assertEqual(acquisition.status, "COMPLETED")
        self.assertEqual(acquisition.document, document)
        self.assertEqual(document.file_url, self.recommendation.url)
        self.assertEqual(document.evidence_json["citations"][0]["page"], 1)
        self.assertEqual(acquisition.citations[0]["label"], "p.1")

    @patch.object(ResearchAcquisitionService, "download", side_effect=ResearchAcquisitionError("UNSAFE_URL", "blocked"))
    def test_terminal_failure_never_creates_a_document(self, _download):
        acquisition = SectorResearchAcquisition.objects.create(
            deal=self.deal,
            recommendation=self.recommendation,
            approved_by=self.user,
            audit_log=self.audit,
            source_url=self.recommendation.canonical_url,
        )
        with self.assertRaises(ResearchAcquisitionError):
            ResearchAcquisitionService().execute(acquisition)
        acquisition.refresh_from_db()
        self.assertEqual(acquisition.status, "FAILED")
        self.assertEqual(acquisition.error_code, "UNSAFE_URL")
        self.assertIsNone(acquisition.document)
        self.assertFalse(DealDocument.objects.filter(deal=self.deal).exists())
