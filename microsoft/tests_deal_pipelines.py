from datetime import timedelta
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from banks.models import Bank
from ai_orchestrator.models import AIAuditLog
from contacts.models import Contact
from deals.models import AnalysisKind, Deal, DealAnalysis, DealDocument
from deals.services.deal_creation import DealCreationService
from microsoft.models import Email, EmailAccount


class NewDealEmailPipelineTests(TestCase):
    def setUp(self):
        self.account = EmailAccount.objects.create(email="pipeline@example.test")
        self.first = Email.objects.create(
            email_account=self.account,
            graph_id="new-deal-1",
            conversation_id="new-deal-thread",
            subject="Acme opportunity",
            body_text="Initial opportunity",
            attachments=[{"id": "att-1", "name": "Acme Deck.pdf"}],
            date_received=timezone.now(),
        )
        self.reply = Email.objects.create(
            email_account=self.account,
            graph_id="new-deal-2",
            conversation_id="new-deal-thread",
            subject="Re: Acme opportunity",
            body_text="Updated model",
            attachments=[
                {"id": "att-1", "name": "Acme Deck.pdf"},
                {"id": "att-2", "name": "Acme Deck.pdf"},
            ],
            date_received=timezone.now() + timedelta(minutes=5),
        )
        self.analysis = {
            "deal_model_data": {"title": "Acme", "industry": "Fintech"},
            "analyst_report": "Initial investment analysis",
            "metadata": {"ambiguous_points": ["Verify churn"]},
        }
        self.contact = {
            "firm_name": "Example Bank",
            "firm_domain": "example.test",
            "name": "Banker One",
            "email": "banker@example.test",
        }

    @patch("ai_orchestrator.services.embedding_processor.EmbeddingService")
    def test_creation_retry_preserves_one_initial_evidence_graph(self, _embedding):
        deal = Deal.objects.create(title="Acme")
        payload = {
            "source_email_id": str(self.first.id),
            "analysis_json": self.analysis,
            "contact_discovery": self.contact,
        }

        DealCreationService.process_deal_creation(deal, payload)
        DealCreationService.process_deal_creation(deal, payload)

        self.assertEqual(DealAnalysis.objects.filter(deal=deal).count(), 1)
        self.assertEqual(DealDocument.objects.filter(deal=deal).count(), 2)
        self.assertEqual(
            set(DealDocument.objects.filter(deal=deal).values_list("onedrive_id", flat=True)),
            {"att-1", "att-2"},
        )
        self.assertFalse(Email.objects.filter(conversation_id="new-deal-thread", deal=deal).exclude(id__in=[self.first.id, self.reply.id]).exists())
        self.assertEqual(Email.objects.filter(conversation_id="new-deal-thread", deal=deal).count(), 2)
        contact = Contact.objects.get(email="banker@example.test")
        self.assertEqual(contact.source_count, 1)
        self.assertEqual(Bank.objects.filter(name="Example Bank").count(), 1)

    @patch("deals.services.deal_creation.DealCreationService._extract_documents_from_email")
    def test_creation_side_effects_roll_back_together(self, extract_documents):
        extract_documents.side_effect = RuntimeError("attachment persistence failed")
        deal = Deal.objects.create(title="Acme")

        with self.assertRaisesMessage(RuntimeError, "attachment persistence failed"):
            DealCreationService.process_deal_creation(
                deal,
                {
                    "source_email_id": str(self.first.id),
                    "analysis_json": self.analysis,
                    "contact_discovery": self.contact,
                },
            )

        self.first.refresh_from_db()
        self.assertIsNone(self.first.deal_id)
        self.assertEqual(DealAnalysis.objects.filter(deal=deal).count(), 0)
        self.assertEqual(DealDocument.objects.filter(deal=deal).count(), 0)
        self.assertFalse(Contact.objects.filter(email="banker@example.test").exists())
        self.assertFalse(Bank.objects.filter(name="Example Bank").exists())


class ExistingDealEmailPipelineTests(TestCase):
    def setUp(self):
        self.deal = Deal.objects.create(
            title="Curated Deal Title",
            priority="High",
            funding_ask="INR 50 Cr",
        )
        DealAnalysis.objects.create(
            deal=self.deal,
            version=1,
            analysis_kind=AnalysisKind.INITIAL,
            analysis_json={
                "canonical_snapshot": {
                    "deal_model_data": {"industry": "Legacy Industry"},
                    "analyst_report": "Original analyst report",
                }
            },
        )
        self.audit = AIAuditLog.objects.create(
            source_type="email",
            source_id="email-1",
            model_used="test-model",
            system_prompt="test",
            user_prompt="test",
            raw_response="",
            status="PROCESSING",
            is_success=False,
            source_metadata={"route": {"mode": "ENRICH_EXISTING", "deal_id": str(self.deal.id)}},
        )
        self.results = [
            {
                "status": "passed",
                "file_id": "body_email-1",
                "file_name": "Email Body",
                "normalized_text": "Revenue grew 30% in FY26.",
                "normalized_json": {"metrics": [{"name": "Revenue growth", "value": "30%"}]},
            },
            {
                "status": "passed",
                "file_id": "attachment-1",
                "file_name": "FY26 MIS.pdf",
                "normalized_text": "FY26 revenue was INR 80 Cr.",
                "normalized_json": {
                    "document_type": "Financials",
                    "metrics": [{"name": "Revenue", "value": "INR 80 Cr"}],
                    "source_map": {"document_name": "FY26 MIS.pdf"},
                },
            },
        ]

    @patch("ai_orchestrator.services.realtime.broadcast_audit_log_update")
    @patch("deals.tasks.log_worker_event")
    @patch("ai_orchestrator.services.ai_processor.AIProcessorService")
    def test_enrichment_is_supplemental_attachment_safe_and_retry_idempotent(
        self, ai_cls, _log_event, _broadcast
    ):
        ai_cls.return_value.process_content.return_value = {
            "deal_model_data": {
                "title": "Model Invented Title",
                "priority": "Low",
                "funding_ask": "INR 100 Cr",
                "industry": "Fintech",
            },
            "analyst_report": "FY26 evidence update",
            "metadata": {"ambiguous_points": []},
        }

        from deals.tasks import finalize_thread_analysis_async

        first = finalize_thread_analysis_async.run(
            self.results, str(self.deal.id), str(self.audit.id)
        )
        replay = finalize_thread_analysis_async.run(
            self.results, str(self.deal.id), str(self.audit.id)
        )

        self.deal.refresh_from_db()
        self.audit.refresh_from_db()
        self.assertEqual(first["status"], "success")
        self.assertTrue(replay["idempotent_replay"])
        self.assertEqual(self.deal.title, "Curated Deal Title")
        self.assertEqual(self.deal.priority, "High")
        self.assertEqual(self.deal.funding_ask, "INR 50 Cr")
        self.assertEqual(self.deal.industry, "Fintech")
        self.assertEqual(self.deal.analyses.count(), 2)
        supplemental = self.deal.analyses.get(analysis_kind=AnalysisKind.SUPPLEMENTAL)
        self.assertIn("Original analyst report", supplemental.analysis_json["canonical_snapshot"]["analyst_report"])
        self.assertIn("FY26 evidence update", supplemental.analysis_json["canonical_snapshot"]["analyst_report"])
        document = DealDocument.objects.get(deal=self.deal, onedrive_id="attachment-1")
        self.assertEqual(document.title, "FY26 MIS.pdf")
        self.assertTrue(document.is_ai_analyzed)
        self.assertEqual(DealDocument.objects.filter(deal=self.deal).count(), 1)
        self.assertEqual(self.audit.source_metadata["deal_analysis_version"], 2)


class DealPipelineT4CommandTests(TestCase):
    @patch("microsoft.management.commands.test_deal_pipelines_t4.AIProcessorService")
    @patch("microsoft.management.commands.test_deal_pipelines_t4.VLLMProviderService")
    def test_command_writes_sanitized_semantic_report(self, provider_cls, ai_cls):
        provider_cls.return_value.health_check.return_value = True
        provider_cls.return_value.get_available_models.return_value = []
        ai_cls.return_value.process_content.return_value = {
            "summary": "Acme Circular is raising 125 for recycling; revenue 80; Maya Rao leads."
        }
        report_path = "/tmp/deal-pipelines-command-test.json"

        call_command(
            "test_deal_pipelines_t4",
            scenario=["new_deal"],
            report_json=report_path,
        )

        import json

        with open(report_path, encoding="utf-8") as report_file:
            report = json.load(report_file)
        self.assertEqual(report["status"], "passed")
        self.assertEqual(report["scenarios"][0]["expected_route"], "PROPOSE_NEW")
