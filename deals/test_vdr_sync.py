from unittest.mock import patch

from django.test import TestCase

from deals.models import Deal, DealDocument
from deals.services.vdr_sync import VDRSyncService


class VDRSyncServiceTests(TestCase):
    @patch("deals.services.vdr_sync.GraphAPIService")
    def test_existing_document_pickup_uses_recursive_folder_traversal(self, graph_service):
        deal = Deal.objects.create(
            title="Nested VDR",
            source_onedrive_id="folder-1",
            source_drive_id="drive-1",
        )
        document = DealDocument.objects.create(deal=deal, title="Discussion Document.pdf")
        graph_service.return_value.get_folder_tree.return_value = [{
            "id": "file-1",
            "name": "Discussion Document.pdf",
            "path": "Materials/Discussion Document.pdf",
            "webUrl": "https://example.test/document",
            "file": {"mimeType": "application/pdf"},
        }]

        count = VDRSyncService.sync_existing_analyses_to_folder(
            deal,
            user_email="analyst@example.test",
        )

        self.assertEqual(count, 1)
        graph_service.return_value.get_folder_tree.assert_called_once_with(
            "drive-1",
            "folder-1",
            user_email="analyst@example.test",
            max_depth=None,
            strict=True,
        )
        document.refresh_from_db()
        self.assertEqual(document.onedrive_id, "file-1")
        self.assertEqual(document.file_url, "https://example.test/document")
