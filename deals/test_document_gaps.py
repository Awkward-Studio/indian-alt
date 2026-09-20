from django.contrib.auth.models import User
from django.urls import reverse
from django.test import TestCase
from rest_framework.test import APIClient
from unittest.mock import patch

from accounts.models import Profile
from ai_orchestrator.services.document_processor import DocumentProcessorService
from deals.models import Deal, DealDocument


class DocumentGapQueueTests(TestCase):
    def setUp(self):
        user = User.objects.create_user(username="gaps@example.com", password="test")
        Profile.objects.create(user=user, email="gaps@example.com", name="Analyst")
        self.client = APIClient()
        self.client.force_authenticate(user)

    def test_folder_and_document_gaps_are_separate(self):
        no_folder = Deal.objects.create(title="No folder")
        no_documents = Deal.objects.create(
            title="No documents", source_onedrive_id="folder-1", source_drive_id="drive-1"
        )
        complete = Deal.objects.create(
            title="Complete", source_onedrive_id="folder-2", source_drive_id="drive-1"
        )
        DealDocument.objects.create(deal=no_folder, title="Pitch")
        DealDocument.objects.create(deal=complete, title="Memo")
        self._record_scan(no_documents, [])
        self._record_scan(complete, [{"id": "file-1", "name": "Pitch.pdf"}])

        response = self.client.get(reverse("deal-document-gaps"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual({item["id"] for item in response.json()["missing_folders"]}, {str(no_folder.id)})
        self.assertEqual({item["id"] for item in response.json()["missing_documents"]}, {str(no_documents.id)})

    def test_no_documents_excludes_unlinked_and_unscanned_deals(self):
        Deal.objects.create(title="Unlinked")
        Deal.objects.create(
            title="Linked but not scanned",
            source_onedrive_id="folder-unscanned",
            source_drive_id="drive-1",
        )

        response = self.client.get(reverse("deal-document-gaps"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["missing_documents"], [])

    def test_no_documents_includes_folder_with_only_unsupported_files(self):
        deal = Deal.objects.create(
            title="Unsupported files",
            source_onedrive_id="folder-unsupported",
            source_drive_id="drive-1",
        )
        self._record_scan(deal, [{"id": "file-1", "name": "Archive.xyz"}])

        response = self.client.get(reverse("deal-document-gaps"))

        self.assertEqual(
            {item["id"] for item in response.json()["missing_documents"]},
            {str(deal.id)},
        )

    def test_mark_no_folder_resolves_exception_and_linking_clears_marker(self):
        deal = Deal.objects.create(title="No folder")

        response = self.client.post(reverse("deal-mark-no-folder", args=[deal.id]))
        self.assertEqual(response.status_code, 200)
        deal.refresh_from_db()
        self.assertTrue(deal.folder_not_available)

        gaps = self.client.get(reverse("deal-document-gaps")).json()
        self.assertNotIn(str(deal.id), {item["id"] for item in gaps["missing_folders"]})

        with patch("deals.tasks.prepare_linked_folder_vdr_async.apply_async") as apply_async:
            apply_async.return_value.id = "task-1"
            response = self.client.patch(
                reverse("deal-connect-onedrive", args=[deal.id]),
                {"source_onedrive_id": "folder-new", "source_drive_id": "drive-1"},
                format="json",
            )
        self.assertEqual(response.status_code, 200)
        deal.refresh_from_db()
        self.assertFalse(deal.folder_not_available)

    @patch("deals.tasks.rescan_linked_deal_folder_async.apply_async")
    def test_scan_linked_folders_queues_only_fully_linked_deals(self, apply_async):
        linked = Deal.objects.create(
            title="Linked", source_onedrive_id="folder-1", source_drive_id="drive-1"
        )
        Deal.objects.create(title="Unlinked")
        Deal.objects.create(title="Missing drive", source_onedrive_id="folder-2")

        response = self.client.post(reverse("deal-scan-linked-folders"))

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["queued_count"], 1)
        apply_async.assert_called_once_with(
            kwargs={"deal_id": str(linked.id)},
            queue="low_priority",
        )

    def test_returns_every_missing_folder_with_dialog_metadata(self):
        Deal.objects.bulk_create([
            Deal(title=f"Missing folder {index}", deal_status="Interesting", priority="High")
            for index in range(55)
        ])

        response = self.client.get(reverse("deal-document-gaps"))

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["counts"]["missing_folders"], 55)
        self.assertEqual(len(payload["missing_folders"]), 55)
        self.assertEqual(payload["missing_folders"][0]["deal_status"], "Interesting")
        self.assertEqual(payload["missing_folders"][0]["priority"], "High")

    @staticmethod
    def _record_scan(deal, file_tree):
        supported_extensions = tuple(DocumentProcessorService.SUPPORTED_EXTENSIONS)
        readable_count = sum(
            1
            for item in file_tree
            if str(item.get("name") or "").lower().endswith(supported_extensions)
        )
        Deal.objects.filter(pk=deal.pk).update(
            folder_file_count=len(file_tree),
            folder_readable_file_count=readable_count,
        )
