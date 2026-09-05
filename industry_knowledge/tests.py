from unittest.mock import Mock, patch

from django.contrib.auth.models import User
from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import Profile
from deals.models import Deal, DealDocument
from meetings.models import MeetingNote
from .models import IATheme, KnowledgeDocument, NewsArticle, NewsSource
from .services import ingest_source


class IndustryKnowledgeApiTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="analyst", password="password")
        self.profile = Profile.objects.create(user=self.user, name="Analyst", email="analyst-knowledge@example.com")
        self.deal = Deal.objects.create(title="Healthcare platform", sector="Healthcare")
        self.document = DealDocument.objects.create(deal=self.deal, title="Healthcare market report", document_type="Other", is_indexed=True, uploaded_by=self.profile)
        self.note = MeetingNote.objects.create(title="Industry expert call", body="Market structure discussion", is_indexed=True, created_by=self.profile)
        self.note.deals.add(self.deal)
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def test_publishes_indexed_report_with_metadata(self):
        response = self.client.post("/api/industry-knowledge/documents/", {
            "kind": "REPORT", "title": "Healthcare market report", "publisher": "Internal research",
            "sector": "Healthcare", "themes": ["Care delivery"], "visibility": "INTERNAL",
            "deal_document_id": str(self.document.id),
        }, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["source_deal"]["id"], str(self.deal.id))
        self.assertTrue(response.data["is_indexed"])

    def test_transcript_requires_confidentiality(self):
        response = self.client.post("/api/industry-knowledge/documents/", {
            "kind": "TRANSCRIPT", "title": "Expert call", "meeting_note_id": str(self.note.id),
        }, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertIn("confidentiality", response.data)

    def test_publishes_transcript_with_participants_and_redaction_note(self):
        response = self.client.post("/api/industry-knowledge/documents/", {
            "kind": "TRANSCRIPT", "title": "Expert call", "meeting_note_id": str(self.note.id),
            "publisher": "Investment team", "participants": "Partner; sector expert",
            "confidentiality": "IA internal", "redaction_note": "Names anonymised",
        }, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["participants"], "Partner; sector expert")
        self.assertEqual(response.data["redaction_note"], "Names anonymised")

    def test_user_can_follow_and_unfollow_theme(self):
        theme = IATheme.objects.create(name="Fintech")
        followed = self.client.post(f"/api/industry-knowledge/themes/{theme.id}/subscribe/")
        self.assertEqual(followed.status_code, 200, followed.data)
        self.assertTrue(followed.data["is_subscribed"])
        unfollowed = self.client.post(f"/api/industry-knowledge/themes/{theme.id}/unsubscribe/")
        self.assertEqual(unfollowed.status_code, 200, unfollowed.data)
        self.assertFalse(unfollowed.data["is_subscribed"])

    def test_restricted_document_is_visible_to_publisher(self):
        KnowledgeDocument.objects.create(kind="REPORT", title="Restricted", visibility="RESTRICTED", deal_document=self.document, published_by=self.profile)
        response = self.client.get("/api/industry-knowledge/documents/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["count"], 1)

    def test_user_can_save_dismiss_and_link_news(self):
        source = NewsSource.objects.create(name="Test source")
        article = NewsArticle.objects.create(source=source, title="Funding update", url="https://example.com/story")
        self.assertEqual(self.client.post(f"/api/industry-knowledge/news/{article.id}/save/").status_code, 200)
        self.assertTrue(article.saved_by.filter(id=self.user.id).exists())
        self.assertEqual(self.client.post(f"/api/industry-knowledge/news/{article.id}/link-deal/", {"deal_id": str(self.deal.id)}, format="json").status_code, 200)
        self.assertTrue(article.linked_deals.filter(id=self.deal.id).exists())
        self.assertEqual(self.client.post(f"/api/industry-knowledge/news/{article.id}/dismiss/").status_code, 204)


class NewsIngestionTests(TestCase):
    @patch("industry_knowledge.services.requests.get")
    def test_ingestion_deduplicates_and_maps_themes(self, get):
        theme = IATheme.objects.create(name="Healthcare", keywords=["healthcare", "hospital"])
        source = NewsSource.objects.create(name="Publisher", feed_url="https://example.com/feed", is_active=True)
        response = Mock()
        response.content = b"<rss><channel><item><title>Healthcare funding</title><link>https://example.com/a</link><description>Hospital platform</description><pubDate>Wed, 02 Oct 2024 10:00:00 GMT</pubDate></item></channel></rss>"
        response.raise_for_status.return_value = None
        get.return_value = response
        self.assertEqual(ingest_source(source)["created"], 1)
        self.assertEqual(ingest_source(source)["created"], 0)
        article = NewsArticle.objects.get(url="https://example.com/a")
        self.assertEqual(list(article.themes.values_list("id", flat=True)), [theme.id])

    @patch("industry_knowledge.services._ai_classifications")
    @patch("industry_knowledge.services._search_entries")
    def test_web_discovery_uses_ai_classification(self, search_entries, classifications):
        theme = IATheme.objects.create(name="Climate", keywords=["climate"])
        source = NewsSource.objects.create(name="Public publisher", homepage_url="https://publisher.example", is_active=True)
        search_entries.return_value = [{
            "title": "Battery company raises capital", "url": "https://publisher.example/battery",
            "summary": "The company develops storage systems.", "author": "", "published_at": "2026-08-30T09:00:00Z",
        }]
        classifications.return_value = {
            "https://publisher.example/battery": {"themes": ["Climate"], "companies": ["Battery Co"]}
        }
        result = ingest_source(source)
        article = NewsArticle.objects.get(url="https://publisher.example/battery")
        self.assertEqual(result["discovery"], "searxng_ai")
        self.assertEqual(article.companies, ["Battery Co"])
        self.assertEqual(list(article.themes.values_list("id", flat=True)), [theme.id])

    def test_licensed_source_requires_approved_feed(self):
        source = NewsSource.objects.create(name="Licensed", homepage_url="https://licensed.example", requires_licensed_api=True)
        with self.assertRaisesRegex(ValueError, "approved feed or API"):
            ingest_source(source)


class IndustryViewSetTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="analyst2", password="password")
        self.deal1 = Deal.objects.create(title="Fintech App", industry="Fintech", current_phase="Initial Review")
        self.deal2 = Deal.objects.create(title="PayTech Solutions", industry="Fintech", current_phase="Due Diligence")
        self.deal3 = Deal.objects.create(title="Cold Storage Logistics", industry="Cold Chain", current_phase="Passed")
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def test_list_industries_syncs_from_deals(self):
        response = self.client.get("/api/industry-knowledge/industries/")
        self.assertEqual(response.status_code, 200)
        names = [item["name"] for item in response.data]
        self.assertIn("Fintech", names)
        self.assertIn("Cold Chain", names)
        fintech = next(item for item in response.data if item["name"] == "Fintech")
        self.assertEqual(fintech["deals_count"], 2)

    def test_retrieve_industry_returns_historic_deals(self):
        response = self.client.get("/api/industry-knowledge/industries/")
        fintech_id = next(item["id"] for item in response.data if item["name"] == "Fintech")
        detail = self.client.get(f"/api/industry-knowledge/industries/{fintech_id}/")
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.data["name"], "Fintech")
        self.assertEqual(detail.data["deals_count"], 2)
        deal_titles = [d["title"] for d in detail.data["deals"]]
        self.assertIn("Fintech App", deal_titles)
        self.assertIn("PayTech Solutions", deal_titles)

    def test_merge_industries_updates_deals_and_provenance(self):
        self.client.get("/api/industry-knowledge/industries/")
        from industry_knowledge.models import Industry
        from deals.models import DealFieldProvenance

        cold_chain = Industry.objects.get(name="Cold Chain")
        fintech = Industry.objects.get(name="Fintech")

        merge_response = self.client.post("/api/industry-knowledge/industries/merge/", {
            "source_industry_id": str(cold_chain.id),
            "target_industry_id": str(fintech.id),
        }, format="json")

        self.assertEqual(merge_response.status_code, 200, merge_response.data)
        self.deal3.refresh_from_db()
        self.assertEqual(self.deal3.industry, "Fintech")
        self.assertFalse(Industry.objects.filter(name="Cold Chain").exists())

        provenance = DealFieldProvenance.objects.filter(deal=self.deal3, field_name="industry").first()
        self.assertIsNotNone(provenance)
        self.assertEqual(provenance.previous_value, "Cold Chain")
        self.assertEqual(provenance.value, "Fintech")

    @patch("ai_orchestrator.services.search_provider.SearXNGProviderService.search_results")
    def test_pull_industry_news(self, mock_search):
        mock_search.return_value = [{
            "title": "Fintech growth in India accelerates",
            "url": "https://example.com/fintech-growth",
            "snippet": "Venture funding increased this quarter.",
            "published_date": "2026-09-01T10:00:00Z",
        }]
        self.client.get("/api/industry-knowledge/industries/")
        from industry_knowledge.models import Industry
        fintech = Industry.objects.get(name="Fintech")

        response = self.client.post(f"/api/industry-knowledge/industries/{fintech.id}/pull-news/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["title"], "Fintech growth in India accelerates")

