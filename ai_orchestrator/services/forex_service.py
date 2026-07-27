import logging
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from typing import Any

import requests
from django.core.cache import cache

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ForexQuote:
    base_currency: str
    quote_currency: str
    rate: float
    provider: str
    effective_date: str | None
    retrieved_at: str
    is_stale: bool
    is_fallback: bool

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class ForexService:
    """
    Fetches a disclosed USD/INR quote.

    The fallback is deliberately marked stale and does not claim an effective
    market date. Existing callers may continue to use ``get_rate``.
    """
    CACHE_KEY = "usd_inr_forex_quote_v2"
    FALLBACK = 84.0
    PROVIDER = "exchangerate-api.com"
    _local_cache: ForexQuote | None = None

    @staticmethod
    def _retrieved_at() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _is_stale(effective_date: str | None) -> bool:
        if not effective_date:
            return True
        try:
            return (date.today() - date.fromisoformat(effective_date)).days > 2
        except ValueError:
            return True

    @classmethod
    def _from_cached_value(cls, value: Any) -> ForexQuote | None:
        if isinstance(value, dict):
            try:
                return ForexQuote(
                    base_currency=str(value["base_currency"]),
                    quote_currency=str(value["quote_currency"]),
                    rate=float(value["rate"]),
                    provider=str(value["provider"]),
                    effective_date=value.get("effective_date"),
                    retrieved_at=str(value["retrieved_at"]),
                    is_stale=bool(value["is_stale"]),
                    is_fallback=bool(value["is_fallback"]),
                )
            except (KeyError, TypeError, ValueError):
                return None
        return None

    def get_quote(self) -> ForexQuote:
        try:
            quote = self._from_cached_value(cache.get(self.CACHE_KEY))
            if quote:
                return quote
        except Exception as e:
            logger.warning("Forex cache get failed: %s", e)

        if ForexService._local_cache is not None:
            return ForexService._local_cache

        try:
            resp = requests.get("https://api.exchangerate-api.com/v4/latest/USD", timeout=3)
            if resp.status_code == 200:
                payload = resp.json()
                new_rate = float(payload.get("rates", {}).get("INR"))
                if new_rate <= 0:
                    raise ValueError("USD/INR rate must be positive")
                effective_date = payload.get("date")
                quote = ForexQuote(
                    base_currency="USD",
                    quote_currency="INR",
                    rate=new_rate,
                    provider=self.PROVIDER,
                    effective_date=effective_date,
                    retrieved_at=self._retrieved_at(),
                    is_stale=self._is_stale(effective_date),
                    is_fallback=False,
                )
                try:
                    cache.set(self.CACHE_KEY, quote.as_dict(), 60 * 60 * 24)
                except Exception as e:
                    logger.warning("Forex cache set failed: %s", e)
                ForexService._local_cache = quote
                return quote
        except Exception as e:
            logger.warning("Forex API unavailable; using disclosed fallback: %s", e)

        return ForexQuote(
            base_currency="USD",
            quote_currency="INR",
            rate=self.FALLBACK,
            provider="configured-fallback",
            effective_date=None,
            retrieved_at=self._retrieved_at(),
            is_stale=True,
            is_fallback=True,
        )

    def get_rate(self) -> float:
        return self.get_quote().rate

    def get_crore_string(self) -> str:
        """Returns e.g. '8.35 Cr'"""
        rate = self.get_rate()
        return f"{round(rate / 10, 2)} Cr"
