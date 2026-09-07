import unittest
from datetime import date

from deals.services.company_news import (
    canonical_url, ground_cards, merge_cards, news_queries, publication_date, select_evidence,
)


class CompanyNewsQualityTests(unittest.TestCase):
    def source(self, **changes):
        return {"title": "Acme Commerce raises capital", "snippet": "Acme Commerce announced a funding round in India.",
                "url": "https://news.example/Acme", "published_date": "2025-01-15", **changes}

    def card(self, **changes):
        return {"title": "Funding round", "summary": "Acme Commerce announced funding.",
                "url": "https://news.example/Acme", "evidence_quote": "Acme Commerce announced a funding round in India.",
                "date": "2099-01-01", "source": "Invented publisher", **changes}

    def test_refinement_preserves_company_anchor(self):
        recent, background = news_queries("Acme Commerce", country="India", instruction="founder updates")
        self.assertTrue(all('"Acme Commerce"' in q for q in recent + background))
        self.assertIn("founder updates", recent[-1])
        self.assertIn("litigation", background[0])

    def test_urls_remove_tracking_without_changing_resource_identity(self):
        self.assertEqual(canonical_url("https://NEWS.example/Acme/?utm_source=x&article=2#top"), "https://news.example/Acme?article=2")
        self.assertNotEqual(canonical_url("https://news.example/Acme"), canonical_url("https://news.example/acme"))
        self.assertEqual(canonical_url("javascript:alert(1)"), "")
        self.assertEqual(canonical_url("https://user:password@news.example/story"), "")

    def test_namesake_and_empty_evidence_are_excluded(self):
        results = [self.source(), self.source(title="Acme film", snippet="A film released in India.", url="https://other.example/film"), self.source(snippet="", url="https://other.example/profile")]
        self.assertEqual(len(select_evidence(results, "Acme Commerce")), 1)

    def test_dates_and_source_diversity(self):
        results = [self.source(url=f"https://news.example/{i}", published_date=f"2025-01-{i+10}") for i in range(5)]
        results.append(self.source(url="https://second.example/story", published_date=""))
        selected = select_evidence(results, "Acme Commerce")
        self.assertEqual(len(selected), 4)
        self.assertEqual(selected[0]["published_date"], "2025-01-14")
        self.assertEqual(selected[-1]["published_date"], "")
        self.assertEqual(publication_date("2099-01-01", date(2026, 1, 1)), "")
        self.assertEqual(publication_date("yesterday"), "")

    def test_duplicate_result_can_supply_missing_publication_date(self):
        selected = select_evidence([self.source(published_date=""), self.source()], "Acme Commerce")
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0]["published_date"], "2025-01-15")

    def test_exact_source_and_quote_required(self):
        for changes in [{"url": "https://invented.example/story"}, {"evidence_quote": "A lawsuit was filed against the founders."}, {"evidence_quote": ""}]:
            self.assertEqual(ground_cards({"news_cards": [self.card(**changes)]}, [self.source()]), [])

    def test_publication_metadata_overrides_invented_date_and_publisher(self):
        cards = ground_cards({"news_cards": [self.card(), self.card()]}, [self.source()])
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]["date"], "2025-01-15")
        self.assertEqual(cards[0]["published_at"], "2025-01-15")
        self.assertEqual(cards[0]["published_date_raw"], "2025-01-15")
        self.assertEqual(cards[0]["date_source"], "search_result_publication_metadata")
        self.assertTrue(cards[0]["retrieved_at"])
        self.assertEqual(cards[0]["source"], "news.example")
        self.assertEqual(cards[0]["evidence_level"], "search_snippet")

    def test_unknown_date_is_withheld(self):
        cards = ground_cards({"news_cards": [self.card()]}, [self.source(published_date="")])
        self.assertEqual(cards, [])

    def test_refresh_keeps_new_items_before_full_history(self):
        old = [self.card(url=f"https://news.example/{i}", title=f"Old {i}") for i in range(10)]
        new = self.card(title="New finding")
        merged = merge_cards([new], old)
        self.assertEqual(len(merged), 10)
        self.assertEqual(merged[0], new)

    def test_merge_deduplicates_urls_and_titles(self):
        new = self.card()
        self.assertEqual(len(merge_cards([new], [self.card(url=new["url"] + "?utm_source=x"), self.card(url="https://second.example/same-event")])), 1)
