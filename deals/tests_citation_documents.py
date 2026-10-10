import hashlib
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase
from rest_framework.test import APIClient

from deals.models import Deal, DealDocument
from microsoft.models import EmailAccount, EmailEvidenceLink, EmailPrivateBlob


class CitationDocumentTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.client.force_authenticate(User.objects.create_user(username='citation-reader'))
        self.deal = Deal.objects.create(title='Source company')
        self.url = f'/api/deals/{self.deal.id}/citation-document/'

    @patch('microsoft.services.graph_service.GraphAPIService')
    def test_saved_folder_link_opens_without_fetching_a_download_url(self, graph):
        source_url = 'https://tenant.sharepoint.com/sites/deals/Model.xlsx?web=1'
        DealDocument.objects.create(deal=self.deal, title='Model.xlsx',
            onedrive_id='drive-item', file_url=source_url, normalized_text='Indexed text')

        response = self.client.get(self.url, {'label': 'Model.xlsx, Summary!B2'})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['open_url'], source_url)
        self.assertFalse(response.data['download_available'])
        self.assertEqual(response.data['indexed_text'], '')
        graph.assert_not_called()

    @patch('microsoft.services.graph_service.GraphAPIService')
    def test_missing_folder_link_uses_graph_web_url(self, graph):
        source_url = 'https://tenant.sharepoint.com/sites/deals/Deck.pdf'
        graph.return_value.get_drive_item.return_value = {'webUrl': source_url}
        DealDocument.objects.create(deal=self.deal, title='Deck.pdf', onedrive_id='drive-item',
            evidence_json={'source_metadata': {'source_drive_id': 'source-drive'}})

        response = self.client.get(self.url, {'label': 'Deck.pdf, page 2'})

        self.assertEqual(response.data['open_url'], source_url)
        graph.return_value.get_drive_item.assert_called_once_with('source-drive', 'drive-item')
        graph.return_value.get_drive_item_download_url.assert_not_called()

    def test_original_email_pdf_download_matches_stored_binary(self):
        content = b'%PDF-original-binary-attachment\x00\xff'
        account = EmailAccount.objects.create(email='citation@example.test')
        blob = EmailPrivateBlob.objects.create(email_account=account,
            sha256=hashlib.sha256(content).hexdigest(), size=len(content), payload=content)
        doc = DealDocument.objects.create(deal=self.deal, title='Attached.pdf',
            normalized_text='Extracted text is different from the original PDF')
        EmailEvidenceLink.objects.create(email_account=account, deal=self.deal, document=doc,
            blob=blob, source_key='blob:' + blob.sha256, kind='email_attachment')

        resolved = self.client.get(self.url, {'label': doc.title})
        response = self.client.get(self.url, {'label': doc.title, 'download': '1'})

        self.assertEqual(resolved.data['source_kind'], 'email_attachment')
        self.assertTrue(resolved.data['download_available'])
        self.assertIsNone(resolved.data['open_url'])
        self.assertEqual(resolved.data['indexed_text'], '')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(b''.join(response.streaming_content), content)
        self.assertIn('attachment;', response['Content-Disposition'])
        self.assertEqual(response['Cache-Control'], 'private, no-store')

    def test_folder_document_cannot_use_email_attachment_download(self):
        DealDocument.objects.create(deal=self.deal, title='Folder.pdf',
            file_url='https://tenant.sharepoint.com/Folder.pdf')

        response = self.client.get(self.url, {'label': 'Folder.pdf', 'download': '1'})

        self.assertEqual(response.status_code, 404)

    def test_missing_original_keeps_indexed_text_as_an_explicit_fallback(self):
        DealDocument.objects.create(deal=self.deal, title='Indexed.json', normalized_text='Source text')

        response = self.client.get(self.url, {'label': 'Indexed.json'})

        self.assertIsNone(response.data['open_url'])
        self.assertFalse(response.data['download_available'])
        self.assertEqual(response.data['indexed_text'], 'Source text')

    def test_ambiguous_source_name_is_not_guessed(self):
        DealDocument.objects.create(deal=self.deal, title='Same.pdf')
        DealDocument.objects.create(deal=self.deal, title='Same.pdf')

        response = self.client.get(self.url, {'label': 'Same.pdf'})

        self.assertEqual(response.status_code, 409)
