from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase

from deals.models import Deal, DealDocument


class ReindexDealDocumentsCommandTests(TestCase):
    def test_spreadsheets_only_dry_run_includes_legacy_file_extensions(self):
        deal = Deal.objects.create(title="Project Atlas")
        DealDocument.objects.create(
            deal=deal,
            title="Legacy Model.XLSX",
            extraction_manifest={},
            extracted_text="[Sheet: Model]\nA1=Revenue\tB1=100",
        )
        DealDocument.objects.create(
            deal=deal,
            title="Current Model",
            extraction_manifest={"kind": "spreadsheet"},
        )
        DealDocument.objects.create(
            deal=deal,
            title="Investment Memo.pdf",
            extraction_manifest={},
        )

        output = StringIO()
        call_command(
            "reindex_deal_documents",
            deal=str(deal.id),
            spreadsheets_only=True,
            dry_run=True,
            stdout=output,
        )

        rendered = output.getvalue()
        self.assertIn("Would reindex Legacy Model.XLSX", rendered)
        self.assertIn("Would reindex Current Model", rendered)
        self.assertNotIn("Investment Memo.pdf", rendered)
        self.assertIn("2 legacy spreadsheet(s) have no cell manifest", rendered)
        self.assertIn("1 can be rebuilt from stored text", rendered)
        self.assertIn("1 require source re-extraction", rendered)
        self.assertIn("Would reindex 2 document(s).", rendered)

    @patch("deals.management.commands.reindex_deal_documents.EmbeddingService")
    def test_manifests_only_persists_cells_without_calling_embeddings(self, embedding_service):
        deal = Deal.objects.create(title="Project Atlas")
        document = DealDocument.objects.create(
            deal=deal,
            title="Legacy Model.xlsx",
            extracted_text="[Sheet: Model]\nA1=Revenue\tB1=100",
            extraction_manifest={},
        )

        output = StringIO()
        call_command(
            "reindex_deal_documents",
            deal=str(deal.id),
            spreadsheets_only=True,
            manifests_only=True,
            stdout=output,
        )

        document.refresh_from_db()
        self.assertEqual(document.extraction_manifest["kind"], "spreadsheet")
        self.assertEqual(document.extraction_manifest["sheets"][0]["cells"][1], {
            "coordinate": "B1",
            "value": "100",
        })
        embedding_service.assert_not_called()
        self.assertIn("Rebuilt 1 spreadsheet cell manifest(s)", output.getvalue())
