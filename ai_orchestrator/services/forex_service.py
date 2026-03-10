import requests
import logging
from django.core.cache import cache

logger = logging.getLogger(__name__)

class ForexService:
    """
    Fetches live USD to INR rates. 
    Defaults to 84.0 (8.4 Cr / 1M) if offline.
    """
    CACHE_KEY = "live_usd_inr_rate"
    FALLBACK = 84.0

    def get_rate(self) -> float:
        rate = cache.get(self.CACHE_KEY)
        if rate: return float(rate)

        try:
            # Reliable free public API
            resp = requests.get("https://api.exchangerate-api.com/v4/latest/USD", timeout=3)
            if resp.status_code == 200:
                new_rate = resp.json().get('rates', {}).get('INR', self.FALLBACK)
                cache.set(self.CACHE_KEY, new_rate, 60 * 60 * 24) # 24h cache
                return float(new_rate)
        except Exception as e:
            logger.warning(f"Forex API unreachable, using fallback: {str(e)}")
        
        return self.FALLBACK

    def get_crore_string(self) -> str:
        """Returns e.g. '8.35 Cr'"""
        rate = self.get_rate()
        return f"{round(rate / 10, 2)} Cr"
