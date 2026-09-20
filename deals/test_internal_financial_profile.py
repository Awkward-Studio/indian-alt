from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from ai_orchestrator.models import AIAuditLog
from deals.models import (
    Deal,
    DealAnalysis,
    DealDocument,
    VentureIntelligenceCompanyProfile,
    VentureIntelligenceCompanyRelation,
    VentureIntelligenceFinancialStatement,
)
from deals.services.internal_financial_profile import (
    InternalFinancialProfileService,
    NoSupportedFinancialData,
)
from deals.services.deal_synthesis import DealSynthesisService


class FakeEvidenceService:
    def __init__(self, **kwargs):
        self.kwargs = kwargs

    def retrieve(self, title):
        assert title == "Key Financials"
        return {
            "context": "R001 Acme Private Limited. Manufacturing. Revenue was INR 12 crore in FY24.",
            "metadata": {"selected_chunk_count": 1},
            "citations": {
                "1": {"document_id": "doc-1", "title": "FY24 accounts"},
            },
        }


class InternalFinancialProfileServiceTests(TestCase):
    def setUp(self):
        self.deal = Deal.objects.create(title="Acme")
        DealDocument.objects.create(deal=self.deal, title="FY24 accounts", is_indexed=True)

    def test_fills_supported_missing_values_and_preserves_existing_values(self):
        profile = VentureIntelligenceCompanyProfile.objects.create(
            name="Acme",
            industry="Existing industry",
            data_source="venture_intelligence",
        )
        VentureIntelligenceCompanyRelation.objects.create(
            deal=self.deal,
            company_profile=profile,
            relation_type="target",
        )
        VentureIntelligenceFinancialStatement.objects.create(
            company_profile=profile,
            statement_type="profit_loss",
            fy="FY24",
            fin_type="Consolidated",
            data={"fy": "FY24", "fin_type": "Consolidated", "ebitda": "INR 3 crore"},
        )
        processor = SimpleNamespace(process_content=lambda **kwargs: {
            "profile": {
                "industry": {"value": "Manufacturing", "evidence_refs": ["R001"]},
                "sector": {"value": "Manufacturing", "evidence_refs": ["R001"]},
                "city": {"value": "Unsupported", "evidence_refs": ["R999"]},
            },
            "financial_statements": [{
                "statement_type": "profit_loss",
                "fy": "FY24",
                "fin_type": "Consolidated",
                "metrics": {
                    "revenue": {"value": "INR 12 crore", "evidence_refs": ["R001"]},
                    "ebitda": {"value": "INR 12 crore", "evidence_refs": ["R001"]},
                    "guess": {"value": "99", "evidence_refs": []},
                },
            }],
        })

        result = InternalFinancialProfileService(
            ai_processor=processor,
            evidence_service_class=FakeEvidenceService,
        ).extract(deal=self.deal)

        profile.refresh_from_db()
        statement = profile.financial_statements.get()
        self.assertEqual(profile.industry, "Existing industry")
        self.assertEqual(profile.sector, "Manufacturing")
        self.assertIsNone(profile.city)
        self.assertEqual(statement.data["revenue"], "INR 12 crore")
        self.assertEqual(statement.data["ebitda"], "INR 3 crore")
        self.assertEqual(statement.data_source, "mixed")
        self.assertEqual(statement.provenance["metrics"]["revenue"]["source"], "local_ai")
        self.assertNotIn("ebitda", statement.provenance["metrics"])
        self.assertNotIn("guess", statement.data)
        self.assertEqual(result["profile_fields_added"], 1)
        self.assertEqual(result["financial_metrics_added"], 1)

    def test_creates_internal_target_profile_without_vi_fetch(self):
        processor = SimpleNamespace(process_content=lambda **kwargs: {
            "profile": {"registered_name": {"value": "Acme Private Limited", "evidence_refs": ["R001"]}},
            "financial_statements": [],
        })
        result = InternalFinancialProfileService(
            ai_processor=processor,
            evidence_service_class=FakeEvidenceService,
        ).extract(deal=self.deal)

        profile = VentureIntelligenceCompanyProfile.objects.get(id=result["profile_id"])
        self.assertEqual(profile.data_source, "internal_documents")
        self.assertEqual(profile.registered_name, "Acme Private Limited")
        self.assertTrue(VentureIntelligenceCompanyRelation.objects.filter(
            deal=self.deal, company_profile=profile, relation_type="target",
        ).exists())

    def test_refreshes_only_local_ai_owned_profile_and_metrics(self):
        profile = VentureIntelligenceCompanyProfile.objects.create(
            name="Acme",
            industry="VI industry",
            sector="Old AI sector",
            data_source="venture_intelligence",
            raw_profile_json={
                "internal_document_extraction": {
                    "profile": {
                        "sector": {"source": "local_ai", "value": "Old AI sector"},
                    },
                },
            },
        )
        VentureIntelligenceCompanyRelation.objects.create(
            deal=self.deal,
            company_profile=profile,
            relation_type="target",
        )
        statement = VentureIntelligenceFinancialStatement.objects.create(
            company_profile=profile,
            statement_type="profit_loss",
            fy="FY24",
            fin_type="Consolidated",
            data={"revenue": "INR 10 crore", "ebitda": "INR 3 crore"},
            data_source="mixed",
            provenance={
                "metrics": {
                    "revenue": {"source": "local_ai", "evidence_refs": ["R001"]},
                    "ebitda": {"source": "venture_intelligence"},
                },
            },
        )
        processor = SimpleNamespace(process_content=lambda **kwargs: {
            "profile": {
                "industry": {"value": "AI industry", "evidence_refs": ["R001"]},
                "sector": {"value": "Manufacturing", "evidence_refs": ["R001"]},
            },
            "financial_statements": [{
                "statement_type": "profit_loss",
                "fy": "FY24",
                "fin_type": "Consolidated",
                "metrics": {
                    "revenue": {"value": "INR 12 crore", "evidence_refs": ["R001"]},
                    "ebitda": {"value": "INR 4 crore", "evidence_refs": ["R001"]},
                },
            }],
        })

        result = InternalFinancialProfileService(
            ai_processor=processor,
            evidence_service_class=FakeEvidenceService,
        ).extract(deal=self.deal)

        profile.refresh_from_db()
        statement.refresh_from_db()
        self.assertEqual(profile.industry, "VI industry")
        self.assertEqual(profile.sector, "Manufacturing")
        self.assertEqual(statement.data["revenue"], "INR 12 crore")
        self.assertEqual(statement.data["ebitda"], "INR 3 crore")
        self.assertEqual(result["profile_fields_refreshed"], 1)
        self.assertEqual(result["financial_metrics_refreshed"], 1)


class DealSynthesisServiceTests(TestCase):
    def setUp(self):
        self.deal = Deal.objects.create(title="Synthesis Deal")
        self.analysis = DealAnalysis.objects.create(
            deal=self.deal,
            version=1,
            analysis_json={"metadata": {"field_synthesis_key": "manual:1"}},
        )

    @patch("deals.services.deal_synthesis.InternalFinancialProfileService")
    @patch("deals.services.deal_synthesis.DealFieldSynthesisService.synthesize")
    def test_records_financial_result_and_reuses_it_on_retry(self, synthesize, financial_cls):
        synthesize.return_value = self.analysis
        financial_cls.return_value.extract.return_value = {
            "profile_fields_added": 1,
            "financial_metrics_added": 2,
        }

        first = DealSynthesisService.run(
            self.deal,
            batch_key="manual:1",
            source_type="manual_vdr",
        )
        replay = DealSynthesisService.run(
            self.deal,
            batch_key="manual:1",
            source_type="manual_vdr",
        )

        self.assertEqual(first["financial"]["status"], "completed")
        self.assertTrue(replay["idempotent_replay"])
        self.assertEqual(financial_cls.return_value.extract.call_count, 1)

    @patch("deals.services.deal_synthesis.InternalFinancialProfileService")
    @patch("deals.services.deal_synthesis.DealFieldSynthesisService.synthesize")
    def test_financial_failure_completes_with_warning(self, synthesize, financial_cls):
        synthesize.return_value = self.analysis
        financial_cls.return_value.extract.side_effect = RuntimeError("retrieval unavailable")

        result = DealSynthesisService.run(
            self.deal,
            batch_key="manual:1",
            source_type="manual_vdr",
        )

        self.assertEqual(result["financial"]["status"], "completed_with_warning")
        self.assertEqual(result["financial"]["warning"], "retrieval unavailable")

    @patch("deals.services.deal_synthesis.InternalFinancialProfileService")
    @patch("deals.services.deal_synthesis.DealFieldSynthesisService.synthesize")
    def test_missing_financial_evidence_is_recorded_as_skipped(self, synthesize, financial_cls):
        synthesize.return_value = self.analysis
        financial_cls.return_value.extract.side_effect = NoSupportedFinancialData("No supported data")

        result = DealSynthesisService.run(
            self.deal,
            batch_key="manual:1",
            source_type="manual_vdr",
        )

        self.assertEqual(result["financial"]["status"], "skipped")
        self.assertEqual(result["financial"]["warning"], "No supported data")


class InternalFinancialFillViewTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="analyst", password="test")
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.deal = Deal.objects.create(title="Acme")

    def test_rejects_deal_without_indexed_documents(self):
        response = self.client.post(f"/api/deals/{self.deal.id}/financials/fill-from-documents/", {})
        self.assertEqual(response.status_code, 400)

    @patch("deals.tasks.fill_deal_financials_from_documents_task.apply_async")
    def test_queues_fill_with_audit_log(self, apply_async):
        DealDocument.objects.create(deal=self.deal, title="Accounts", is_indexed=True)
        apply_async.return_value = SimpleNamespace(id="task-123")
        response = self.client.post(f"/api/deals/{self.deal.id}/financials/fill-from-documents/", {})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["status"], "queued")
        self.assertEqual(response.data["task_id"], "task-123")
        apply_async.assert_called_once()

    @patch("deals.tasks.redo_deal_synthesis_task.apply_async")
    def test_queues_synthesis_only_run_with_audit_log(self, apply_async):
        document = DealDocument.objects.create(
            deal=self.deal,
            title="Accounts",
            is_indexed=True,
        )
        apply_async.return_value = SimpleNamespace(id="synthesis-task-1")

        response = self.client.post(f"/api/deals/{self.deal.id}/redo-deal-synthesis/", {})

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.data["task_id"], "synthesis-task-1")
        synthesis_audit = AIAuditLog.objects.get(id=response.data["audit_log_id"])
        self.assertEqual(synthesis_audit.source_type, "deal_synthesis")
        self.assertEqual(
            synthesis_audit.source_metadata["required_document_ids"],
            [str(document.id)],
        )

    def test_rejects_synthesis_without_indexed_documents(self):
        response = self.client.post(f"/api/deals/{self.deal.id}/redo-deal-synthesis/", {})
        self.assertEqual(response.status_code, 400)

    @patch("deals.tasks.redo_deal_synthesis_task.apply_async")
    def test_reuses_active_synthesis(self, apply_async):
        DealDocument.objects.create(deal=self.deal, title="Accounts", is_indexed=True)
        active = AIAuditLog.objects.create(
            source_type="deal_synthesis",
            source_id=str(self.deal.id),
            context_label="Existing synthesis",
            model_used="test-model",
            system_prompt="test",
            user_prompt="test",
            status="PROCESSING",
            is_success=False,
            celery_task_id="existing-task",
        )

        response = self.client.post(f"/api/deals/{self.deal.id}/redo-deal-synthesis/", {})

        self.assertEqual(response.status_code, 202)
        self.assertTrue(response.data["reused"])
        self.assertEqual(response.data["audit_log_id"], str(active.id))
        apply_async.assert_not_called()


class RedoDealSynthesisTaskTests(TestCase):
    @patch("deals.tasks.broadcast_audit_log_update")
    @patch("deals.services.deal_synthesis.DealSynthesisService.run")
    def test_completes_core_synthesis_with_financial_warning(self, run, _broadcast):
        from deals.tasks import redo_deal_synthesis_task

        deal = Deal.objects.create(title="Task Deal")
        audit = AIAuditLog.objects.create(
            source_type="deal_synthesis",
            source_id=str(deal.id),
            context_label="Redo synthesis",
            model_used="test-model",
            system_prompt="test",
            user_prompt="test",
            status="PENDING",
            is_success=False,
            source_metadata={"required_document_ids": ["doc-1"]},
        )
        run.return_value = {
            "analysis": SimpleNamespace(id="analysis-1", version=2),
            "financial": {
                "status": "completed_with_warning",
                "summary": {},
                "warning": "No supported statements found.",
            },
        }

        result = redo_deal_synthesis_task.run(str(deal.id), str(audit.id))

        audit.refresh_from_db()
        self.assertEqual(result["status"], "SUCCESS")
        self.assertEqual(audit.status, "COMPLETED")
        self.assertTrue(audit.is_success)
        self.assertEqual(audit.source_metadata["workflow_stage"], "completed_with_warning")
        self.assertEqual(
            audit.parsed_json["financial"]["warning"],
            "No supported statements found.",
        )
