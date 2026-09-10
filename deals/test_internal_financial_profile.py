from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from deals.models import (
    Deal,
    DealDocument,
    VentureIntelligenceCompanyProfile,
    VentureIntelligenceCompanyRelation,
    VentureIntelligenceFinancialStatement,
)
from deals.services.internal_financial_profile import InternalFinancialProfileService


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
                "industry": {"value": "AI industry", "evidence_refs": ["R001"]},
                "sector": {"value": "Manufacturing", "evidence_refs": ["R001"]},
                "city": {"value": "Unsupported", "evidence_refs": ["R999"]},
            },
            "financial_statements": [{
                "statement_type": "profit_loss",
                "fy": "FY24",
                "fin_type": "Consolidated",
                "metrics": {
                    "revenue": {"value": "INR 12 crore", "evidence_refs": ["R001"]},
                    "ebitda": {"value": "INR 4 crore", "evidence_refs": ["R001"]},
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
