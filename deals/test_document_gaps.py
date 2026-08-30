from django.contrib.auth.models import User
from django.urls import reverse
from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import Profile
from deals.models import Deal, DealDocument


class DocumentGapQueueTests(TestCase):
    def setUp(self):
        user = User.objects.create_user(username="gaps@example.com", password="test")
        Profile.objects.create(user=user, email="gaps@example.com", name="Analyst")
        self.client = APIClient()
        self.client.force_authenticate(user)

    def test_folder_and_document_gaps_are_separate(self):
        no_folder = Deal.objects.create(title="No folder")
        no_documents = Deal.objects.create(title="No documents", source_onedrive_id="folder-1")
        complete = Deal.objects.create(title="Complete", source_onedrive_id="folder-2")
        DealDocument.objects.create(deal=no_folder, title="Pitch")
        DealDocument.objects.create(deal=complete, title="Memo")

        response = self.client.get(reverse("deal-document-gaps"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual({item["id"] for item in response.json()["missing_folders"]}, {str(no_folder.id)})
        self.assertEqual({item["id"] for item in response.json()["missing_documents"]}, {str(no_documents.id)})
