from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import SimpleTestCase
from rest_framework.test import APIRequestFactory, force_authenticate

from ai_orchestrator.services.forex_service import ForexService
from ai_orchestrator.views import ForexRateView


class ForexServiceTests(SimpleTestCase):
    def setUp(self):
        ForexService._local_cache = None

    @patch("ai_orchestrator.services.forex_service.cache")
    @patch("ai_orchestrator.services.forex_service.requests.get")
    def test_live_quote_includes_provider_and_dates(self, get, cache):
        cache.get.return_value = None
        get.return_value = Mock(
            status_code=200,
            json=lambda: {"date": "2026-07-27", "rates": {"INR": 86.5}},
        )

        quote = ForexService().get_quote()

        self.assertEqual(quote.rate, 86.5)
        self.assertEqual(quote.provider, "exchangerate-api.com")
        self.assertEqual(quote.effective_date, "2026-07-27")
        self.assertFalse(quote.is_fallback)
        cache.set.assert_called_once()

    @patch("ai_orchestrator.services.forex_service.cache")
    @patch("ai_orchestrator.services.forex_service.requests.get", side_effect=TimeoutError)
    def test_fallback_is_never_presented_as_current(self, get, cache):
        cache.get.return_value = None

        quote = ForexService().get_quote()

        self.assertEqual(quote.rate, ForexService.FALLBACK)
        self.assertTrue(quote.is_stale)
        self.assertTrue(quote.is_fallback)
        self.assertIsNone(quote.effective_date)
        self.assertEqual(quote.provider, "configured-fallback")


class ForexRateViewTests(SimpleTestCase):
    def setUp(self):
        self.factory = APIRequestFactory()

    def test_authentication_is_required(self):
        response = ForexRateView.as_view()(self.factory.get("/api/ai/forex-rate/"))
        self.assertEqual(response.status_code, 401)

    @patch("ai_orchestrator.services.forex_service.ForexService.get_quote")
    def test_contract_exposes_units_and_quote_metadata(self, get_quote):
        get_quote.return_value = SimpleNamespace(
            as_dict=lambda: {
                "base_currency": "USD",
                "quote_currency": "INR",
                "rate": 86.5,
                "provider": "test-provider",
                "effective_date": "2026-07-27",
                "retrieved_at": "2026-07-27T00:00:00+00:00",
                "is_stale": False,
                "is_fallback": False,
            }
        )
        request = self.factory.get("/api/ai/forex-rate/")
        force_authenticate(request, user=SimpleNamespace(is_authenticated=True))

        response = ForexRateView.as_view()(request)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["canonical_currency"], "INR")
        self.assertEqual(response.data["supported_units"]["crore"], 10_000_000)
        self.assertEqual(response.data["supported_display_currencies"], ["INR", "USD"])
